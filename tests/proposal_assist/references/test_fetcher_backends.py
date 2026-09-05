"""Source-specific fetch backends. Each parses its API surface,
extracts the actual PDF/XML URL, then delegates to fetch_bytes for
the SSRF-safe download."""

import pytest

from grant_lab.proposal_assist.references.fetcher import (
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
    """arXiv ID maps to https://arxiv.org/pdf/<id>.pdf.
    The Atom API is also fetched for metadata (may 404 gracefully)."""
    # Atom metadata endpoint — return a minimal valid Atom document
    httpx_mock.add_response(
        url="https://export.arxiv.org/api/query?id_list=2401.12345",
        text=(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            "  <entry>"
            "    <id>http://arxiv.org/abs/2401.12345v1</id>"
            "    <title>ArXiv Test Paper</title>"
            "    <published>2024-01-01T00:00:00Z</published>"
            "    <author><name>Doe, John</name></author>"
            "  </entry>"
            "</feed>"
        ),
    )
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
    # source must be "web" to match the DB CHECK constraint and dispatcher
    # classification — not "generic" (which was the old, broken value).
    assert meta["source"] == "web"


# ---------------------------------------------------------------------------
# Slice 1B Task 2 — full bibliographic metadata in meta dicts
# ---------------------------------------------------------------------------


def test_crossref_meta_includes_full_message_dict(httpx_mock):
    """fetch_crossref_doi must add meta['message'] = the full Crossref
    message object.  Existing keys (doi, title, source) must survive."""
    message = {
        "title": ["Slice 1B full metadata paper"],
        "author": [{"family": "Smith", "given": "Jane"}],
        "issued": {"date-parts": [[2024, 3, 15]]},
        "container-title": ["Journal of Testing"],
        "link": [
            {
                "URL": "https://example.com/slice1b.pdf",
                "content-type": "application/pdf",
            }
        ],
    }
    httpx_mock.add_response(
        url="https://api.crossref.org/works/10.9999%2Fslice1b",
        json={"message": message},
    )
    httpx_mock.add_response(
        url="https://example.com/slice1b.pdf",
        content=b"%PDF-1.4\nslice1b",
    )
    bytes_, filename, mime, meta = fetch_crossref_doi("10.9999/slice1b")

    # tuple shape unchanged
    assert bytes_ == b"%PDF-1.4\nslice1b"
    assert filename.endswith(".pdf")
    assert mime == "application/pdf"

    # existing keys preserved
    assert meta["doi"] == "10.9999/slice1b"
    assert meta["source"] == "crossref"
    assert "title" in meta

    # new: full message dict
    assert meta["message"] == message
    assert meta["message"]["author"][0]["family"] == "Smith"
    assert meta["message"]["issued"] == {"date-parts": [[2024, 3, 15]]}


def test_crossref_meta_message_absent_does_not_raise(httpx_mock):
    """If the Crossref JSON lacks a 'message' key entirely the function
    must not raise — it should omit meta['message'] and raise only when
    there is no PDF link (which is the real failure mode here)."""
    httpx_mock.add_response(
        url="https://api.crossref.org/works/10.9999%2Fno-message",
        json={},  # no 'message' key at all
    )
    with pytest.raises(FetchError, match="PDF"):
        fetch_crossref_doi("10.9999/no-message")
    # The important assertion: no KeyError / crash before the PDF check.


def test_arxiv_meta_includes_bibliographic_fields(httpx_mock):
    """fetch_arxiv must fetch the Atom API, parse title/authors/year/url
    and embed them in meta.  Existing keys (arxiv_id, source) must survive.
    The PDF bytes are unchanged."""
    atom_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "  <entry>"
        "    <id>http://arxiv.org/abs/2401.99999v1</id>"
        "    <title>Attention Is All You Need (test edition)</title>"
        "    <published>2024-01-20T00:00:00Z</published>"
        "    <author><name>Vaswani, Ashish</name></author>"
        "    <author><name>Shazeer, Noam</name></author>"
        "  </entry>"
        "</feed>"
    )
    httpx_mock.add_response(
        url="https://export.arxiv.org/api/query?id_list=2401.99999",
        text=atom_xml,
    )
    httpx_mock.add_response(
        url="https://arxiv.org/pdf/2401.99999.pdf",
        content=b"%PDF-1.4\naxriv test",
    )
    bytes_, filename, mime, meta = fetch_arxiv("2401.99999")

    # tuple shape unchanged
    assert bytes_ == b"%PDF-1.4\naxriv test"
    assert filename == "arxiv-2401.99999.pdf"
    assert mime == "application/pdf"

    # existing keys preserved
    assert meta["arxiv_id"] == "2401.99999"
    assert meta["source"] == "arxiv"

    # new bibliographic fields
    assert meta["title"] == "Attention Is All You Need (test edition)"
    assert meta["authors"] == ["Vaswani, Ashish", "Shazeer, Noam"]
    assert meta["year"] == 2024
    assert meta["url"] == "http://arxiv.org/abs/2401.99999v1"


