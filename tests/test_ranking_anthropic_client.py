"""AnthropicClient — wire-format parity with OllamaClient.

These tests drive the SDK with a stubbed messages.create so we exercise
the envelope construction without burning real tokens.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from grant_lab.ranking.anthropic_client import (
    AnthropicClient,
    UnusableReplyError,
    _split_system,
    _strip_markdown_fence,
)
from grant_lab.ranking.ollama_client import LLMError
from grant_lab.ranking.prompt import ParseError


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


def test_chat_sends_a_json_schema_response_format_as_output_config():
    """Structured outputs: the API then guarantees JSON matching the schema.
    Without it Sonnet wrote ~10% of rankings with a stray `;` (2026-10-05)."""
    client = AnthropicClient(api_key="sk-test")
    schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    response_format = {"type": "json_schema", "json_schema": {"name": "x", "schema": schema}}
    with patch.object(client._client.messages, "create", return_value=_fake_response("{}")) as mock:
        _call_chat(client, response_format=response_format)
    assert mock.call_args.kwargs["output_config"] == {
        "format": {"type": "json_schema", "schema": schema}
    }


@pytest.mark.parametrize("response_format", [None, {"type": "json_object"}])
def test_chat_without_a_schema_sends_no_output_config(response_format):
    """Proposal-assist and citations share this client and send no schema;
    their requests must not change."""
    client = AnthropicClient(api_key="sk-test")
    with patch.object(client._client.messages, "create", return_value=_fake_response("{}")) as mock:
        _call_chat(client, response_format=response_format)
    assert "output_config" not in mock.call_args.kwargs


def test_chat_returns_a_schema_reply_without_stripping_fences():
    """A structured-output reply is the JSON itself. The fence stripper would
    cut a valid reply whose prose contains a ``` pair down to the fenced word
    (final review 2026-10-05, finding 3)."""
    client = AnthropicClient(api_key="sk-test")
    reply = json.dumps({"summary": "Submit the ```json``` form."})
    response_format = {"type": "json_schema", "json_schema": {"name": "x", "schema": {}}}
    with patch.object(client._client.messages, "create", return_value=_fake_response(reply)):
        env = _call_chat(client, response_format=response_format)
    assert env["choices"][0]["message"]["content"] == reply


_SCHEMA_FORMAT = {"type": "json_schema", "json_schema": {"name": "x", "schema": {}}}


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_an_unusable_ranking_reply_is_both_a_service_and_a_parse_failure(stop_reason):
    """A refused or truncated ranking reply recurs on the same inputs, so
    rank_pair must count it as parse_error (toward gave_up), not llm_error
    (pending every night). Still an LLMError, as truncation always was
    (final review 2026-10-05, finding 4)."""
    client = AnthropicClient(api_key="sk-test")
    resp = _fake_response("{", stop_reason=stop_reason)
    with (
        patch.object(client._client.messages, "create", return_value=resp),
        pytest.raises(UnusableReplyError) as raised,
    ):
        _call_chat(client, response_format=_SCHEMA_FORMAT)
    assert isinstance(raised.value, LLMError)
    assert isinstance(raised.value, ParseError)


def test_a_refusal_without_a_schema_still_returns_its_text():
    """Proposal-assist compaction, citations and the extractors send no
    schema. A refusal there kept its old behaviour: the text comes back. Raising
    would turn compaction's retry-every-tick into an unrecorded re-bill
    (second review 2026-10-05, finding 1)."""
    client = AnthropicClient(api_key="sk-test")
    resp = _fake_response("partial answer", stop_reason="refusal")
    with patch.object(client._client.messages, "create", return_value=resp):
        env = _call_chat(client)
    assert env["choices"][0]["message"]["content"] == "partial answer"


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


def test_chat_wraps_api_response_validation_error_in_llm_error():
    """APIResponseValidationError derives directly from anthropic.APIError —
    it is neither an APIStatusError nor an APIConnectionError, so without a
    final `except anthropic.APIError` fallback it escapes the LLMError wrap
    and fails the whole worker job, breaking rank_pair's keep-local contract
    (remote failures must NEVER propagate)."""
    import anthropic

    client = AnthropicClient(api_key="sk-test")
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    err = anthropic.APIResponseValidationError(response=fake_resp, body=None)
    with (
        patch.object(client._client.messages, "create", side_effect=err),
        pytest.raises(LLMError) as exc_info,
    ):
        _call_chat(client)
    # The exception class name is carried in the message for triage.
    assert "APIResponseValidationError" in str(exc_info.value)


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


def test_strip_markdown_fence_extracts_from_prose_preamble():
    """A fenced block inside a prose preamble (e.g.
    'Here's the analysis: ```json...```') IS recovered — a single stray
    preamble token must not dead-letter the pair through five paid retries."""
    text = 'Here\'s the answer: ```json\n{"a": 1}\n```'
    assert _strip_markdown_fence(text) == '{"a": 1}'


def test_strip_markdown_fence_leaves_raw_json_untouched():
    """A response that is already raw JSON (no fence) is returned verbatim."""
    text = '{"a": 1, "b": [2, 3]}'
    assert _strip_markdown_fence(text) == text


def test_strip_markdown_fence_prefers_last_parseable_block_over_preamble_example():
    """B11 Defect 4b: when the model emits a pre-JSON EXAMPLE fence followed by
    the REAL answer fence, taking the FIRST block grabs the example (often
    truncated/illustrative) and dead-letters a valid reply. Prefer the LAST
    parseable block — the real answer."""
    text = (
        "Here is an example of the format I will use:\n"
        '```json\n{"example": true}\n```\n'
        "And here is my actual answer:\n"
        '```json\n{"topic_fit": {"rating": 4}}\n```'
    )
    out = _strip_markdown_fence(text)
    assert out == '{"topic_fit": {"rating": 4}}'


def test_strip_markdown_fence_skips_unparseable_trailing_block():
    """When the LAST fenced block is not valid JSON but an earlier one is,
    fall back to the earlier parseable block rather than returning junk."""
    text = (
        '```json\n{"topic_fit": {"rating": 4}}\n```\n'
        "Note: the above is the answer.\n"
        "```\nnot json at all\n```"
    )
    out = _strip_markdown_fence(text)
    assert out == '{"topic_fit": {"rating": 4}}'


def test_chat_strips_markdown_fence_from_response():
    """A caller without a schema (the extractors, citations) gets
    ```json{...}``` back as the raw JSON, not the fenced version. The ranker
    sends a schema and skips this path."""
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
    on the envelope sees the OpenAI vocabulary.

    Exercise the mapper directly: a `max_tokens` *response* now raises
    LLMError (see test_chat_raises_on_max_tokens_truncation), so we
    verify the vocabulary mapping on the helper rather than the envelope.
    """
    from grant_lab.ranking.anthropic_client import _map_stop_reason

    assert _map_stop_reason("max_tokens") == "length"
    assert _map_stop_reason("end_turn") == "stop"
    assert _map_stop_reason("refusal") == "content_filter"


def test_chat_raises_on_max_tokens_truncation():
    """stop_reason='max_tokens' means content is a truncated JSON fragment.
    It would fail parse_response → ParseError → 5 paid retries → dead-letter,
    and retrying cannot help (truncation is deterministic). Surface it as
    LLMError immediately, before the (useless) partial content escapes."""
    client = AnthropicClient(api_key="sk-test")
    # Even though the body looks like it starts valid JSON, it's truncated.
    truncated = '{"topic_fit": {"rating": 4, "rationale": "the call'
    with (
        patch.object(
            client._client.messages,
            "create",
            return_value=_fake_response(truncated, stop_reason="max_tokens"),
        ),
        pytest.raises(LLMError) as exc_info,
    ):
        _call_chat(client)
    assert "max_tokens" in str(exc_info.value)


@pytest.mark.parametrize(
    "stop_reason,blocks", [("max_tokens", "text"), ("end_turn", "none"), ("refusal", "text")]
)
def test_an_error_raised_after_the_api_answered_carries_the_billed_usage(stop_reason, blocks):
    """Anthropic billed the call before the client raised. Callers that record
    spend on failure (compaction) read `.usage` instead of storing 0."""
    client = AnthropicClient(api_key="sk-test")
    resp = _fake_response("{", stop_reason=stop_reason)
    if blocks == "none":
        resp.content = []
    response_format = _SCHEMA_FORMAT if stop_reason == "refusal" else None
    with (
        patch.object(client._client.messages, "create", return_value=resp),
        pytest.raises(LLMError) as raised,
    ):
        _call_chat(client, response_format=response_format)
    assert raised.value.usage["prompt_tokens"] == 120
    assert raised.value.usage["completion_tokens"] == 80


def test_a_timeout_is_an_llm_error_and_a_timeout_error():
    """The server may have finished (and billed) a reply the client gave up
    on, so callers that record spend mark it `timeout`, not a free failure:
    compaction counts it toward its retry limit, and citations' existing
    `except TimeoutError` branch records it (review 2026-10-05, finding 1)."""
    import httpx

    from grant_lab.ranking.anthropic_client import AnthropicTimeoutError

    client = AnthropicClient(api_key="sk-test")
    with (
        patch.object(
            client._client.messages, "create", side_effect=_timeout(httpx.ReadTimeout("r"))
        ),
        pytest.raises(AnthropicTimeoutError) as raised,
    ):
        _call_chat(client)
    assert isinstance(raised.value, LLMError)
    assert isinstance(raised.value, TimeoutError)


@pytest.mark.parametrize("cause", ["connect", "pool", "write"])
def test_a_timeout_before_the_request_was_read_is_a_plain_connection_error(cause):
    """A connect, pool or write timeout never delivered the request, so nothing
    was billed: a plain LLMError, not a TimeoutError. Counting it as possibly
    billed let a 15-minute network outage stop compaction for 24 h (review
    2026-10-05, round 3, finding 1)."""
    import httpx

    exc = {
        "connect": httpx.ConnectTimeout("c"),
        "pool": httpx.PoolTimeout("p"),
        "write": httpx.WriteTimeout("w"),
    }[cause]
    client = AnthropicClient(api_key="sk-test")
    with (
        patch.object(client._client.messages, "create", side_effect=_timeout(exc)),
        pytest.raises(LLMError) as raised,
    ):
        _call_chat(client)
    assert not isinstance(raised.value, TimeoutError)


def test_max_retries_reaches_the_sdk():
    """Compaction's tick already retries every 300 s; SDK retries on top
    multiplied each billed attempt by three (review 2026-10-05)."""
    assert AnthropicClient(api_key="sk-test", max_retries=0)._client.max_retries == 0
    assert AnthropicClient(api_key="sk-test")._client.max_retries == 2


def _timeout(cause):
    """anthropic.APITimeoutError as the SDK raises it: from the httpx timeout."""
    import anthropic
    import httpx

    exc = anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.example"))
    exc.__cause__ = cause
    return exc
