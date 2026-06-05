from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from call_manager.sources.types import AdapterError
from call_manager.sources.vinnova import VinnovaAdapter

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

_ROUND_OPEN = [
    {
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
        "Publik": 1,
        "Webbsida": 1,
        "Extern": 1,
    }
]

_ROUND_CLOSED = [
    {
        "Diarienummer": "ROUND-CLOSED",
        "DiarienummerUtlysning": "2009-02335",
        "Titel": "Closed round",
        "Beskrivning": "...",
        "Oppningsdatum": "2020-01-01T00:00:00",
        "Stangningsdatum": "2020-06-30T23:59:00",
        "LankLista": [{"Beskrivning": "x", "URL": "https://www.vinnova.se/e/closed"}],
        "Publik": 1,
        "Webbsida": 1,
    }
]

_ROUND_NONPUBLIC = [
    {
        "Diarienummer": "ROUND-NONPUBLIC",
        "DiarienummerUtlysning": "2009-02335",
        "Titel": "Non-public round",
        "Beskrivning": "...",
        "Oppningsdatum": "2026-04-01T00:00:00",
        "Stangningsdatum": "2099-12-31T14:00:00",
        "LankLista": [{"Beskrivning": "x", "URL": "https://www.vinnova.se/e/nonpub"}],
        "Publik": 0,
        "Webbsida": 1,
    }
]


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
            {
                "Diarienummer": "P",
                "Titel": "T",
                "AnsokningsomgangDnrLista": [{"DiarienummerAnsokningsomgang": "BAD-URL"}],
            },
        ],
        "/api/ansokningsomgangar/BAD-URL": [
            {
                "Diarienummer": "BAD-URL",
                "Titel": "x",
                "Beskrivning": "x",
                "Oppningsdatum": "2026-04-01T00:00:00",
                "Stangningsdatum": "2099-12-31T14:00:00",
                "LankLista": [{"URL": "javascript:alert(1)"}],
                "Publik": 1,
                "Webbsida": 1,
            }
        ],
    }
    out = list(
        _make(transport=_mock_transport(routes)).fetch_new_calls(
            since=datetime(2026, 1, 1, tzinfo=UTC)
        )
    )
    assert out == []


def test_fetch_skips_round_with_no_links():
    routes = {
        "/api/utlysningar/2026-01-01": [
            {
                "Diarienummer": "P",
                "Titel": "T",
                "AnsokningsomgangDnrLista": [{"DiarienummerAnsokningsomgang": "NOLINKS"}],
            },
        ],
        "/api/ansokningsomgangar/NOLINKS": [
            {
                "Diarienummer": "NOLINKS",
                "Titel": "x",
                "Beskrivning": "x",
                "Oppningsdatum": "2026-04-01T00:00:00",
                "Stangningsdatum": "2099-12-31T14:00:00",
                "LankLista": [],
                "Publik": 1,
                "Webbsida": 1,
            }
        ],
    }
    out = list(
        _make(transport=_mock_transport(routes)).fetch_new_calls(
            since=datetime(2026, 1, 1, tzinfo=UTC)
        )
    )
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


