from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from call_monitor.sources.types import AdapterError
from call_monitor.sources.vinnova import VinnovaAdapter

_STOCKHOLM = ZoneInfo("Europe/Stockholm")

_PROGRAMMES = [
    {
        "Diarienummer": "2009-02335",
        "Titel": "FFI - Styrelse initierade projekt",
        "AnsokningsomgangDnrLista": [
            {"DiarienummerAnsokningsomgang": "ROUND-OPEN"},
            {"DiarienummerAnsokningsomgang": "ROUND-CLOSED"},
            {"DiarienummerAnsokningsomgang": "ROUND-NONPUBLIC"},
        ],
    }
]

_ROUND_OPEN = [{
    "Diarienummer": "ROUND-OPEN",
    "DiarienummerUtlysning": "2009-02335",
    "Titel": "Open round",
    "Beskrivning": "Lots of detail.",
    "TitelEngelska": "Open round (en)",
    "BeskrivningEngelska": "Lots of detail (en).",
    "Oppningsdatum": "2026-04-01T00:00:00",
    "Stangningsdatum": "2099-12-31T14:00:00",
    "LankLista": [
        {"Beskrivning": "ansök här:", "URL": "https://www.vinnova.se/e/round-open"},
    ],
    "Publik": 1, "Webbsida": 1, "Extern": 1,
}]

_ROUND_CLOSED = [{
    "Diarienummer": "ROUND-CLOSED",
    "DiarienummerUtlysning": "2009-02335",
    "Titel": "Closed round",
    "Beskrivning": "...",
    "Oppningsdatum": "2020-01-01T00:00:00",
    "Stangningsdatum": "2020-06-30T23:59:00",
    "LankLista": [{"Beskrivning": "x", "URL": "https://www.vinnova.se/e/closed"}],
    "Publik": 1, "Webbsida": 1,
}]

_ROUND_NONPUBLIC = [{
    "Diarienummer": "ROUND-NONPUBLIC",
    "DiarienummerUtlysning": "2009-02335",
    "Titel": "Non-public round",
    "Beskrivning": "...",
    "Oppningsdatum": "2026-04-01T00:00:00",
    "Stangningsdatum": "2099-12-31T14:00:00",
    "LankLista": [{"Beskrivning": "x", "URL": "https://www.vinnova.se/e/nonpub"}],
    "Publik": 0, "Webbsida": 1,
}]


def _mock_transport(routes: dict[str, list | dict] | None = None):
    """Map exact path suffix → JSON body."""
    routes = routes or {
        "/api/utlysningar/2026-01-01": _PROGRAMMES,
        "/api/ansokningsomgangar/ROUND-OPEN": _ROUND_OPEN,
        "/api/ansokningsomgangar/ROUND-CLOSED": _ROUND_CLOSED,
        "/api/ansokningsomgangar/ROUND-NONPUBLIC": _ROUND_NONPUBLIC,
    }

    def handler(request):
        assert request.headers.get("Accept") == "application/vnd.vinnova.opendata.2.0+json"
        for needle, body in routes.items():
            if needle in str(request.url.path):
                return httpx.Response(200, json=body)
        raise AssertionError(f"unexpected URL: {request.url}")

    return httpx.MockTransport(handler)


def _make(transport=None):
    return VinnovaAdapter(
        base_url="https://data.vinnova.se",
        transport=transport or _mock_transport(),
        sleep_between_rounds=0,
    )


