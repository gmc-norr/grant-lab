"""Source-specific fetch backends. Each parses its API surface,
extracts the actual PDF/XML URL, then delegates to fetch_bytes for
the SSRF-safe download."""

import socket

import pytest

from call_manager.proposal_assist.references.fetcher import (
    FetchError,
    fetch_arxiv,
    fetch_crossref_doi,
    fetch_generic_url,
    fetch_pubmed_pmid,
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


def test_fetch_crossref_resolves_doi_to_publisher_pdf(httpx_mock):
    """Crossref API returns a `link` array; we pick the PDF link and
    fetch it via fetch_bytes (SSRF-safe)."""
    httpx_mock.add_response(
        url="https://api.crossref.org/works/10.1056/NEJMms1411398",
        json={
            "message": {
                "title": ["A study on AI in pathology"],
                "link": [
                    {
                        "URL": "https://www.nejm.org/doi/full/10.1056/NEJMms1411398.pdf",
                        "content-type": "application/pdf",
                    }
                ],
            }
        },
    )
    httpx_mock.add_response(
        url="https://www.nejm.org/doi/full/10.1056/NEJMms1411398.pdf",
        content=b"%PDF-1.4\nfake pdf",
    )
    bytes_, filename, mime, meta = fetch_crossref_doi("10.1056/NEJMms1411398")
    assert bytes_ == b"%PDF-1.4\nfake pdf"
    assert filename.endswith(".pdf")
    assert mime == "application/pdf"
    assert meta["doi"] == "10.1056/NEJMms1411398"
    assert "title" in meta


def test_fetch_crossref_no_pdf_link_raises(httpx_mock):
    """Some DOIs have only HTML links; we don't try to download those
    (the user can upload manually)."""
    httpx_mock.add_response(
        url="https://api.crossref.org/works/10.1234/no-pdf",
        json={
            "message": {
                "title": ["Paywalled paper"],
                "link": [
                    {
                        "URL": "https://example.com/paywall.html",
                        "content-type": "text/html",
                    }
                ],
            }
        },
    )
    with pytest.raises(FetchError, match="PDF"):
        fetch_crossref_doi("10.1234/no-pdf")


def test_fetch_pubmed_returns_pmc_xml_when_open_access(httpx_mock):
    """PMC's E-utilities returns PMC ID for an open-access PMID;
    we fetch the full-text XML."""
    # elink step: PMID → PMC ID
    elink_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi?dbfrom=pubmed&db=pmc&id=12345678"
    )
    elink_xml = (
        '<?xml version="1.0"?>'
        "<eLinkResult><LinkSet><LinkSetDb><Link>"
        "<Id>PMC123456</Id>"
        "</Link></LinkSetDb></LinkSet></eLinkResult>"
    )
    httpx_mock.add_response(url=elink_url, text=elink_xml)
    # efetch step: PMC XML
    efetch_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id=PMC123456&rettype=xml"
    )
    httpx_mock.add_response(
        url=efetch_url,
        text="<article><body><p>full text body</p></body></article>",
    )
    bytes_, filename, mime, meta = fetch_pubmed_pmid("12345678")
    assert b"full text body" in bytes_
    assert mime == "application/xml"
    assert meta["pmid"] == "12345678"
    assert meta["pmc_id"] == "PMC123456"


def test_fetch_pubmed_no_pmc_raises(httpx_mock):
    """Closed-access PMID has no PMC link → fetch fails with a clear
    message (user uploads manually)."""
    elink_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi?dbfrom=pubmed&db=pmc&id=99999999"
    )
    httpx_mock.add_response(
        url=elink_url,
        text='<?xml version="1.0"?><eLinkResult><LinkSet></LinkSet></eLinkResult>',
    )
    with pytest.raises(FetchError, match="PMC"):
        fetch_pubmed_pmid("99999999")


def test_fetch_arxiv_pulls_pdf(httpx_mock):
    """arXiv ID maps to https://arxiv.org/pdf/<id>.pdf"""
    httpx_mock.add_response(
        url="https://arxiv.org/pdf/2401.12345.pdf",
        content=b"%PDF-1.4\narxiv paper",
    )
    bytes_, filename, mime, meta = fetch_arxiv("2401.12345")
    assert bytes_ == b"%PDF-1.4\narxiv paper"
    assert filename == "arxiv-2401.12345.pdf"
    assert mime == "application/pdf"
    assert meta["arxiv_id"] == "2401.12345"


def test_fetch_generic_url_returns_bytes_with_mime_sniff(httpx_mock):
    """Generic URL → just fetch_bytes + Content-Type header."""
    httpx_mock.add_response(
        url="https://example.com/paper.pdf",
        content=b"%PDF-1.4\ngeneric",
        headers={"content-type": "application/pdf"},
    )
    bytes_, filename, mime, meta = fetch_generic_url("https://example.com/paper.pdf")
    assert bytes_ == b"%PDF-1.4\ngeneric"
    assert filename == "paper.pdf"
    assert mime == "application/pdf"
    assert meta["url"] == "https://example.com/paper.pdf"
