from call_manager.profiles.bootstrap import bootstrap_profile
from call_manager.profiles.pubmed import PubMedError
from call_manager.profiles.seed import Member, Publication, SeedInput
from call_manager.ranking.ollama_client import LLMError

_FAKE_PUB = Publication(pmid="1", title="Fake title", year=2024)


class _Pub:
    def __init__(self, n):
        self._n = n

    def fetch_publications(self, members):
        return [_FAKE_PUB] * self._n


class _PubFail:
    def fetch_publications(self, members):
        raise PubMedError("down")


def _ollama(content):
    class _O:
        def chat(self, *, messages):
            return {"choices": [{"message": {"content": content}}]}

    return _O()


class _LLMFail:
    def chat(self, *, messages):
        raise LLMError("model down")


def _seed(members):
    return SeedInput(name="G", kind="organizational_unit", members=members, mission_text="x")


_GOOD = '{"description": "A solid two-paragraph draft.", "structured": {}}'


def test_llm_success_with_pubs_is_ok():
    d = bootstrap_profile(
        seed=_seed([Member(orcid="0000-0002-1825-0097")]), pubmed=_Pub(3), ollama=_ollama(_GOOD)
    )
    assert d.llm_succeeded is True
    assert d.pubmed_status == "ok"
    assert d.publications_fetched == 3


def test_pubmed_empty_unavailable_not_queried():
    m = [Member(orcid="0000-0002-1825-0097")]
    d_empty = bootstrap_profile(seed=_seed(m), pubmed=_Pub(0), ollama=_ollama(_GOOD))
    assert d_empty.pubmed_status == "empty"
    d_unavail = bootstrap_profile(seed=_seed(m), pubmed=_PubFail(), ollama=_ollama(_GOOD))
    assert d_unavail.pubmed_status == "unavailable"
    d_noq = bootstrap_profile(seed=_seed([]), pubmed=_Pub(0), ollama=_ollama(_GOOD))
    assert d_noq.pubmed_status == "not_queried"


def test_llm_exception_is_failure():
    d = bootstrap_profile(seed=_seed([]), pubmed=_Pub(0), ollama=_LLMFail())
    assert d.llm_succeeded is False


def test_parseable_but_blank_description_is_failure():
    d = bootstrap_profile(
        seed=_seed([]), pubmed=_Pub(0), ollama=_ollama('{"description": "", "structured": {}}')
    )
    assert d.llm_succeeded is False
