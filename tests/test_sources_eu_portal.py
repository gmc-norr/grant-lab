from datetime import UTC, datetime

import httpx
import pytest

from call_manager.sources.eu_portal import EUPortalAdapter
from call_manager.sources.types import AdapterError

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


def _mock_html_transport(status: int = 200, html: str = ""):
    """Mock transport for the topic-detail page scrape (full_text).

    Default returns a 404 so existing tests stay hermetic without caring
    about full_text. Pass html=... to assert extraction.
    """

    def handler(request):
        return httpx.Response(status, text=html)

    return httpx.MockTransport(handler)


def test_fetch_yields_normalized_calls():
    adapter = EUPortalAdapter(
        base_url="https://ec.europa.eu",
        transport=_mock_transport(),
        extraction_transport=_mock_html_transport(status=404),
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
        base_url="https://x",
        transport=_mock_transport(body=body),
        extraction_transport=_mock_html_transport(status=404),
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
        base_url="https://x",
        transport=_mock_transport(body=body),
        extraction_transport=_mock_html_transport(status=404),
    )
    out = list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert [c.external_id for c in out] == ["OK-1"]


def test_http_error_raises_adapter_error():
    adapter = EUPortalAdapter(base_url="https://x", transport=_mock_transport(status=503))
    with pytest.raises(AdapterError):
        list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))


def test_network_failure_raises_adapter_error():
    adapter = EUPortalAdapter(
        base_url="https://x",
        transport=_mock_transport(raise_exc=httpx.ConnectError("boom")),
    )
    with pytest.raises(AdapterError):
        list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))


def test_eu_portal_adapter_populates_full_text():
    """Plan F: the topic-detail HTML page is scraped for the call body and
    its main content lands in `full_text`."""
    html = """
    <html>
      <head><title>Topic detail</title></head>
      <body>
        <nav>nav junk we should not see</nav>
        <main>
          <article>
            <h1>AI for early disease detection</h1>
            <p>This topic supports the development of AI-driven methods
            for detecting diseases at an early stage in clinical care.</p>
            <p>Eligible projects must demonstrate translational potential
            and a clear plan for hospital-scale validation.</p>
          </article>
        </main>
        <footer>footer junk we should not see</footer>
      </body>
    </html>
    """
    adapter = EUPortalAdapter(
        base_url="https://ec.europa.eu",
        transport=_mock_transport(),
        extraction_transport=_mock_html_transport(status=200, html=html),
    )
    out = list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert len(out) == 1
    nc = out[0]
    assert nc.full_text is not None
    assert "AI-driven methods" in nc.full_text
    assert "translational potential" in nc.full_text


def test_existing_call_with_full_text_is_not_rescraped():
    """Reliability: an EU call already ingested WITH full_text must NOT be
    re-scraped on every run — that's hundreds of serial blocking HTTP calls.
    The orchestrator passes a `needs_body(identifier)` predicate built from a
    cheap DB query; when it returns False the adapter skips the scrape (the
    extraction transport would raise if hit)."""

    def boom(request):
        raise AssertionError("extraction transport must not be called for an existing call")

    # needs_body returns False for the one identifier in _FAKE_OK_BODY → skip.
    already_have = {"HORIZON-HLTH-2026-DISEASE-03-15"}

    adapter = EUPortalAdapter(
        base_url="https://ec.europa.eu",
        transport=_mock_transport(),
        extraction_transport=httpx.MockTransport(boom),
    )
    out = list(
        adapter.fetch_new_calls(
            since=datetime(2026, 1, 1, tzinfo=UTC),
            needs_body=lambda identifier: identifier not in already_have,
        )
    )
    assert len(out) == 1
    # full_text left None — we did not scrape; upsert coalesce preserves the
    # existing body in the DB.
    assert out[0].full_text is None


def test_new_call_is_scraped_even_with_needs_body():
    """A brand-new call (needs_body returns True) IS scraped for its body."""
    html = "<html><body><main><article><p>Brand new body text here.</p>"
    html += "</article></main></body></html>"
    adapter = EUPortalAdapter(
        base_url="https://ec.europa.eu",
        transport=_mock_transport(),
        extraction_transport=_mock_html_transport(status=200, html=html),
    )
    out = list(
        adapter.fetch_new_calls(
            since=datetime(2026, 1, 1, tzinfo=UTC),
            needs_body=lambda identifier: True,
        )
    )
    assert len(out) == 1
    assert out[0].full_text is not None
    assert "Brand new body text" in out[0].full_text


