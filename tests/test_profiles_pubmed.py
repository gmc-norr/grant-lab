import httpx
import pytest

from call_monitor.profiles.pubmed import PubMedClient, PubMedError
from call_monitor.profiles.seed import Member

# Representative esearch response: returns 2 PMIDs
_FAKE_ESEARCH = {
    "esearchresult": {
        "count": "2",
        "retmax": "2",
        "retstart": "0",
        "idlist": ["12345678", "23456789"],
    }
}

# esummary returns one record per PMID
_FAKE_ESUMMARY = {
    "result": {
        "uids": ["12345678", "23456789"],
        "12345678": {
            "uid": "12345678",
            "title": "AI for digital pathology: a survey",
            "pubdate": "2024 Jan",
            "authors": [{"name": "Smith J"}, {"name": "Doe A"}],
        },
        "23456789": {
            "uid": "23456789",
            "title": "Federated learning on radiology images",
            "pubdate": "2025 Jun",
            "authors": [{"name": "Smith J"}],
        },
    }
}


def _mock(responses):
    """`responses` is a list of (url-substring, body) pairs consumed in
    order — each call pops the first matching entry. Raises on unexpected
    URL."""

    calls = []
    remaining = list(responses)

    def handler(request):
        url = str(request.url)
        calls.append(url)
        for i, (needle, body) in enumerate(remaining):
            if needle in url:
                remaining.pop(i)
                return httpx.Response(200, json=body)
        raise AssertionError(f"unexpected URL: {url}")

    return httpx.MockTransport(handler), calls


def test_fetch_publications_by_orcid():
    transport, calls = _mock([
        ("esearch.fcgi", _FAKE_ESEARCH),
        ("esummary.fcgi", _FAKE_ESUMMARY),
    ])
    client = PubMedClient(transport=transport, sleep_between_members=0)
    pubs = client.fetch_publications(
        [Member(orcid="0000-0001-2345-6789")]
    )
    assert len(pubs) == 2
    assert pubs[0].pmid == "12345678"
    assert pubs[0].title.startswith("AI for digital pathology")
    assert pubs[0].year == 2024
    # Search URL must use the AUID field
    assert "AUID" in calls[0] or "orcid" in calls[0].lower()


def test_fetch_publications_by_name_affiliation_fallback():
    transport, calls = _mock([
        ("esearch.fcgi", _FAKE_ESEARCH),
        ("esummary.fcgi", _FAKE_ESUMMARY),
    ])
    client = PubMedClient(transport=transport, sleep_between_members=0)
    pubs = client.fetch_publications(
        [Member(name="Smith J", affiliation="Karolinska Institutet")]
    )
    assert len(pubs) == 2
    # Search URL must use the [au] and [ad] fields
    assert "[au]" in calls[0].lower() or "%5bau%5d" in calls[0].lower()
    assert "[ad]" in calls[0].lower() or "%5bad%5d" in calls[0].lower()


def test_empty_result_for_one_member_does_not_fail_others():
    empty = {"esearchresult": {"count": "0", "idlist": []}}
    transport, _ = _mock([
        ("esearch.fcgi", _FAKE_ESEARCH),  # first member → hits
        ("esearch.fcgi", empty),           # second member → empty
        ("esummary.fcgi", _FAKE_ESUMMARY),
    ])
    # This mock ordering is approximate; real implementation just needs
    # to handle the 0-idlist case gracefully.
    client = PubMedClient(transport=transport, sleep_between_members=0)
    pubs = client.fetch_publications(
        [
            Member(orcid="0000-0001-2345-6789"),
            Member(orcid="0000-0000-0000-0000"),
        ]
    )
    # At minimum the first member's hits are present; the empty member
    # is logged and skipped — not an error.
    assert any(p.pmid == "12345678" for p in pubs)


def test_http_error_raises_pubmed_error():
    def handler(request):
        return httpx.Response(503)
    client = PubMedClient(
        transport=httpx.MockTransport(handler), sleep_between_members=0
    )
    with pytest.raises(PubMedError):
        client.fetch_publications([Member(orcid="0000-0001-2345-6789")])


def test_member_rejects_malformed_orcid():
    with pytest.raises(ValueError, match="invalid ORCID"):
        Member(orcid="not-an-orcid")


def test_member_accepts_x_check_digit_orcid():
    # Valid ORCIDs can end in a literal 'X' (checksum).
    m = Member(orcid="0000-0002-1825-009X")
    assert m.orcid == "0000-0002-1825-009X"
