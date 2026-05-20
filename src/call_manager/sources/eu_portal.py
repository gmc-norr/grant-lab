from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx

from call_manager.sources.extraction import fetch_html_main_content
from call_manager.sources.types import AdapterError, NormalizedCall, is_safe_url

_logger = logging.getLogger(__name__)

# v3.0 search endpoint returns OPEN + FORTHCOMING calls in JSON form.
# Pin a versioned path so a portal-side rev to v4 is a deliberate code change.
_SEARCH_PATH = "/info/funding-tenders/opportunities/api/v3.0/notices/search"

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

    def fetch_new_calls(self, since: datetime) -> Iterator[NormalizedCall]:
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
        try:
            with httpx.Client(
                base_url=self._base_url,
                transport=self._transport,
                timeout=self._timeout,
            ) as client:
                resp = client.get(_SEARCH_PATH, params=params)
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPError as e:
            raise AdapterError(f"EU Portal fetch failed: {e}") from e
        except ValueError as e:  # JSON decode
            raise AdapterError(f"EU Portal returned non-JSON body: {e}") from e

        results = payload.get("results", []) if isinstance(payload, dict) else []
        for item in results:
            normalized = self._normalize(item)
            if normalized is None:
                continue
            yield normalized

    def _normalize(self, item: dict) -> NormalizedCall | None:
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

        # Best-effort scrape of the topic-detail page for the call body.
        # `fetch_html_main_content` returns None on HTTP error / empty
        # extraction (and logs); we just propagate that — ranking can still
        # use `summary` alone if the scrape fails.
        full_text = fetch_html_main_content(str(url), transport=self._extraction_transport)

        return NormalizedCall(
            external_id=str(external_id),
            url=str(url),
            title=str(title),
            summary=item.get("summary"),
            full_text=full_text,
            deadline=_parse_iso(item.get("deadlineDate")),
            budget_min=item.get("budgetMin") if isinstance(item.get("budgetMin"), int) else None,
            budget_max=(
                item.get("budgetTotal") if isinstance(item.get("budgetTotal"), int) else None
            ),
            currency=item.get("currency"),
            programme=item.get("programmeName"),
            keywords=list(item.get("keywords") or []),
            published_at=_parse_iso(item.get("publicationDate")),
            raw=item,
        )