def test_single_flaky_round_does_not_abort_batch():
    """A 500 on ONE round must not discard the other rounds' calls. The
    flaky round is logged and skipped; partial progress is yielded so the
    idempotent upsert can fill the gap next run."""
    rounds = {
        "/api/ansokningsomgangar/R1": [
            {
                "Diarienummer": "R1",
                "Titel": "Round one",
                "Beskrivning": "x",
                "Oppningsdatum": "2026-04-01T00:00:00",
                "Stangningsdatum": "2099-12-31T14:00:00",
                "LankLista": [{"URL": "https://www.vinnova.se/e/r1"}],
                "Publik": 1,
                "Webbsida": 1,
            }
        ],
        # R2 returns a 500 — handled below.
        "/api/ansokningsomgangar/R3": [
            {
                "Diarienummer": "R3",
                "Titel": "Round three",
                "Beskrivning": "x",
                "Oppningsdatum": "2026-04-01T00:00:00",
                "Stangningsdatum": "2099-12-31T14:00:00",
                "LankLista": [{"URL": "https://www.vinnova.se/e/r3"}],
                "Publik": 1,
                "Webbsida": 1,
            }
        ],
    }
    programmes = [
        {
            "Diarienummer": "P",
            "Titel": "T",
            "AnsokningsomgangDnrLista": [
                {"DiarienummerAnsokningsomgang": "R1"},
                {"DiarienummerAnsokningsomgang": "R2"},
                {"DiarienummerAnsokningsomgang": "R3"},
            ],
        }
    ]

    def handler(request):
        path = str(request.url.path)
        if "/api/utlysningar/" in path:
            return httpx.Response(200, json=programmes)
        if "R2" in path:
            return httpx.Response(500)
        for needle, body in rounds.items():
            if needle in path:
                return httpx.Response(200, json=body)
        raise AssertionError(f"unexpected URL: {request.url}")

    adapter = VinnovaAdapter(
        base_url="https://data.vinnova.se",
        transport=httpx.MockTransport(handler),
        sleep_between_rounds=0,
    )
    out = list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert [c.external_id for c in out] == ["R1", "R3"]


def test_many_consecutive_round_failures_abort():
    """If rounds keep failing back-to-back past the threshold, abort with an
    AdapterError — that's a systemic outage worth alerting on, not a single
    flaky round."""
    n_rounds = 10
    programmes = [
        {
            "Diarienummer": "P",
            "Titel": "T",
            "AnsokningsomgangDnrLista": [
                {"DiarienummerAnsokningsomgang": f"R{i}"} for i in range(n_rounds)
            ],
        }
    ]

    def handler(request):
        path = str(request.url.path)
        if "/api/utlysningar/" in path:
            return httpx.Response(200, json=programmes)
        return httpx.Response(500)

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


def test_summary_falls_back_to_just_beskrivning_when_no_webtext():
    """Round with only the short Beskrivning should still produce a summary."""
    routes = {
        "/api/utlysningar/2026-01-01": [
            {
                "Diarienummer": "P",
                "Titel": "T",
                "AnsokningsomgangDnrLista": [{"DiarienummerAnsokningsomgang": "PLAIN"}],
            },
        ],
        "/api/ansokningsomgangar/PLAIN": [
            {
                "Diarienummer": "PLAIN",
                "Titel": "Title",
                "Beskrivning": "Just a tagline.",
                "Oppningsdatum": "2026-04-01T00:00:00",
                "Stangningsdatum": "2099-12-31T14:00:00",
                "LankLista": [{"URL": "https://www.vinnova.se/e/plain"}],
                "Publik": 1,
                "Webbsida": 1,
            }
        ],
    }
    out = list(
        _make(transport=_mock_transport(routes)).fetch_new_calls(
            since=datetime(2026, 1, 1, tzinfo=UTC)
        )
    )
    assert out[0].summary == "Just a tagline."


def test_vinnova_adapter_splits_summary_and_full_text():
    """After Plan F: Beskrivning -> summary; WebTextLista[*].TextSv -> full_text."""
    routes = {
        "/api/utlysningar/2026-01-01": [
            {
                "Diarienummer": "P",
                "Titel": "T",
                "AnsokningsomgangDnrLista": [{"DiarienummerAnsokningsomgang": "TEST-123"}],
            },
        ],
        "/api/ansokningsomgangar/TEST-123": [
            {
                "Diarienummer": "TEST-123",
                "Titel": "Test programme",
                "Beskrivning": "A brief description.",
                "BeskrivningEngelska": "A brief description in English.",
                "WebTextLista": [
                    {"TextSv": "First substantive paragraph about the call."},
                    {"TextSv": "Second paragraph with more detail and eligibility."},
                ],
                "Stangningsdatum": "2099-12-31T14:00:00",
                "Oppningsdatum": "2026-04-01T00:00:00",
                "Publik": 1,
                "Webbsida": 1,
                "LankLista": [{"URL": "https://vinnova.se/test"}],
            }
        ],
    }
    out = list(
        _make(transport=_mock_transport(routes)).fetch_new_calls(
            since=datetime(2026, 1, 1, tzinfo=UTC)
        )
    )
    assert len(out) == 1
    nc = out[0]

    assert nc.summary is not None
    assert "A brief description." in nc.summary
    # Body lives in full_text now, not summary.
    assert "First substantive paragraph" not in nc.summary
    # English blurb still rides along in summary (distinct from Beskrivning).
    assert "A brief description in English." in nc.summary

    assert nc.full_text is not None
    assert "First substantive paragraph about the call." in nc.full_text
    assert "Second paragraph with more detail" in nc.full_text


