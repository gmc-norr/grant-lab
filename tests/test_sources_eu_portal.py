from datetime import UTC, datetime

import httpx
import pytest

from call_monitor.sources.eu_portal import EUPortalAdapter
from call_monitor.sources.types import AdapterError

_FAKE_OK_BODY = {
    "results": [
        {
            "identifier": "HORIZON-HLTH-2026-DISEASE-03-15",
            "title": "AI for early disease detection",
            "summary": "Calls supporting AI methods for early-stage disease detection.",
            "deadlineDate": "2026-09-15T17:00:00Z",
            "budgetTotal": 12000000,
            "currency": "EUR",
            "programmeName": "Horizon Europe",
            "keywords": ["health", "ai", "diagnostics"],
            "publicationDate": "2026-04-01T00:00:00Z",
            "url": "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/HORIZON-HLTH-2026-DISEASE-03-15",
        }
    ],
    "totalResults": 1,
}


def _mock_transport(status: int = 200, body=None, raise_exc=None):
    def handler(request):
        if raise_exc is not None:
            raise raise_exc
        return httpx.Response(status, json=body if body is not None else _FAKE_OK_BODY)
    return httpx.MockTransport(handler)


def test_fetch_yields_normalized_calls():
    adapter = EUPortalAdapter(
        base_url="https://ec.europa.eu",
        transport=_mock_transport(),
    )
    out = list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert len(out) == 1
    c = out[0]
    assert c.external_id == "HORIZON-HLTH-2026-DISEASE-03-15"
    assert c.title.startswith("AI for early")
    assert c.deadline == datetime(2026, 9, 15, 17, 0, tzinfo=UTC)
    assert c.published_at == datetime(2026, 4, 1, tzinfo=UTC)
    assert c.budget_max == 12000000
    assert c.currency == "EUR"
    assert "ai" in c.keywords
    assert c.raw["identifier"] == "HORIZON-HLTH-2026-DISEASE-03-15"


def test_fetch_skips_records_missing_required_fields():
    body = {
        "results": [
            {"identifier": "OK-1", "title": "ok", "url": "https://example.org/ok-1"},
            {"identifier": "BAD-1"},  # missing title and url
            {"title": "no id"},
        ]
    }
    adapter = EUPortalAdapter(
        base_url="https://x", transport=_mock_transport(body=body)
    )
    out = list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert [c.external_id for c in out] == ["OK-1"]


def test_fetch_skips_records_with_unsafe_url_scheme():
    """Defense in depth: javascript:/data: URLs would render as click-to-XSS
    in the dashboard template. Adapter must drop them at ingest time."""
    body = {
        "results": [
            {
                "identifier": "OK-1",
                "title": "ok",
                "url": "https://example.org/ok-1",
            },
            {
                "identifier": "XSS-1",
                "title": "click me",
                "url": "javascript:alert(document.cookie)",
            },
            {
                "identifier": "DATA-1",
                "title": "data url",
                "url": "data:text/html,<script>alert(1)</script>",
            },
        ]
    }
    adapter = EUPortalAdapter(
        base_url="https://x", transport=_mock_transport(body=body)
    )
    out = list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert [c.external_id for c in out] == ["OK-1"]


def test_http_error_raises_adapter_error():
    adapter = EUPortalAdapter(
        base_url="https://x", transport=_mock_transport(status=503)
    )
    with pytest.raises(AdapterError):
        list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))


def test_network_failure_raises_adapter_error():
    adapter = EUPortalAdapter(
        base_url="https://x",
        transport=_mock_transport(raise_exc=httpx.ConnectError("boom")),
    )
    with pytest.raises(AdapterError):
        list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
