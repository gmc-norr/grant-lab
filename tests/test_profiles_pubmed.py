import httpx
import pytest

from call_manager.profiles.pubmed import PubMedClient, PubMedError
from call_manager.profiles.seed import Member

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
    transport, calls = _mock(
        [
            ("esearch.fcgi", _FAKE_ESEARCH),
            ("esummary.fcgi", _FAKE_ESUMMARY),
        ]
    )
    client = PubMedClient(transport=transport, sleep_between_members=0)
    pubs = client.fetch_publications([Member(orcid="0000-0001-2345-6789")])
    assert len(pubs) == 2
    assert pubs[0].pmid == "12345678"
    assert pubs[0].title.startswith("AI for digital pathology")
    assert pubs[0].year == 2024
    # Search URL must use the AUID field
    assert "AUID" in calls[0] or "orcid" in calls[0].lower()


def test_fetch_publications_by_name_affiliation_fallback():
    transport, calls = _mock(
        [
            ("esearch.fcgi", _FAKE_ESEARCH),
            ("esummary.fcgi", _FAKE_ESUMMARY),
        ]
    )
    client = PubMedClient(transport=transport, sleep_between_members=0)
    pubs = client.fetch_publications([Member(name="Smith J", affiliation="Karolinska Institutet")])
    assert len(pubs) == 2
    # Search URL must use the [au] and [ad] fields
    assert "[au]" in calls[0].lower() or "%5bau%5d" in calls[0].lower()
    assert "[ad]" in calls[0].lower() or "%5bad%5d" in calls[0].lower()


def test_empty_result_for_one_member_does_not_fail_others():
    empty = {"esearchresult": {"count": "0", "idlist": []}}
    transport, _ = _mock(
        [
            ("esearch.fcgi", _FAKE_ESEARCH),  # first member → hits
            ("esearch.fcgi", empty),  # second member → empty
            ("esummary.fcgi", _FAKE_ESUMMARY),
        ]
    )
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


def test_query_for_sanitizes_entrez_injection():
    """Injection defense: a member name/affiliation containing field tags or
    boolean operators must not change the structure of the Entrez term — the
    values are stripped of metacharacters and matched as quoted phrases."""
    from call_manager.profiles.pubmed import _query_for

    member = Member(
        name='Smith J"[au] OR 1[uid] OR "',
        affiliation="Karolinska [ad] AND hacked",
    )
    q = _query_for(member)
    # The only field tags present are the two we intentionally emit.
    assert q.count("[au]") == 1
    assert q.count("[ad]") == 1
    # No injected uid tag, no stray quotes that would break out of our phrase.
    assert "[uid]" not in q
    # Exactly one structural AND (ours, joining au + ad) — the injected
    # boolean operators were neutralized.
    assert q.count(" AND ") == 1
    assert " OR " not in q


def test_query_for_orcid_unaffected():
    from call_manager.profiles.pubmed import _query_for

    assert _query_for(Member(orcid="0000-0001-2345-6789")) == "0000-0001-2345-6789[AUID]"


def test_http_error_raises_pubmed_error():
    def handler(request):
        return httpx.Response(503)

    client = PubMedClient(transport=httpx.MockTransport(handler), sleep_between_members=0)
    with pytest.raises(PubMedError):
        client.fetch_publications([Member(orcid="0000-0001-2345-6789")])


def test_esearch_retries_once_on_429():
    """E-utilities 429 (rate-limited) is retried once after a short backoff
    rather than failing the whole fetch. The second attempt succeeds."""
    attempts = {"n": 0}

    def handler(request):
        url = str(request.url)
        if "esearch.fcgi" in url:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(429, json={})
            return httpx.Response(200, json=_FAKE_ESEARCH)
        return httpx.Response(200, json=_FAKE_ESUMMARY)

    client = PubMedClient(
        transport=httpx.MockTransport(handler),
        sleep_between_members=0,
        retry_backoff=0,
    )
    pubs = client.fetch_publications([Member(orcid="0000-0001-2345-6789")])
    assert attempts["n"] == 2  # first 429, then retried successfully
    assert len(pubs) == 2


def test_esearch_retries_once_on_503_then_gives_up():
    """A persistent 503 (both attempts fail) still raises PubMedError after the
    single retry is exhausted."""
    attempts = {"n": 0}

    def handler(request):
        if "esearch.fcgi" in str(request.url):
            attempts["n"] += 1
        return httpx.Response(503)

    client = PubMedClient(
        transport=httpx.MockTransport(handler),
        sleep_between_members=0,
        retry_backoff=0,
    )
    with pytest.raises(PubMedError):
        client.fetch_publications([Member(orcid="0000-0001-2345-6789")])
    assert attempts["n"] == 2  # initial + one retry


def test_api_key_and_tool_email_included_in_request():
    """When api_key/tool/email are configured they ride along on the
    E-utilities query params (NCBI's documented identification + higher rate
    limit)."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if "esearch.fcgi" in str(request.url):
            return httpx.Response(200, json=_FAKE_ESEARCH)
        return httpx.Response(200, json=_FAKE_ESUMMARY)

    client = PubMedClient(
        transport=httpx.MockTransport(handler),
        sleep_between_members=0,
        api_key="SECRET-KEY",
        tool="call-manager",
        email="ops@ki.se",
    )
    client.fetch_publications([Member(orcid="0000-0001-2345-6789")])
    assert any("api_key=SECRET-KEY" in u for u in seen)
    assert any("tool=call-manager" in u for u in seen)
    assert any("email=ops%40ki.se" in u or "email=ops@ki.se" in u for u in seen)


def test_member_rejects_malformed_orcid():
    with pytest.raises(ValueError, match="invalid ORCID"):
        Member(orcid="not-an-orcid")


def test_member_accepts_x_check_digit_orcid():
    # Valid ORCIDs can end in a literal 'X' (checksum).
    m = Member(orcid="0000-0002-1825-009X")
    assert m.orcid == "0000-0002-1825-009X"
