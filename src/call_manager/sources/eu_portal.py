from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx

from call_manager.sources.extraction import fetch_html_main_content
from call_manager.sources.types import (
    AdapterError,
    NeedsBody,
    NormalizedCall,
    is_safe_url,
    request_with_retry,
)

_logger = logging.getLogger(__name__)

# v3.0 search endpoint returns OPEN + FORTHCOMING calls in JSON form.
# Pin a versioned path so a portal-side rev to v4 is a deliberate code change.
_SEARCH_PATH = "/info/funding-tenders/opportunities/api/v3.0/notices/search"

# Page size per request and a hard cap on page count. The cap is a defensive
# guard against an upstream that never decrements its remaining-results count
# (which would otherwise spin forever). 200 * 100 = 20k calls is far beyond
# the realistic OPEN+FORTHCOMING set.
_PAGE_SIZE = 100
_MAX_PAGES = 200

# ---------------------------------------------------------------------------
# Plan F (full-text ingestion) — investigation outcome
# ---------------------------------------------------------------------------
# The EU Funding & Tenders portal's search API (v3.0 /notices/search) returns
# only a brief abstract per topic in `summary`. Inspection of the existing
# canned fixtures (tests/test_sources_eu_portal.py::_FAKE_OK_BODY) shows the
# only string-valued body field is `summary` — there is no `description`,
# `topicDescription`, `objectives`, `expectedImpact`, `additionalInfo`,
# `callConditions`, or `topicConditions` field present in the payload the
# adapter consumes. The substantive call body lives on the topic-detail HTML
# page that `url` points at.
#
# Outcome: B — scrape the topic-detail page via `fetch_html_main_content`
# as a best-effort fallback to populate `full_text`. On scrape failure the
# adapter logs and proceeds with `full_text=None`; ranking still works using
# the `summary` field.
# ---------------------------------------------------------------------------


