"""Source-specific fetch backends. Each parses its API surface,
extracts the actual PDF/XML URL, then delegates to fetch_bytes for
the SSRF-safe download."""

import pytest

from call_manager.proposal_assist.references.fetcher import (
    FetchError,
    fetch_arxiv,
    fetch_crossref_doi,
    fetch_generic_url,
    fetch_pubmed_pmid,
)

# Every test here hits a mocked hostname through fetch_bytes, which
# validates DNS via socket.getaddrinfo. Apply the shared fixture from
# tests/proposal_assist/conftest.py to all tests in this module.
pytestmark = pytest.mark.usefixtures("fake_public_dns")


def test_fetch_crossref_resolves_doi_to_publisher_pdf(httpx_mock):
    """Crossref API returns a `link` array; we pick the PDF link and
    fetch it via fetch_bytes (SSRF-safe)."""
    httpx_mock.add_response(
        # DOI is percent-encoded (safe="") so '/' becomes %2F.
        url="https://api.crossref.org/works/10.1056%2FNEJMms1411398",
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
        url="https://api.crossref.org/works/10.1234%2Fno-pdf",
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


def test_fetch_pubmed_entity_expansion_is_rejected(httpx_mock):
    """A malicious elink response with an XML entity-expansion (billion
    laughs) payload must be refused by defusedxml, surfaced as a
    FetchError rather than exhausting memory."""
    elink_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi?dbfrom=pubmed&db=pmc&id=42"
    )
    bomb = (
        '<?xml version="1.0"?>'
        "<!DOCTYPE lolz ["
        '<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
        '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">'
        "]>"
        "<eLinkResult><LinkSet><Id>&lol3;</Id></LinkSet></eLinkResult>"
    )
    httpx_mock.add_response(url=elink_url, text=bomb)
    with pytest.raises(FetchError):
        fetch_pubmed_pmid("42")


def test_fetch_crossref_url_encodes_doi(httpx_mock):
    """A DOI containing characters that are significant in a URL path
    (e.g. '#', '?') must be percent-encoded so it can't break out of the
    Crossref works/ path or inject query params."""
    import urllib.parse

    doi = "10.1234/weird#frag?x=1"
    api_url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
    httpx_mock.add_response(
        url=api_url,
        json={
            "message": {
                "title": ["Edge case DOI"],
                "link": [
                    {
                        "URL": "https://example.com/p.pdf",
                        "content-type": "application/pdf",
                    }
                ],
            }
        },
    )
    httpx_mock.add_response(url="https://example.com/p.pdf", content=b"%PDF-1.4\nok")
    bytes_, _filename, mime, meta = fetch_crossref_doi(doi)
    assert bytes_ == b"%PDF-1.4\nok"
    assert mime == "application/pdf"
    assert meta["doi"] == doi


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
