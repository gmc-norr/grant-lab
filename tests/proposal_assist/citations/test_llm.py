import pytest

from call_manager.proposal_assist.citations.llm import (
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

    from call_manager.config import Settings

    for k, v in {
        "DATABASE_URL": "postgresql://x",
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://localhost:8000",
    }.items():
        os.environ.setdefault(k, v)
    s = Settings()
    object.__setattr__(s, "citation_llm_provider", "anthropic")
    object.__setattr__(s, "anthropic_api_key", None)
    with pytest.raises(CitationLLMConfigError):
        build_citation_llm(s)