def test_fetch_returns_only_open_public_rounds():
    out = list(_make().fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    ids = [c.external_id for c in out]
    assert ids == ["ROUND-OPEN"]


def test_open_round_is_normalized_correctly():
    out = list(_make().fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert len(out) == 1
    c = out[0]
    assert c.external_id == "ROUND-OPEN"
    assert c.title == "Open round"
    # _ROUND_OPEN has both Beskrivning and BeskrivningEngelska; _build_summary
    # concatenates them.
    assert c.summary == "Lots of detail.\n\nLots of detail (en)."
    assert c.url == "https://www.vinnova.se/e/round-open"
    # Oppningsdatum 2026-04-01T00:00:00 (Stockholm) -> UTC = 2026-03-31T22:00:00 (CEST -2)
    # ... or 2026-03-31T23:00:00 if CET. April 1 is post-DST in EU, so CEST (+02).
    assert c.published_at is not None and c.published_at.tzinfo is not None
    # Stangningsdatum 2099-12-31T14:00:00 (Stockholm CET +01 in winter) -> UTC 13:00
    assert c.deadline == datetime(2099, 12, 31, 13, 0, tzinfo=UTC)
    # Adapter doesn't populate budget/programme/keywords for v1 (Vinnova API
    # doesn't expose them on the round endpoint).
    assert c.budget_min is None
    assert c.budget_max is None
    assert c.currency is None
    assert c.programme is None
    assert c.keywords == []


def test_fetch_skips_round_with_no_safe_url():
    routes = {
        "/api/utlysningar/2026-01-01": [
            {"Diarienummer": "P", "Titel": "T",
             "AnsokningsomgangDnrLista": [{"DiarienummerAnsokningsomgang": "BAD-URL"}]},
        ],
        "/api/ansokningsomgangar/BAD-URL": [{
            "Diarienummer": "BAD-URL", "Titel": "x", "Beskrivning": "x",
            "Oppningsdatum": "2026-04-01T00:00:00",
            "Stangningsdatum": "2099-12-31T14:00:00",
            "LankLista": [{"URL": "javascript:alert(1)"}],
            "Publik": 1, "Webbsida": 1,
        }],
    }
    out = list(_make(transport=_mock_transport(routes)).fetch_new_calls(
        since=datetime(2026, 1, 1, tzinfo=UTC)
    ))
    assert out == []


def test_fetch_skips_round_with_no_links():
    routes = {
        "/api/utlysningar/2026-01-01": [
            {"Diarienummer": "P", "Titel": "T",
             "AnsokningsomgangDnrLista": [{"DiarienummerAnsokningsomgang": "NOLINKS"}]},
        ],
        "/api/ansokningsomgangar/NOLINKS": [{
            "Diarienummer": "NOLINKS", "Titel": "x", "Beskrivning": "x",
            "Oppningsdatum": "2026-04-01T00:00:00",
            "Stangningsdatum": "2099-12-31T14:00:00",
            "LankLista": [],
            "Publik": 1, "Webbsida": 1,
        }],
    }
    out = list(_make(transport=_mock_transport(routes)).fetch_new_calls(
        since=datetime(2026, 1, 1, tzinfo=UTC)
    ))
    assert out == []


def test_http_error_raises_adapter_error():
    def handler(request):
        return httpx.Response(503)

    adapter = VinnovaAdapter(
        base_url="https://data.vinnova.se",
        transport=httpx.MockTransport(handler),
        sleep_between_rounds=0,
    )
    with pytest.raises(AdapterError):
        list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))


def test_non_list_payload_raises():
    def handler(request):
        return httpx.Response(200, json={"not": "a list"})

    adapter = VinnovaAdapter(
        base_url="https://data.vinnova.se",
        transport=httpx.MockTransport(handler),
        sleep_between_rounds=0,
    )
    with pytest.raises(AdapterError):
        list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))


def test_https_required():
    with pytest.raises(ValueError, match="https"):
        VinnovaAdapter(base_url="http://insecure.example/")


def test_summary_includes_webtextlista_and_english_blurb():
    """The ranker needs more signal than Vinnova's 40-char Beskrivning.
    WebTextLista carries the real prose; BeskrivningEngelska is bonus."""
    routes = {
        "/api/utlysningar/2026-01-01": [
            {"Diarienummer": "P", "Titel": "T",
             "AnsokningsomgangDnrLista": [{"DiarienummerAnsokningsomgang": "RICH"}]},
        ],
        "/api/ansokningsomgangar/RICH": [{
            "Diarienummer": "RICH", "Titel": "Title",
            "Beskrivning": "Short tagline.",
            "BeskrivningEngelska": "Short tagline (en).",
            "WebTextLista": [
                {"TextID": 1, "TextSv": "Detaljerad svensk beskrivning av projektet."},
                {"TextID": 2, "TextSv": "Mer detaljer om finansieringen."},
            ],
            "Oppningsdatum": "2026-04-01T00:00:00",
            "Stangningsdatum": "2099-12-31T14:00:00",
            "LankLista": [{"URL": "https://www.vinnova.se/e/rich"}],
            "Publik": 1, "Webbsida": 1,
        }],
    }
    out = list(_make(transport=_mock_transport(routes)).fetch_new_calls(
        since=datetime(2026, 1, 1, tzinfo=UTC)
    ))
    assert len(out) == 1
    summary = out[0].summary
    assert summary is not None
    assert "Short tagline." in summary
    assert "Detaljerad svensk beskrivning av projektet." in summary
    assert "Mer detaljer om finansieringen." in summary
    assert "Short tagline (en)." in summary


def test_summary_falls_back_to_just_beskrivning_when_no_webtext():
    """Round with only the short Beskrivning should still produce a summary."""
    routes = {
        "/api/utlysningar/2026-01-01": [
            {"Diarienummer": "P", "Titel": "T",
             "AnsokningsomgangDnrLista": [{"DiarienummerAnsokningsomgang": "PLAIN"}]},
        ],
        "/api/ansokningsomgangar/PLAIN": [{
            "Diarienummer": "PLAIN", "Titel": "Title",
            "Beskrivning": "Just a tagline.",
            "Oppningsdatum": "2026-04-01T00:00:00",
            "Stangningsdatum": "2099-12-31T14:00:00",
            "LankLista": [{"URL": "https://www.vinnova.se/e/plain"}],
            "Publik": 1, "Webbsida": 1,
        }],
    }
    out = list(_make(transport=_mock_transport(routes)).fetch_new_calls(
        since=datetime(2026, 1, 1, tzinfo=UTC)
    ))
    assert out[0].summary == "Just a tagline."
