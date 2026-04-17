from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

from call_monitor.sources.types import AdapterError, NormalizedCall, is_safe_url

_logger = logging.getLogger(__name__)
_STOCKHOLM = ZoneInfo("Europe/Stockholm")
_ACCEPT_V2 = "application/vnd.vinnova.opendata.2.0+json"


def _parse_ymd(s: str | None) -> tuple[int, int, int] | None:
    """Parse a Vinnova `YYYY-MM-DD` calendar date into (y, m, d) ints.

    Returns None on any malformed input — the caller decides what UTC
    instant the date should map to (see _publication_at vs _deadline_at).
    """
    if not s:
        return None
    try:
        y, m, d = (int(p) for p in s.split("-"))
    except (ValueError, AttributeError):
        return None
    return y, m, d


def _publication_at(ymd: str | None) -> datetime | None:
    """Map an `OppningsDatum` (publication day) to UTC midnight of that
    calendar day. Vinnova only gives us the day, not the moment of
    publication; UTC midnight preserves the calendar date verbatim
    (no DST shifts) and the dashboard renders just the date anyway.
    """
    parsed = _parse_ymd(ymd)
    if parsed is None:
        return None
    y, m, d = parsed
    return datetime(y, m, d, 0, 0, tzinfo=UTC)


def _deadline_at(ymd: str | None) -> datetime | None:
    """Map a `StangningsDatum` (closing day) to 23:59 Europe/Stockholm of
    that calendar day, then convert to UTC. Deadlines ARE wall-clock
    cutoffs ('applications must be in by end of day Stockholm') so the
    timezone math is load-bearing — a deadline at 23:59 CEST (April–Oct)
    is 21:59 UTC; at 23:59 CET (Nov–Mar) it's 22:59 UTC.
    """
    parsed = _parse_ymd(ymd)
    if parsed is None:
        return None
    y, m, d = parsed
    local = datetime(y, m, d, 23, 59, tzinfo=_STOCKHOLM)
    return local.astimezone(UTC)


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
        # Insist on https — the API key is sent as a header on every request,
        # and a misconfigured `VINNOVA_BASE_URL=http://...` would leak the
        # subscription key in plaintext over the network. Tests can pass an
        # https URL alongside a MockTransport (no real connection happens).
        normalized = base_url.rstrip("/")
        if not normalized.lower().startswith("https://"):
            raise ValueError(
                f"VinnovaAdapter requires an https:// base_url; got {base_url!r}"
            )
        self._api_key = api_key
        self._base_url = normalized
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
        # Defense in depth against stored XSS — see is_safe_url docstring.
        if not is_safe_url(url):
            _logger.warning(
                "vinnova.skip_unsafe_url",
                extra={"event": "skip_unsafe_url", "external_id": str(external_id)},
            )
            return None

        budget = item.get("Budget")
        return NormalizedCall(
            external_id=str(external_id),
            url=url,
            title=str(title),
            summary=item.get("Beskrivning"),
            deadline=_deadline_at(item.get("StangningsDatum")),
            budget_max=int(budget) if isinstance(budget, int | float) else None,
            currency=item.get("Valuta"),
            programme=item.get("Program"),
            keywords=list(item.get("Nyckelord") or []),
            published_at=_publication_at(item.get("OppningsDatum")),
            raw=item,
        )
