import json
from datetime import UTC, datetime

import httpx
import pytest

from call_monitor.db.models import Call, Profile, ProfileKind
from call_monitor.ranking.ollama_client import LLMError, OllamaClient
from call_monitor.ranking.rank_pair import RankResult, rank_pair


def _profile():
    return Profile(
        id=1,
        name="Pathology AI",
        kind=ProfileKind.GROUP,
        description="Develop AI methods for digital pathology and federated learning.",
        structured={
            "areas": ["digital pathology"],
            "must_include": ["pathology"],
            "hard_exclusions": ["animal studies"],
        },
        is_active=True,
        created_by=1,
    )


def _call():
    return Call(
        id=42,
        source_id=1,
        external_id="HORIZON-X",
        url="https://x",
        title="AI for digital pathology",
        summary="EU funding for AI-based digital pathology research.",
        deadline=datetime(2026, 9, 1, tzinfo=UTC),
        budget_max=2_000_000,
        currency="EUR",
        programme="Horizon Europe",
        keywords=["health", "ai"],
        published_at=datetime(2026, 4, 1, tzinfo=UTC),
        raw={},
    )


def _client_returning(content: str | dict, status: int = 200):
    if isinstance(content, dict):
        content = json.dumps(content)
    body = {
        "model": "gemma4:e4b-q8",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
    }

    def handler(request):
        return httpx.Response(status, json=body)

    return OllamaClient(
        base_url="http://x",
        model="gemma4:e4b-q8",
        transport=httpx.MockTransport(handler),
    )


def test_rank_pair_happy_path():
    client = _client_returning({"score": 72, "rationale": "Strong overlap"})
    out = rank_pair(client=client, profile=_profile(), call=_call())
    assert isinstance(out, RankResult)
    assert out.score == 72
    assert out.rationale == "Strong overlap"
    assert out.model == "gemma4:e4b-q8"
    assert out.prompt_version == "v1"
    assert out.raw_response["choices"][0]["message"]["content"]


def test_malformed_json_raises_llm_error():
    client = _client_returning("not-json-at-all")
    with pytest.raises(LLMError, match="invalid JSON"):
        rank_pair(client=client, profile=_profile(), call=_call())


def test_missing_score_field_raises_llm_error():
    client = _client_returning({"rationale": "missing score"})
    with pytest.raises(LLMError):
        rank_pair(client=client, profile=_profile(), call=_call())


def test_score_out_of_range_raises_llm_error():
    client = _client_returning({"score": 150, "rationale": "too high"})
    with pytest.raises(LLMError):
        rank_pair(client=client, profile=_profile(), call=_call())


def test_score_bool_raises_llm_error():
    """Python's isinstance(True, int) is True (bool subclasses int). Without
    an explicit bool check, `{"score": true}` would silently persist as
    score=1 instead of being retried/dead-lettered."""
    client = _client_returning({"score": True, "rationale": "boolish"})
    with pytest.raises(LLMError, match="score"):
        rank_pair(client=client, profile=_profile(), call=_call())


def test_empty_assistant_content_raises_llm_error():
    """Gemma in JSON mode may respond with tool_calls instead of plain
    content, leaving content=None or empty. Must raise LLMError, not
    TypeError on the downstream json.loads(None)."""
    body = {
        "model": "gemma4:e4b-q8",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": None}}
        ],
    }
    def handler(request):
        return httpx.Response(200, json=body)
    client = OllamaClient(
        base_url="http://x", model="gemma4:e4b-q8",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError, match="empty assistant content"):
        rank_pair(client=client, profile=_profile(), call=_call())


def test_score_clamped_in_envelope_truncated_rationale():
    """Rationale longer than reasonable is truncated to 1000 chars to
    bound row size; full text remains in raw_response."""
    long = "x" * 5000
    client = _client_returning({"score": 50, "rationale": long})
    out = rank_pair(client=client, profile=_profile(), call=_call())
    assert len(out.rationale) <= 1000
    # Full text preserved in raw_response
    assert long in out.raw_response["choices"][0]["message"]["content"]
