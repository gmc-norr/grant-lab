from datetime import UTC, datetime

import httpx
import pytest

from call_monitor.sources.types import AdapterError
from call_monitor.sources.vinnova import VinnovaAdapter

_FAKE_OK_BODY = [
    {
        "Diarienummer": "2026-01234",
        "Titel": "Datadriven sjukvård",
        "Beskrivning": "Stöd för datadriven utveckling i hälso- och sjukvård.",
        "OppningsDatum": "2026-04-01",
        "StangningsDatum": "2026-06-30",
        "Budget": 50000000,
        "Valuta": "SEK",
        "Program": "Datadriven hälsa",
        "Lankar": [
            {"Url": "https://www.vinnova.se/e/2026-01234"}
        ],
        "Nyckelord": ["digital pathology", "ai"],
    }
]


def _mock_transport(status: int = 200, body=None, raise_exc=None):
    def handler(request):
        if raise_exc is not None:
            raise raise_exc
        # Sanity check: header negotiation
        assert request.headers.get("Accept") == "application/vnd.vinnova.opendata.2.0+json"
        return httpx.Response(status, json=body if body is not None else _FAKE_OK_BODY)
    return httpx.MockTransport(handler)


def test_fetch_normalizes_vinnova_record():
    adapter = VinnovaAdapter(
        api_key="test-key",
        base_url="https://data.vinnova.se",
        transport=_mock_transport(),
    )
    out = list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert len(out) == 1
    c = out[0]
    assert c.external_id == "2026-01234"
    assert c.title == "Datadriven sjukvård"
    # OppningsDatum interpreted as published_at (Europe/Stockholm midnight → UTC)
    assert c.published_at == datetime(2026, 4, 1, tzinfo=UTC)
    # StangningsDatum at 23:59 Europe/Stockholm → 21:59 UTC during summer (CEST)
    # We store as the day's end in the *source's* timezone; conversion happens
    # at write. Adapter MUST emit tz-aware UTC.
    assert c.deadline is not None and c.deadline.tzinfo is not None
    assert c.budget_max == 50_000_000
    assert c.currency == "SEK"
    assert c.url == "https://www.vinnova.se/e/2026-01234"
    assert "ai" in c.keywords


def test_skips_records_without_diarienummer():
    body = [
        {"Diarienummer": "OK", "Titel": "T", "Lankar": [{"Url": "u"}]},
        {"Titel": "no id"},
    ]
    adapter = VinnovaAdapter(
        api_key="k", base_url="https://x", transport=_mock_transport(body=body)
    )
    out = list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert [c.external_id for c in out] == ["OK"]


def test_5xx_raises_adapter_error():
    adapter = VinnovaAdapter(
        api_key="k", base_url="https://x", transport=_mock_transport(status=502)
    )
    with pytest.raises(AdapterError):
        list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))


def test_no_api_key_raises_at_construction():
    with pytest.raises(ValueError):
        VinnovaAdapter(api_key="", base_url="https://x")
