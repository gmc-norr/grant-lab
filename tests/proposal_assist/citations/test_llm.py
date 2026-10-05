import pytest

from grant_lab.proposal_assist.citations.llm import (
    CitationLLMConfigError,
    StructuredLLM,
    build_citation_llm,
)


class _FakeClient:
    def __init__(self, content, usage=None):
        self._content = content
        self._usage = usage or {"prompt_tokens": 10, "completion_tokens": 4}
        self.calls = []

    def chat(self, *, messages, response_format=None):
        self.calls.append(messages)
        return {
            "model": "fake-model",
            "choices": [{"message": {"role": "assistant", "content": self._content}}],
            "usage": self._usage,
        }


def test_complete_json_parses_and_reports_usage():
    client = _FakeClient('{"title": "X"}')
    result = StructuredLLM(client, "fake-model").complete_json(system="s", user="u")
    assert result.data == {"title": "X"}
    assert result.usage.tokens_in == 10
    assert result.usage.tokens_out == 4
    assert client.calls[0][0]["role"] == "system" and client.calls[0][1]["role"] == "user"


def test_complete_json_sums_anthropic_cache_tokens():
    client = _FakeClient(
        '{"ok": true}',
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 50,
        },
    )
    result = StructuredLLM(client, "fake-model").complete_json(system="s", user="u")
    assert result.usage.tokens_in == 160  # 10 + 100 + 50


def test_build_citation_llm_anthropic_requires_key():
    import os

    from grant_lab.config import Settings

    for k, v in {
        "DATABASE_URL": "postgresql://x",
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://localhost:8000",
    }.items():
        os.environ.setdefault(k, v)
    s = Settings()
    object.__setattr__(s, "citation_llm_provider", "anthropic")
    object.__setattr__(s, "anthropic_api_key", None)
    with pytest.raises(CitationLLMConfigError):
        build_citation_llm(s)


def test_build_citation_llm_rejects_unknown_provider():
    import os

    from grant_lab.config import Settings

    for k, v in {
        "DATABASE_URL": "postgresql://x",
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://localhost:8000",
    }.items():
        os.environ.setdefault(k, v)
    s = Settings()
    object.__setattr__(s, "citation_llm_provider", "bogus")
    with pytest.raises(CitationLLMConfigError):
        build_citation_llm(s)


def test_complete_json_handles_none_cache_tokens():
    # Anthropic sends cache fields as None (present, not absent) when caching
    # is unused; tokens_in must treat them as 0, not crash.
    client = _FakeClient(
        '{"ok": true}',
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "cache_read_input_tokens": None,
            "cache_creation_input_tokens": None,
        },
    )
    result = StructuredLLM(client, "fake-model").complete_json(system="s", user="u")
    assert result.usage.tokens_in == 10
    assert result.usage.tokens_out == 4


def test_complete_json_raises_on_invalid_json():
    client = _FakeClient("not json at all")
    import json

    with pytest.raises(json.JSONDecodeError):
        StructuredLLM(client, "fake-model").complete_json(system="s", user="u")


def test_complete_json_raises_on_non_dict_json():
    from grant_lab.proposal_assist.citations.llm import CitationLLMOutputError

    client = _FakeClient("[1, 2, 3]")
    with pytest.raises(CitationLLMOutputError):
        StructuredLLM(client, "fake-model").complete_json(system="s", user="u")


@pytest.mark.parametrize("content", ["not json at all", "[1, 2, 3]"])
def test_a_reply_that_fails_to_parse_still_carries_its_billed_usage(content):
    """The call was answered and billed before parsing failed. The service
    records `.usage` instead of 0 (review 2026-10-05, finding 5)."""
    usage = {"prompt_tokens": 10, "completion_tokens": 4}
    with pytest.raises(Exception) as raised:
        StructuredLLM(_FakeClient(content, usage=usage), "fake-model").complete_json(
            system="s", user="u"
        )
    assert raised.value.usage == usage