def test_arxiv_atom_fetch_failure_does_not_abort_pdf(httpx_mock):
    """If the Atom metadata fetch fails (e.g. HTTP 500) the PDF must still
    be returned and arxiv_id / source must still be present; no exception."""
    httpx_mock.add_response(
        url="https://export.arxiv.org/api/query?id_list=2401.00001",
        status_code=500,
    )
    httpx_mock.add_response(
        url="https://arxiv.org/pdf/2401.00001.pdf",
        content=b"%PDF-1.4\nfallback",
    )
    bytes_, filename, mime, meta = fetch_arxiv("2401.00001")
    assert bytes_ == b"%PDF-1.4\nfallback"
    assert meta["arxiv_id"] == "2401.00001"
    assert meta["source"] == "arxiv"
    # bibliographic fields should be absent (not populated on failure)
    assert "title" not in meta
    assert "authors" not in meta


def test_pubmed_meta_includes_title_and_authors(httpx_mock):
    """fetch_pubmed_pmid must parse the efetch JATS XML to extract the
    article title and author list.  Existing keys (pmid, pmc_id, source)
    must survive unchanged."""
    elink_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi?dbfrom=pubmed&db=pmc&id=11223344"
    )
    elink_xml = (
        '<?xml version="1.0"?>'
        "<eLinkResult><LinkSet><LinkSetDb><Link>"
        "<Id>PMC654321</Id>"
        "</Link></LinkSetDb></LinkSet></eLinkResult>"
    )
    httpx_mock.add_response(url=elink_url, text=elink_xml)

    efetch_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id=PMC654321&rettype=xml"
    )
    efetch_xml = (
        '<?xml version="1.0"?>'
        "<article>"
        "  <front>"
        "    <article-meta>"
        "      <title-group>"
        "        <article-title>JATS Metadata Test Article</article-title>"
        "      </title-group>"
        "      <contrib-group>"
        "        <contrib contrib-type='author'>"
        "          <name>"
        "            <surname>Johnson</surname>"
        "            <given-names>Alice B.</given-names>"
        "          </name>"
        "        </contrib>"
        "        <contrib contrib-type='author'>"
        "          <name>"
        "            <surname>Lee</surname>"
        "            <given-names>Charles</given-names>"
        "          </name>"
        "        </contrib>"
        "      </contrib-group>"
        "    </article-meta>"
        "  </front>"
        "</article>"
    )
    httpx_mock.add_response(url=efetch_url, text=efetch_xml)

    bytes_, filename, mime, meta = fetch_pubmed_pmid("11223344")

    # tuple shape unchanged
    assert b"JATS" in bytes_
    assert mime == "application/xml"

    # existing keys preserved
    assert meta["pmid"] == "11223344"
    assert meta["pmc_id"] == "PMC654321"
    assert meta["source"] == "pubmed"

    # new bibliographic fields
    assert meta["title"] == "JATS Metadata Test Article"
    assert meta["authors"] == [
        {"family": "Johnson", "given": "Alice B."},
        {"family": "Lee", "given": "Charles"},
    ]


def test_pubmed_malformed_xml_still_returns_ids(httpx_mock):
    """If the efetch body is malformed XML the function must NOT raise;
    it must return the tuple with at least pmid, pmc_id, source intact."""
    elink_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi?dbfrom=pubmed&db=pmc&id=55667788"
    )
    elink_xml = (
        '<?xml version="1.0"?>'
        "<eLinkResult><LinkSet><LinkSetDb><Link>"
        "<Id>PMC999888</Id>"
        "</Link></LinkSetDb></LinkSet></eLinkResult>"
    )
    httpx_mock.add_response(url=elink_url, text=elink_xml)

    efetch_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id=PMC999888&rettype=xml"
    )
    httpx_mock.add_response(url=efetch_url, text="<<NOT VALID XML>>")

    bytes_, filename, mime, meta = fetch_pubmed_pmid("55667788")

    # IDs must survive
    assert meta["pmid"] == "55667788"
    assert meta["pmc_id"] == "PMC999888"
    assert meta["source"] == "pubmed"
    # title/authors may be absent — no crash required
    assert "title" not in meta or isinstance(meta.get("title"), str)