def _parse_iso(value: str | None) -> datetime | None:
    """Parse the portal's ISO timestamps into tz-aware UTC.

    The portal mostly emits 'Z' suffix; treat naive parses as UTC since the
    spec mandates UTC storage.
    """
    if not value:
        return None
    try:
        # `fromisoformat` accepts 'Z' suffix on Python 3.11+.
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _coerce_budget(value) -> int | None:
    """Coerce a budget field to int. Accepts int or float (the portal often
    serializes amounts as floats, e.g. 12000000.0); rejects bool and anything
    non-numeric. Returns None when the value isn't a usable number.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


class EUPortalAdapter:
    """SourceAdapter for the EU Funding & Tenders Portal.

    EU portal's search API returns only a brief abstract; full call body is
    on the topic-detail HTML page. We scrape via `fetch_html_main_content`
    as a fallback to populate `full_text`. The scrape transport is injected
    separately from the API transport so tests can mock the two endpoints
    independently.
    """

    slug = "eu_portal"

    def __init__(
        self,
        *,
        base_url: str,
        transport: httpx.BaseTransport | None = None,
        extraction_transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._extraction_transport = extraction_transport
        self._timeout = timeout

    def fetch_new_calls(
        self, since: datetime, *, needs_body: NeedsBody | None = None
    ) -> Iterator[NormalizedCall]:
        # `needs_body(identifier) -> bool`: the orchestrator builds this from a
        # cheap DB query of which calls already have a non-null `full_text`.
        # When it returns False we SKIP the topic-detail scrape entirely — the
        # body is already in the DB and upsert.py coalesces None over the
        # existing value. This turns the per-run cost from "scrape every call"
        # (hundreds of serial blocking HTTP+parse calls) into "scrape only
        # brand-new / body-less calls". When `needs_body` is None (e.g. CLI
        # one-shots that don't wire it), we fall back to always scraping, the
        # pre-fix behavior.
        #
        # Repeated `status` params (status=OPEN&status=FORTHCOMING) are the
        # safest cross-API encoding — comma-separated values get URL-encoded
        # to %2C and not all servers reverse that.
        params = [("status", "OPEN"), ("status", "FORTHCOMING")]
        # `since` is informational at this endpoint — server-side filtering
        # by date isn't always honored. The orchestrator's idempotent upsert
        # tolerates fetching the full set on every run.
        # TODO (post-Plan 3): a periodic reaper should mark calls absent from
        # consecutive fetches as stale, so the dashboard doesn't accumulate
        # CLOSED entries indefinitely. Currently the orchestrator is
        # additive-only — by design at v1 — but worth scheduling once the
        # ranking and digest layers exist.
        seen = 0
        try:
            with httpx.Client(
                base_url=self._base_url,
                transport=self._transport,
                timeout=self._timeout,
            ) as client:
                # Paginate: the search API returns one page of `results` at a
                # time. A single GET silently truncated to the first page,
                # dropping every call beyond it. Loop over pageNumber until we
                # exhaust the result set (empty page, or seen >= totalResults),
                # bounded by _MAX_PAGES so a misbehaving server can't spin us
                # forever.
                for page_number in range(1, _MAX_PAGES + 1):
                    page_params = [
                        *params,
                        ("pageSize", str(_PAGE_SIZE)),
                        ("pageNumber", str(page_number)),
                    ]
                    # The v3.0 search endpoint expects a query body; `text="*"`
                    # matches all notices (server-side date/status filtering is
                    # applied via the params above).
                    resp = request_with_retry(
                        client, "POST", _SEARCH_PATH, params=page_params, json={"text": "*"}
                    )
                    resp.raise_for_status()
                    payload = resp.json()

                    results = payload.get("results", []) if isinstance(payload, dict) else []
                    if not results:
                        break
                    for item in results:
                        normalized = self._normalize(item, needs_body=needs_body)
                        if normalized is not None:
                            yield normalized
                    seen += len(results)

                    total = payload.get("totalResults") if isinstance(payload, dict) else None
                    if isinstance(total, int) and seen >= total:
                        break
                    if len(results) < _PAGE_SIZE:
                        # A short page means there is no next page.
                        break
        except httpx.HTTPError as e:
            raise AdapterError(f"EU Portal fetch failed: {e}") from e
        except ValueError as e:  # JSON decode
            raise AdapterError(f"EU Portal returned non-JSON body: {e}") from e

    def _normalize(
        self, item: dict, *, needs_body: NeedsBody | None = None
    ) -> NormalizedCall | None:
        # Required fields per our contract.
        external_id = item.get("identifier")
        title = item.get("title")
        url = item.get("url")
        if not (external_id and title and url):
            _logger.info(
                "eu_portal.skip_incomplete",
                extra={"event": "skip_incomplete", "fields_present": list(item.keys())},
            )
            return None
        # Defense in depth against stored XSS — see is_safe_url docstring.
        if not is_safe_url(str(url)):
            _logger.warning(
                "eu_portal.skip_unsafe_url",
                extra={"event": "skip_unsafe_url", "external_id": str(external_id)},
            )
            return None

        # Best-effort scrape of the topic-detail page for the call body —
        # but only when the body is actually needed. `needs_body` returns
        # False for calls already ingested with a non-null `full_text`, so we
        # skip the (blocking, serial) scrape and let upsert's coalesce keep the
        # existing body. When `needs_body` is None we always scrape (CLI /
        # pre-fix behavior). `fetch_html_main_content` returns None on HTTP
        # error / empty extraction (and logs); ranking can still use `summary`.
        full_text: str | None = None
        if needs_body is None or needs_body(str(external_id)):
            full_text = fetch_html_main_content(str(url), transport=self._extraction_transport)

        return NormalizedCall(
            external_id=str(external_id),
            url=str(url),
            title=str(title),
            summary=item.get("summary"),
            full_text=full_text,
            deadline=_parse_iso(item.get("deadlineDate")),
            budget_min=_coerce_budget(item.get("budgetMin")),
            budget_max=_coerce_budget(item.get("budgetTotal")),
            currency=item.get("currency"),
            programme=item.get("programmeName"),
            keywords=list(item.get("keywords") or []),
            published_at=_parse_iso(item.get("publicationDate")),
            raw=item,
        )
