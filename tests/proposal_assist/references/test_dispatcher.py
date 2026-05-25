"""Dispatcher routes an input string to the right backend by regex.
Patterns checked in order:
  ^10\\.\\d{4,9}/ → DOI → Crossref
  ^\\d{6,9}$     → PMID → PubMed
  ^\\d{4}\\.\\d{4,5}(v\\d+)?$ → arXiv ID
  ^https?://     → generic URL
"""

import socket

import pytest

from call_manager.proposal_assist.references.dispatcher import (
    detect_source_type,
    dispatch_fetch,
)


@pytest.fixture(autouse=True)
def fake_public_dns(monkeypatch):
    """pytest-httpx mocks at the httpx transport layer, NOT the socket
    layer, so our SSRF resolver still attempts real DNS for hosts referenced
    by tests. Pretend the known mocked hostnames resolve to a real public
    IP (8.8.8.8). Other hostnames fall through to the real resolver.

    We use 8.8.8.8 rather than a TEST-NET address because Python's
    ipaddress.is_private treats the documentation ranges as private,
    which would defeat the SSRF check."""
    real_getaddrinfo = socket.getaddrinfo

    _fake_hosts = {
        "api.crossref.org",
        "www.nejm.org",
        "eutils.ncbi.nlm.nih.gov",
        "arxiv.org",
        "example.com",
    }

    def fake_getaddrinfo(host, *args, **kwargs):
        if host in _fake_hosts or host.endswith(".example.com"):
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("8.8.8.8", 0),
                )
            ]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return fake_getaddrinfo


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