def test_fetch_paginates_across_multiple_pages():
    """Pagination: the adapter must follow pageNumber until the result set is
    exhausted, not stop after the first page. Before the fix a single GET read
    only page 1 and silently dropped everything beyond it."""

    # Two full pages then a short page. Page size is 100; a full page returns
    # 100 results so the adapter knows to request the next one.
    def _page(items):
        return {"results": items, "totalResults": 205}

    page1 = _page(
        [
            {
                "identifier": f"P1-{i}",
                "title": f"call {i}",
                "url": f"https://ec.europa.eu/topic/{i}",
            }
            for i in range(100)
        ]
    )
    page2 = _page(
        [
            {
                "identifier": f"P2-{i}",
                "title": f"call {i}",
                "url": f"https://ec.europa.eu/topic/2-{i}",
            }
            for i in range(100)
        ]
    )
    page3 = _page(
        [
            {
                "identifier": f"P3-{i}",
                "title": f"call {i}",
                "url": f"https://ec.europa.eu/topic/3-{i}",
            }
            for i in range(5)
        ]
    )
    pages = {1: page1, 2: page2, 3: page3}

    def handler(request):
        # pageNumber is in the query string.
        page_number = int(dict(request.url.params).get("pageNumber", "1"))
        return httpx.Response(200, json=pages.get(page_number, {"results": []}))

    adapter = EUPortalAdapter(
        base_url="https://ec.europa.eu",
        transport=httpx.MockTransport(handler),
        extraction_transport=_mock_html_transport(status=404),
    )
    out = list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert len(out) == 205
    ids = {c.external_id for c in out}
    assert "P1-0" in ids
    assert "P2-99" in ids
    assert "P3-4" in ids


def test_fetch_pagination_stops_on_empty_page():
    """If a page comes back empty, pagination terminates even when
    totalResults claims more remain (defensive against a bad upstream count)."""
    page1 = {
        "results": [
            {"identifier": f"A-{i}", "title": "t", "url": f"https://ec.europa.eu/a/{i}"}
            for i in range(100)
        ],
        "totalResults": 999999,  # lies — there is no page 2
    }

    def handler(request):
        page_number = int(dict(request.url.params).get("pageNumber", "1"))
        if page_number == 1:
            return httpx.Response(200, json=page1)
        return httpx.Response(200, json={"results": [], "totalResults": 999999})

    adapter = EUPortalAdapter(
        base_url="https://ec.europa.eu",
        transport=httpx.MockTransport(handler),
        extraction_transport=_mock_html_transport(status=404),
    )
    out = list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert len(out) == 100


def test_budget_accepts_float_and_coerces_to_int():
    """Codex: budgetMin/budgetTotal serialized as floats (12000000.0) were
    dropped by the old `isinstance(..., int)` check. Accept int|float (not
    bool) and coerce to int."""
    body = {
        "results": [
            {
                "identifier": "BUDGET-FLOAT",
                "title": "t",
                "url": "https://ec.europa.eu/budget-float",
                "budgetMin": 250000.0,
                "budgetTotal": 12000000.5,
            }
        ],
        "totalResults": 1,
    }
    adapter = EUPortalAdapter(
        base_url="https://ec.europa.eu",
        transport=_mock_transport(body=body),
        extraction_transport=_mock_html_transport(status=404),
    )
    out = list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert len(out) == 1
    assert out[0].budget_min == 250000
    assert out[0].budget_max == 12000000


def test_budget_rejects_bool_and_non_numeric():
    """`isinstance(True, int)` is True — a JSON bool must NOT be coerced to a
    budget. Non-numeric strings are also dropped to None."""
    body = {
        "results": [
            {
                "identifier": "BUDGET-BAD",
                "title": "t",
                "url": "https://ec.europa.eu/budget-bad",
                "budgetMin": True,
                "budgetTotal": "lots",
            }
        ],
        "totalResults": 1,
    }
    adapter = EUPortalAdapter(
        base_url="https://ec.europa.eu",
        transport=_mock_transport(body=body),
        extraction_transport=_mock_html_transport(status=404),
    )
    out = list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert out[0].budget_min is None
    assert out[0].budget_max is None


def test_eu_portal_adapter_full_text_none_on_failure():
    """If the topic-detail page returns 404, full_text is None and the
    NormalizedCall is still produced — ranking can fall back to summary."""
    adapter = EUPortalAdapter(
        base_url="https://ec.europa.eu",
        transport=_mock_transport(),
        extraction_transport=_mock_html_transport(status=404),
    )
    out = list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert len(out) == 1
    nc = out[0]
    assert nc.full_text is None
    # The rest of the normalized record is unaffected.
    assert nc.external_id == "HORIZON-HLTH-2026-DISEASE-03-15"
    assert nc.summary is not None