def test_pubmed_jats_title_with_inline_markup(httpx_mock):
    """An <article-title> containing inline child elements (e.g. <italic>)
    must produce the FULL concatenated text, not just the text node before
    the first child tag."""
    elink_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi?dbfrom=pubmed&db=pmc&id=77001122"
    )
    elink_xml = (
        '<?xml version="1.0"?>'
        "<eLinkResult><LinkSet><LinkSetDb><Link>"
        "<Id>PMC700100</Id>"
        "</Link></LinkSetDb></LinkSet></eLinkResult>"
    )
    httpx_mock.add_response(url=elink_url, text=elink_xml)

    efetch_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id=PMC700100&rettype=xml"
    )
    efetch_xml = (
        '<?xml version="1.0"?>'
        "<article>"
        "  <front>"
        "    <article-meta>"
        "      <title-group>"
        "        <article-title>"
        "A study of <italic>Homo sapiens</italic> cells"
        "        </article-title>"
        "      </title-group>"
        "    </article-meta>"
        "  </front>"
        "</article>"
    )
    httpx_mock.add_response(url=efetch_url, text=efetch_xml)

    bytes_, filename, mime, meta = fetch_pubmed_pmid("77001122")

    assert meta["title"] == "A study of Homo sapiens cells"


def test_fetch_arxiv_version_suffix_passes_through(httpx_mock):
    """An arxiv_id with a version suffix (e.g. 2401.12345v2) contains only
    alphanumeric characters and dots/hyphens — urllib.parse.quote(safe='')
    must not alter it, so the Atom URL is correct and the PDF is fetched."""
    arxiv_id = "2401.12345v2"
    httpx_mock.add_response(
        url=f"https://export.arxiv.org/api/query?id_list={arxiv_id}",
        status_code=500,  # metadata failure is non-fatal
    )
    httpx_mock.add_response(
        url=f"https://arxiv.org/pdf/{arxiv_id}.pdf",
        content=b"%PDF-1.4\nversioned",
    )
    bytes_, filename, mime, meta = fetch_arxiv(arxiv_id)
    assert bytes_ == b"%PDF-1.4\nversioned"
    assert meta["arxiv_id"] == arxiv_id


def test_arxiv_atom_no_entry_returns_pdf_without_meta(httpx_mock):
    """Atom feed with no <entry> element: PDF is still returned, arxiv_id is
    present, but bibliographic fields (title, authors, year, url) are absent."""
    arxiv_id = "2401.55555"
    httpx_mock.add_response(
        url=f"https://export.arxiv.org/api/query?id_list={arxiv_id}",
        text='<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>',
    )
    httpx_mock.add_response(
        url=f"https://arxiv.org/pdf/{arxiv_id}.pdf",
        content=b"%PDF-1.4\nno-entry",
    )
    bytes_, filename, mime, meta = fetch_arxiv(arxiv_id)
    assert bytes_ == b"%PDF-1.4\nno-entry"
    assert meta["arxiv_id"] == arxiv_id
    assert "title" not in meta


def test_pubmed_single_author(httpx_mock):
    """JATS XML with exactly one author: authors list contains one entry
    with the correct family/given shape; title is parsed correctly."""
    elink_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi?dbfrom=pubmed&db=pmc&id=33445566"
    )
    elink_xml = (
        '<?xml version="1.0"?>'
        "<eLinkResult><LinkSet><LinkSetDb><Link>"
        "<Id>PMC810101</Id>"
        "</Link></LinkSetDb></LinkSet></eLinkResult>"
    )
    httpx_mock.add_response(url=elink_url, text=elink_xml)

    efetch_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id=PMC810101&rettype=xml"
    )
    efetch_xml = (
        '<?xml version="1.0"?>'
        "<article>"
        "  <front>"
        "    <article-meta>"
        "      <title-group>"
        "        <article-title>Single Author Study</article-title>"
        "      </title-group>"
        "      <contrib-group>"
        "        <contrib contrib-type='author'>"
        "          <name>"
        "            <surname>Roe</surname>"
        "            <given-names>J</given-names>"
        "          </name>"
        "        </contrib>"
        "      </contrib-group>"
        "    </article-meta>"
        "  </front>"
        "</article>"
    )
    httpx_mock.add_response(url=efetch_url, text=efetch_xml)

    bytes_, filename, mime, meta = fetch_pubmed_pmid("33445566")

    assert meta["title"] == "Single Author Study"
    assert meta["authors"] == [{"family": "Roe", "given": "J"}]
