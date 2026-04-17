from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

from call_monitor.sources.types import AdapterError, NormalizedCall

_logger = logging.getLogger(__name__)
_STOCKHOLM = ZoneInfo("Europe/Stockholm")
_ACCEPT_V2 = "application/vnd.vinnova.opendata.2.0+json"


def _parse_local_date(s: str | None, *, end_of_day: bool) -> datetime | None:
    """Parse a Vinnova `YYYY-MM-DD` string to a tz-aware UTC datetime.

    `end_of_day=True` → 23:59:00 Europe/Stockholm wall time (used for deadlines),
    then converted to UTC. This preserves the correct wall-clock cutoff.
    `end_of_day=False` → 00:00:00 UTC (publication date; stored as UTC midnight
    so the calendar date is preserved without timezone shift).
    Returns a tz-aware UTC datetime, since the spec mandates UTC storage.
    """
    if not s:
        return None
    try:
        y, m, d = (int(p) for p in s.split("-"))
    except (ValueError, AttributeError):
        return None
    if end_of_day:
        local = datetime(y, m, d, 23, 59, tzinfo=_STOCKHOLM)
        return local.astimezone(UTC)
    return datetime(y, m, d, 0, 0, tzinfo=UTC)


class VinnovaAdapter:
    """SourceAdapter for Vinnova's open-data Utlysningar endpoint."""

    slug = "vinnova"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ):
        if not api_key:
            raise ValueError("VinnovaAdapter requires a non-empty api_key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout

    def fetch_new_calls(self, since: datetime) -> Iterator[NormalizedCall]:
        # The endpoint takes a date suffix and returns calls modified on/after.
        date_suffix = since.astimezone(_STOCKHOLM).date().isoformat()
        path = f"/api/utlysningar/{date_suffix}"
        try:
            with httpx.Client(
                base_url=self._base_url,
                transport=self._transport,
                timeout=self._timeout,
            ) as client:
                resp = client.get(
                    path,
                    headers={
                        "Accept": _ACCEPT_V2,
                        "Ocp-Apim-Subscription-Key": self._api_key,
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPError as e:
            raise AdapterError(f"Vinnova fetch failed: {e}") from e
        except ValueError as e:
            raise AdapterError(f"Vinnova returned non-JSON body: {e}") from e

        if not isinstance(payload, list):
            raise AdapterError(
                f"Vinnova returned non-list payload: {type(payload).__name__}"
            )

        for item in payload:
            normalized = self._normalize(item)
            if normalized is None:
                continue
            yield normalized

    def _normalize(self, item: dict) -> NormalizedCall | None:
        external_id = item.get("Diarienummer")
        title = item.get("Titel")
        if not external_id or not title:
            _logger.info(
                "vinnova.skip_incomplete",
                extra={"event": "skip_incomplete", "fields_present": list(item.keys())},
            )
            return None

        # The portal nests the canonical URL inside `Lankar`. Take the first.
        url = ""
        for link in item.get("Lankar") or []:
            if isinstance(link, dict) and link.get("Url"):
                url = str(link["Url"])
                break
        if not url:
            return None

        budget = item.get("Budget")
        return NormalizedCall(
            external_id=str(external_id),
            url=url,
            title=str(title),
            summary=item.get("Beskrivning"),
            deadline=_parse_local_date(item.get("StangningsDatum"), end_of_day=True),
            budget_max=int(budget) if isinstance(budget, int | float) else None,
            currency=item.get("Valuta"),
            programme=item.get("Program"),
            keywords=list(item.get("Nyckelord") or []),
            published_at=_parse_local_date(item.get("OppningsDatum"), end_of_day=False),
            raw=item,
        )
