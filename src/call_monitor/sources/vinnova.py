from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

from call_monitor.sources.types import AdapterError, NormalizedCall, is_safe_url

_logger = logging.getLogger(__name__)
_STOCKHOLM = ZoneInfo("Europe/Stockholm")
_ACCEPT_V2 = "application/vnd.vinnova.opendata.2.0+json"


def _parse_iso_stockholm(s: str | None) -> datetime | None:
    """Parse a naive ISO timestamp from Vinnova (e.g. ``"2022-04-11T00:00:00"``)
    and interpret it as Europe/Stockholm wall-clock time.

    Returns a UTC-aware datetime, or None on any malformed / missing input.
    """
    if not s:
        return None
    try:
        naive = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    local = naive.replace(tzinfo=_STOCKHOLM)
    return local.astimezone(UTC)


class VinnovaAdapter:
    """SourceAdapter for Vinnova's open-data two-stage API.

    Stage 1: ``GET /api/utlysningar/{date}``  — list of programmes.
    Stage 2: ``GET /api/ansokningsomgangar/{Dnr}`` — one round per call.

    Both endpoints are unauthenticated (no API key required).
    """

    slug = "vinnova"

    def __init__(
        self,
        *,
        base_url: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        sleep_between_rounds: float = 0.15,
    ):
        # Insist on https — even though the endpoint is unauthenticated,
        # enforcing TLS prevents accidental misconfiguration that could
        # let a network attacker tamper with funding-call data in transit.
        # Tests can pass an https URL alongside a MockTransport (no real
        # connection happens).
        normalized = base_url.rstrip("/")
        if not normalized.lower().startswith("https://"):
            raise ValueError(
                f"VinnovaAdapter requires an https:// base_url; got {base_url!r}"
            )
        self._base_url = normalized
        self._transport = transport
        self._timeout = timeout
        self._sleep_between_rounds = sleep_between_rounds

    def fetch_new_calls(self, since: datetime) -> Iterator[NormalizedCall]:
        # The endpoint takes a date suffix: modified-since semantics.
        date_suffix = since.astimezone(_STOCKHOLM).date().isoformat()
        programmes_path = f"/api/utlysningar/{date_suffix}"

        try:
            with httpx.Client(
                base_url=self._base_url,
                transport=self._transport,
                timeout=self._timeout,
            ) as client:
                resp = client.get(programmes_path, headers={"Accept": _ACCEPT_V2})
                resp.raise_for_status()
                programmes = resp.json()

                if not isinstance(programmes, list):
                    raise AdapterError(
                        f"Vinnova /utlysningar returned non-list: {type(programmes).__name__}"
                    )

                first_round = True
                for programme in programmes:
                    for round_ref in programme.get("AnsokningsomgangDnrLista") or []:
                        dnr = round_ref.get("DiarienummerAnsokningsomgang")
                        if not dnr:
                            continue
                        if not first_round:
                            time.sleep(self._sleep_between_rounds)
                        first_round = False

                        # An HTTP error on any one round aborts the whole
                        # batch (raise_for_status → AdapterError below). The
                        # orchestrator's idempotent upsert tolerates that:
                        # the next tick re-fetches and picks up where we
                        # stopped. Per-round try/except would swallow
                        # systemic outages we want to alert on.
                        round_path = f"/api/ansokningsomgangar/{dnr}"
                        round_resp = client.get(round_path, headers={"Accept": _ACCEPT_V2})
                        round_resp.raise_for_status()
                        round_list = round_resp.json()

                        if not isinstance(round_list, list):
                            raise AdapterError(
                                f"Vinnova /ansokningsomgangar/{dnr} returned non-list: "
                                f"{type(round_list).__name__}"
                            )

                        if not round_list:
                            continue
                        normalized = self._normalize(round_list[0])
                        if normalized is not None:
                            yield normalized

        except httpx.HTTPError as e:
            raise AdapterError(f"Vinnova fetch failed: {e}") from e
        except AdapterError:
            raise
        except ValueError as e:
            raise AdapterError(f"Vinnova returned non-JSON body: {e}") from e

    def _normalize(self, item: dict) -> NormalizedCall | None:
        now_utc = datetime.now(UTC)

        external_id = item.get("Diarienummer")
        title = item.get("Titel")
        if not external_id or not title:
            _logger.info(
                "vinnova.skip_incomplete",
                extra={
                    "event": "skip_incomplete",
                    "fields_present": list(item.keys()),
                },
            )
            return None

        # Only surface rounds that are publicly visible on the web portal.
        if item.get("Publik") != 1 or item.get("Webbsida") != 1:
            _logger.info(
                "vinnova.skip_unpublished",
                extra={"event": "skip_unpublished", "external_id": str(external_id)},
            )
            return None

        # Skip rounds whose deadline has already passed. We also skip rounds
        # with NO Stangningsdatum (perpetual / rolling) — they're rare in
        # Vinnova's data and ranking against an open-ended deadline confuses
        # the dashboard's deadline-sorted feed. Revisit if real perpetuals
        # ever surface and matter to a hospital-research user.
        deadline = _parse_iso_stockholm(item.get("Stangningsdatum"))
        if deadline is None or deadline <= now_utc:
            _logger.info(
                "vinnova.skip_closed",
                extra={"event": "skip_closed", "external_id": str(external_id)},
            )
            return None

        # The URL lives in LankLista; each entry has a "URL" key (uppercase).
        url = ""
        for link in item.get("LankLista") or []:
            if isinstance(link, dict) and link.get("URL"):
                candidate = str(link["URL"])
                if is_safe_url(candidate):
                    url = candidate
                    break
        if not url:
            _logger.info(
                "vinnova.skip_no_url",
                extra={"event": "skip_no_url", "external_id": str(external_id)},
            )
            return None

        published_at = _parse_iso_stockholm(item.get("Oppningsdatum"))

        return NormalizedCall(
            external_id=str(external_id),
            url=url,
            title=str(title),
            summary=item.get("Beskrivning"),
            deadline=deadline,
            budget_min=None,
            budget_max=None,
            currency=None,
            programme=None,
            keywords=[],
            published_at=published_at,
            raw=item,
        )
