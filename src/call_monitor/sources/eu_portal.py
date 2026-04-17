from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx

from call_monitor.sources.types import AdapterError, NormalizedCall

_logger = logging.getLogger(__name__)

# v3.0 search endpoint returns OPEN + FORTHCOMING calls in JSON form.
# Pin a versioned path so a portal-side rev to v4 is a deliberate code change.
_SEARCH_PATH = "/info/funding-tenders/opportunities/api/v3.0/notices/search"


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
    """SourceAdapter for the EU Funding & Tenders Portal."""

    slug = "eu_portal"

    def __init__(
        self,
        *,
        base_url: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout

    def fetch_new_calls(self, since: datetime) -> Iterator[NormalizedCall]:
        params = {
            "status": "OPEN,FORTHCOMING",
            # `since` is informational at this endpoint — server-side filtering
            # by date isn't always honored. The orchestrator's idempotent upsert
            # tolerates fetching the full set on every run.
        }
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

        return NormalizedCall(
            external_id=str(external_id),
            url=str(url),
            title=str(title),
            summary=item.get("summary"),
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