def test_midnight_deadline_today_is_treated_as_end_of_day():
    """A date-only Stangningsdatum (midnight Stockholm) on the call's CLOSING
    day must NOT mark the call already-closed. Vinnova emits date-only
    deadlines as 00:00; treating that literally drops calls on their last day.
    We bump a midnight deadline to 23:59:59 so the call survives its final
    day."""
    # Today's Stockholm date at midnight — without the end-of-day bump this
    # parses to a deadline already in the past (00:00 < now), wrongly closing.
    today_sthlm = datetime.now(_STOCKHOLM).date()
    midnight_today = f"{today_sthlm.isoformat()}T00:00:00"
    routes = {
        "/api/utlysningar/2026-01-01": [
            {
                "Diarienummer": "P",
                "Titel": "T",
                "AnsokningsomgangDnrLista": [{"DiarienummerAnsokningsomgang": "CLOSES-TODAY"}],
            },
        ],
        "/api/ansokningsomgangar/CLOSES-TODAY": [
            {
                "Diarienummer": "CLOSES-TODAY",
                "Titel": "Closes today",
                "Beskrivning": "x",
                "Oppningsdatum": "2026-01-01T00:00:00",
                "Stangningsdatum": midnight_today,
                "LankLista": [{"URL": "https://www.vinnova.se/e/closes-today"}],
                "Publik": 1,
                "Webbsida": 1,
            }
        ],
    }
    out = list(
        _make(transport=_mock_transport(routes)).fetch_new_calls(
            since=datetime(2026, 1, 1, tzinfo=UTC)
        )
    )
    assert [c.external_id for c in out] == ["CLOSES-TODAY"]
    # Deadline was bumped to end-of-day, so it's still in the future.
    assert out[0].deadline > datetime.now(UTC)


def test_explicit_time_deadline_not_bumped():
    """A deadline with an explicit non-midnight time is left exactly as-is —
    the end-of-day bump only applies to bare-date (midnight) values."""
    out = list(_make().fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    # _ROUND_OPEN has Stangningsdatum 2099-12-31T14:00:00 (explicit time).
    assert out[0].deadline == datetime(2099, 12, 31, 13, 0, tzinfo=UTC)


def test_vinnova_adapter_full_text_none_when_no_webtextlista():
    """If the source provides no WebTextLista, full_text is None."""
    routes = {
        "/api/utlysningar/2026-01-01": [
            {
                "Diarienummer": "P",
                "Titel": "T",
                "AnsokningsomgangDnrLista": [{"DiarienummerAnsokningsomgang": "TEST-456"}],
            },
        ],
        "/api/ansokningsomgangar/TEST-456": [
            {
                "Diarienummer": "TEST-456",
                "Titel": "Test programme",
                "Beskrivning": "Just a description.",
                "WebTextLista": [],
                "Stangningsdatum": "2099-12-31T14:00:00",
                "Oppningsdatum": "2026-04-01T00:00:00",
                "Publik": 1,
                "Webbsida": 1,
                "LankLista": [{"URL": "https://vinnova.se/test"}],
            }
        ],
    }
    out = list(
        _make(transport=_mock_transport(routes)).fetch_new_calls(
            since=datetime(2026, 1, 1, tzinfo=UTC)
        )
    )
    assert len(out) == 1
    nc = out[0]
    assert nc.summary == "Just a description."
    assert nc.full_text is None
