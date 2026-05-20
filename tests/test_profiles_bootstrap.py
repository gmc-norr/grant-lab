import json

import httpx

from call_manager.profiles.bootstrap import bootstrap_profile
from call_manager.profiles.pubmed import PubMedClient
from call_manager.profiles.seed import Member, SeedInput
from call_manager.ranking.ollama_client import OllamaClient

_ESEARCH = {"esearchresult": {"count": "1", "idlist": ["111"]}}
_ESUMMARY = {
    "result": {
        "uids": ["111"],
        "111": {
            "uid": "111",
            "title": "Digital pathology AI",
            "pubdate": "2024",
            "authors": [{"name": "Smith J"}],
        },
    },
}
_DRAFT_JSON = json.dumps(
    {
        "description": "Research group on AI for digital pathology.",
        "areas": ["digital pathology"],
        "budget_min": 100000,
        "budget_max": 5000000,
        "must_include": ["pathology"],
        "hard_exclusions": [],
    }
)


def _pubmed_mock():
    def handler(request):
        url = str(request.url)
        if "esearch" in url:
            return httpx.Response(200, json=_ESEARCH)
        if "esummary" in url:
            return httpx.Response(200, json=_ESUMMARY)
        raise AssertionError(f"unexpected: {url}")

    return httpx.MockTransport(handler)


def _ollama_mock(content: str = _DRAFT_JSON):
    body = {
        "model": "gemma4:e4b-q8",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
    }

    def handler(request):
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def test_bootstrap_happy_path():
    pubmed = PubMedClient(transport=_pubmed_mock(), sleep_between_members=0)
    ollama = OllamaClient(base_url="http://x", model="gemma4:e4b-q8", transport=_ollama_mock())
    seed = SeedInput(
        name="Pathology AI",
        kind="group",
        members=[Member(orcid="0000-0001-2345-6789")],
        pasted_grants="",
    )
    draft = bootstrap_profile(seed=seed, pubmed=pubmed, ollama=ollama)
    assert draft.description.startswith("Research group")
    assert draft.structured["areas"] == ["digital pathology"]
    assert draft.publications_fetched == 1
    assert draft.publications_skipped == 0


def test_bootstrap_with_no_publications_still_drafts_from_grants():
    empty_pubmed = PubMedClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"esearchresult": {"idlist": []}})
        ),
        sleep_between_members=0,
    )
    ollama = OllamaClient(base_url="http://x", model="gemma4:e4b-q8", transport=_ollama_mock())
    seed = SeedInput(
        name="Pathology AI",
        kind="group",
        members=[Member(orcid="0000-0001-2345-6789")],
        pasted_grants="Past grant: digital pathology pilot 2023",
    )
    draft = bootstrap_profile(seed=seed, pubmed=empty_pubmed, ollama=ollama)
    assert draft.publications_fetched == 0
    assert draft.publications_skipped == 1
    # Description still produced (from pasted grants)
    assert draft.description


def test_bootstrap_pubmed_failure_falls_back_to_grants_only():
    """PubMedError during bootstrap should NOT abort — fall back to the
    pasted-grants-only draft so the user at least gets a starting point."""
    broken_pubmed = PubMedClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503)),
        sleep_between_members=0,
    )
    ollama = OllamaClient(base_url="http://x", model="gemma4:e4b-q8", transport=_ollama_mock())
    seed = SeedInput(
        name="Pathology AI",
        kind="group",
        members=[Member(orcid="0000-0001-2345-6789")],
        pasted_grants="Past grant: digital pathology pilot 2023",
    )
    draft = bootstrap_profile(seed=seed, pubmed=broken_pubmed, ollama=ollama)
    assert draft.publications_fetched == 0
    assert draft.description  # LLM still produces one from grants


def test_bootstrap_malformed_llm_envelope_does_not_throw():
    """If the LLM returns a garbage envelope shape (e.g. choices[0].message
    is a list instead of a dict), bootstrap must still return a DraftProfile."""
    empty_pubmed = PubMedClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"esearchresult": {"idlist": []}})
        ),
        sleep_between_members=0,
    )
    bad_envelope = {
        "choices": [{"message": ["not", "a", "dict"]}],
    }
    ollama = OllamaClient(
        base_url="http://x",
        model="gemma4:e4b-q8",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=bad_envelope)),
    )
    seed = SeedInput(
        name="X", kind="group", members=[Member(orcid="0000-0001-2345-6789")], pasted_grants=""
    )
    draft = bootstrap_profile(seed=seed, pubmed=empty_pubmed, ollama=ollama)
    # Doesn't throw, returns empty-ish draft
    assert draft.description == ""
    assert draft.structured["areas"] == []
