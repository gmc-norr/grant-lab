"""AnthropicClient — wire-format parity with OllamaClient.

These tests drive the SDK with a stubbed messages.create so we exercise
the envelope construction without burning real tokens.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from call_manager.ranking.anthropic_client import (
    AnthropicClient,
    _split_system,
    _strip_markdown_fence,
)
from call_manager.ranking.ollama_client import LLMError


def _fake_response(text: str, *, stop_reason: str = "end_turn", model: str = "claude-sonnet-4-6"):
    """Build a minimal stand-in for anthropic.types.Message."""
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = text
    resp = MagicMock()
    resp.content = [text_block]
    resp.model = model
    resp.stop_reason = stop_reason
    resp.usage = MagicMock(
        input_tokens=120,
        output_tokens=80,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return resp


def _call_chat(client: AnthropicClient, **kw):
    """Helper to invoke .chat with a default message list."""
    return client.chat(
        messages=kw.pop(
            "messages",
            [
                {"role": "system", "content": "You are a ranker."},
                {"role": "user", "content": "Score this call."},
            ],
        ),
        **kw,
    )


def test_chat_returns_openai_shaped_envelope():
    """rank_pair's downstream code reaches `envelope['choices'][0]
    ['message']['content']` — Anthropic and Ollama paths must agree on
    that shape exactly."""
    client = AnthropicClient(api_key="sk-test", model="claude-sonnet-4-6")
    payload = json.dumps(
        {
            "topic_fit": {"rating": 4, "rationale": "good", "quoted_evidence": "x"},
            "eligibility": {"rating": "pass", "rationale": "fits", "quoted_evidence": None},
            "exclusions": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
        }
    )
    with patch.object(client._client.messages, "create", return_value=_fake_response(payload)):
        env = _call_chat(client)
    assert env["choices"][0]["message"]["content"] == payload
    assert env["choices"][0]["message"]["role"] == "assistant"
    assert env["model"] == "claude-sonnet-4-6"


def test_chat_splits_system_into_anthropic_top_level_param():
    """Anthropic refuses role='system' inside messages — verify our
    client lifts the system message into the top-level `system` arg
    and forwards only user/assistant turns in messages."""
    client = AnthropicClient(api_key="sk-test")
    with patch.object(client._client.messages, "create", return_value=_fake_response("{}")) as mock:
        client.chat(
            messages=[
                {"role": "system", "content": "Be helpful."},
                {"role": "user", "content": "Hi."},
            ]
        )
    kwargs = mock.call_args.kwargs
    # system reaches Anthropic as a list with cache_control on the last block
    assert isinstance(kwargs["system"], list)
    assert kwargs["system"][0]["text"] == "Be helpful."
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    # messages contains only the user turn — no role=system slipped through
    assert [m["role"] for m in kwargs["messages"]] == ["user"]


def test_chat_applies_prompt_caching_to_system_block():
    """The system block carries the per-profile rubric, stable across
    every call scored against that profile within the 5-min cache window.
    Without cache_control we'd re-pay full input cost on every pair."""
    client = AnthropicClient(api_key="sk-test")
    with patch.object(client._client.messages, "create", return_value=_fake_response("{}")) as mock:
        client.chat(
            messages=[
                {"role": "system", "content": "criteria + rubric here"},
                {"role": "user", "content": "score this"},
            ]
        )
    system = mock.call_args.kwargs["system"]
    assert system[0]["cache_control"]["type"] == "ephemeral"


def test_chat_handles_empty_system():
    """Some callers may build a single-user-turn message list. We must
    not break by passing an empty system list to Anthropic."""
    client = AnthropicClient(api_key="sk-test")
    with patch.object(client._client.messages, "create", return_value=_fake_response("ok")) as mock:
        client.chat(messages=[{"role": "user", "content": "Hi."}])
    # system param either omitted or an empty list — both acceptable
    system = mock.call_args.kwargs.get("system")
    assert system in ([], None) or (isinstance(system, list) and not system)


def test_chat_wraps_api_status_error_in_llm_error():
    """worker.drain_queue catches LLMError to mark a job pending-or-failed.
    Letting an anthropic.APIStatusError bubble would crash the worker."""
    import anthropic

    client = AnthropicClient(api_key="sk-test")
    fake_resp = MagicMock()
    fake_resp.headers = {"request-id": "req_abc123"}
    fake_resp.status_code = 429
    err = anthropic.APIStatusError(message="rate limited", response=fake_resp, body={})
    with (
        patch.object(client._client.messages, "create", side_effect=err),
        pytest.raises(LLMError) as exc_info,
    ):
        _call_chat(client)
    # request_id must appear in the message for support-ticket debugging
    assert "req_abc123" in str(exc_info.value)


def test_chat_wraps_connection_error_in_llm_error():
    import anthropic

    client = AnthropicClient(api_key="sk-test")
    with (
        patch.object(
            client._client.messages,
            "create",
            side_effect=anthropic.APIConnectionError(request=MagicMock()),
        ),
        pytest.raises(LLMError),
    ):
        _call_chat(client)


def test_chat_raises_llm_error_on_no_text_blocks():
    """Anthropic responses without a text block would silently produce
    `envelope['choices'][0]['message']['content']` = '' and confuse the
    parser. Surface as LLMError so the job goes back to PENDING."""
    client = AnthropicClient(api_key="sk-test")
    empty_resp = MagicMock()
    empty_resp.content = []  # no text blocks
    empty_resp.model = "claude-sonnet-4-6"
    empty_resp.stop_reason = "end_turn"
    empty_resp.usage = MagicMock(
        input_tokens=10,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    with (
        patch.object(client._client.messages, "create", return_value=empty_resp),
        pytest.raises(LLMError),
    ):
        _call_chat(client)


def test_split_system_concatenates_multiple_system_messages():
    """Defensive: if a caller ever ships two system messages we want
    them joined, not silently dropped."""
    system, rest = _split_system(
        [
            {"role": "system", "content": "rule one"},
            {"role": "system", "content": "rule two"},
            {"role": "user", "content": "go"},
        ]
    )
    assert "rule one" in system
    assert "rule two" in system
    assert rest == [{"role": "user", "content": "go"}]


def test_chat_reports_usage_with_cache_fields():
    """Cache hits drop input cost ~90% — make the field visible on the
    envelope so callers can log and verify caching is working."""
    client = AnthropicClient(api_key="sk-test")
    response = _fake_response("{}")
    response.usage.cache_read_input_tokens = 500
    response.usage.cache_creation_input_tokens = 0
    with patch.object(client._client.messages, "create", return_value=response):
        env = _call_chat(client)
    assert env["usage"]["cache_read_input_tokens"] == 500


def test_strip_markdown_fence_handles_json_block():
    """Anthropic models often wrap structured output in ```json ... ```
    fences. The downstream parser uses json.loads() directly, so we
    strip a single outer fence before returning the envelope."""
    text = '```json\n{"a": 1}\n```'
    assert _strip_markdown_fence(text) == '{"a": 1}'


def test_strip_markdown_fence_handles_bare_fence():
    """Sometimes the fence has no language tag."""
    text = '```\n{"a": 1}\n```'
    assert _strip_markdown_fence(text) == '{"a": 1}'


def test_strip_markdown_fence_leaves_raw_json_alone():
    """Models that follow instructions and emit raw JSON shouldn't be
    touched — passthrough is the no-op."""
    text = '{"a": 1}'
    assert _strip_markdown_fence(text) == text


def test_strip_markdown_fence_no_partial_strip():
    """If the response has prose before/after the fence (e.g.
    'Here's the analysis: ```json...```'), don't strip — that's a
    different failure mode the prompt should fix, not silent recovery."""
    text = 'Here\'s the answer: ```json\n{"a": 1}\n```'
    # The fence isn't at the start, so leave the whole string alone
    assert _strip_markdown_fence(text) == text


def test_chat_strips_markdown_fence_from_response():
    """End-to-end: Sonnet returns ```json{...}``` → envelope content
    is the parseable raw JSON, not the fenced version."""
    client = AnthropicClient(api_key="sk-test")
    fenced = '```json\n{"topic_fit": {"rating": 4}}\n```'
    with patch.object(client._client.messages, "create", return_value=_fake_response(fenced)):
        env = _call_chat(client)
    content = env["choices"][0]["message"]["content"]
    # Must be loadable as a dict — no fence chars left
    assert json.loads(content) == {"topic_fit": {"rating": 4}}


def test_finish_reason_maps_anthropic_stop_to_openai_vocab():
    """Anthropic uses end_turn/max_tokens/refusal; OpenAI uses stop/
    length/content_filter. Map so anything inspecting finish_reason
    on the envelope sees the OpenAI vocabulary."""
    client = AnthropicClient(api_key="sk-test")
    with patch.object(
        client._client.messages,
        "create",
        return_value=_fake_response("{}", stop_reason="max_tokens"),
    ):
        env = _call_chat(client)
    assert env["choices"][0]["finish_reason"] == "length"
