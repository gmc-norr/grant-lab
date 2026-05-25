"""Dispatcher routes an input string to the right backend by regex.
Patterns checked in order:
  ^10\\.\\d{4,9}/ → DOI → Crossref
  ^\\d{6,9}$     → PMID → PubMed
  ^\\d{4}\\.\\d{4,5}(v\\d+)?$ → arXiv ID
  ^https?://     → generic URL
"""

import pytest

from call_manager.proposal_assist.references.dispatcher import (
    detect_source_type,
    dispatch_fetch,
)

# Tests that route to a backend hit mocked hostnames; pure detect_*
# tests are unaffected. Apply the shared fixture from
# tests/proposal_assist/conftest.py to all tests in this module — the
# detect_* tests pay only the cost of one monkeypatch per test, which
# is trivial.
pytestmark = pytest.mark.usefixtures("fake_public_dns")


def test_detect_doi():
    assert detect_source_type("10.1056/NEJMms1411398") == "crossref"
    assert detect_source_type("10.1234/foo.bar") == "crossref"


def test_detect_pmid():
    assert detect_source_type("12345678") == "pubmed"
    assert detect_source_type("9999999") == "pubmed"


def test_detect_arxiv():
    assert detect_source_type("2401.12345") == "arxiv"
    assert detect_source_type("2401.1234") == "arxiv"
    assert detect_source_type("2401.12345v2") == "arxiv"


def test_detect_url():
    assert detect_source_type("https://example.com/paper.pdf") == "web"
    assert detect_source_type("http://example.com/paper.pdf") == "web"


def test_detect_unknown_raises():
    with pytest.raises(ValueError, match="unrecognized input"):
        detect_source_type("just some text")
    with pytest.raises(ValueError, match="unrecognized input"):
        detect_source_type("")


def test_dispatch_routes_doi_to_crossref(httpx_mock):
    """Sanity round-trip: dispatch_fetch with a DOI hits Crossref + PDF."""
    httpx_mock.add_response(
        url="https://api.crossref.org/works/10.1234/test",
        json={
            "message": {
                "title": ["t"],
                "link": [{"URL": "https://example.com/p.pdf", "content-type": "application/pdf"}],
            }
        },
    )
    httpx_mock.add_response(
        url="https://example.com/p.pdf",
        content=b"%PDF-1.4",
    )
    bytes_, _, _, meta = dispatch_fetch("10.1234/test")
    assert meta["source"] == "crossref"
    assert bytes_ == b"%PDF-1.4"
