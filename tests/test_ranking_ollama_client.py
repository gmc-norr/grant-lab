import json

import httpx
import pytest

from grant_lab.ranking.ollama_client import (
    LLMError,
    LocalUnusableReplyError,
    OllamaClient,
)
from grant_lab.ranking.prompt import ParseError

_FAKE_RESPONSE = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "model": "gemma4:e4b-q8",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": json.dumps({"score": 72, "rationale": "Strong overlap"}),
            },
            "finish_reason": "stop",
        }
    ],
}


def _mock(status: int = 200, body=None, raise_exc=None):
    def handler(request):
        if raise_exc is not None:
            raise raise_exc
        return httpx.Response(status, json=body if body is not None else _FAKE_RESPONSE)

    return httpx.MockTransport(handler)


def test_chat_returns_full_response_dict():
    client = OllamaClient(
        base_url="http://localhost:11434",
        model="gemma4:e4b-q8",
        transport=_mock(),
    )
    out = client.chat(messages=[{"role": "user", "content": "hi"}])
    assert out == _FAKE_RESPONSE


def test_chat_5xx_raises_llm_error():
    client = OllamaClient(
        base_url="http://localhost:11434",
        model="gemma4:e4b-q8",
        transport=_mock(status=503),
    )
    with pytest.raises(LLMError) as excinfo:
        client.chat(messages=[{"role": "user", "content": "hi"}])
    # NOT a ParseError. `LocalUnusableReplyError` satisfies `raises(LLMError)`
    # too, so without this line the dangerous direction is unpinned: a real
    # outage classified `parse` would be SUPPRESSED, and the pair would stop
    # re-ranking silently instead of retrying.
    assert not isinstance(excinfo.value, ParseError)


def test_chat_network_failure_raises_llm_error():
    client = OllamaClient(
        base_url="http://localhost:11434",
        model="gemma4:e4b-q8",
        transport=_mock(raise_exc=httpx.ConnectError("boom")),
    )
    with pytest.raises(LLMError) as excinfo:
        client.chat(messages=[{"role": "user", "content": "hi"}])
    assert not isinstance(excinfo.value, ParseError)  # see the 5xx test


def test_chat_raises_on_missing_choices():
    """Malformed envelope (e.g. `{"error": "..."}`) must raise LLMError,
    not let a KeyError bubble up to the caller and crash the rank worker."""
    client = OllamaClient(
        base_url="http://localhost:11434",
        model="gemma4:e4b-q8",
        transport=_mock(body={"error": "model not loaded"}),
    )
    with pytest.raises(LLMError):
        client.chat(messages=[{"role": "user", "content": "hi"}])


def test_chat_raises_on_empty_choices_list():
    client = OllamaClient(
        base_url="http://localhost:11434",
        model="gemma4:e4b-q8",
        transport=_mock(body={"choices": []}),
    )
    with pytest.raises(LLMError):
        client.chat(messages=[{"role": "user", "content": "hi"}])


def test_chat_raises_on_missing_message_content():
    client = OllamaClient(
        base_url="http://localhost:11434",
        model="gemma4:e4b-q8",
        transport=_mock(body={"choices": [{"index": 0, "message": {"role": "assistant"}}]}),
    )
    with pytest.raises(LLMError):
        client.chat(messages=[{"role": "user", "content": "hi"}])


def test_chat_raises_on_null_message_content():
    client = OllamaClient(
        base_url="http://localhost:11434",
        model="gemma4:e4b-q8",
        transport=_mock(body={"choices": [{"index": 0, "message": {"content": None}}]}),
    )
    with pytest.raises(LLMError):
        client.chat(messages=[{"role": "user", "content": "hi"}])


def test_chat_raises_on_length_truncation():
    """If Ollama stops at the num_predict ceiling (finish_reason 'length'),
    the content is a partial, almost-certainly-invalid JSON fragment. Mirror
    the Anthropic client: raise before that truncated body reaches the parser.

    Review 2026-10-26 H2 changed WHAT is raised. This used to say "LLMError
    ... instead of dead-lettering on a misleading ParseError"; it is now
    `LocalUnusableReplyError`, which IS a `ParseError` as well as an
    `LLMError`, deliberately -- a reply cut off by the window is deterministic,
    so the worker must classify it `parse` and stop re-enqueueing it nightly.
    The second parent is the whole point of the change."""
    truncated = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "gemma4:e4b-q8",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": '{"topic_fit": {"rat'},
                "finish_reason": "length",
            }
        ],
    }
    client = OllamaClient(
        base_url="http://localhost:11434",
        model="gemma4:e4b-q8",
        transport=_mock(body=truncated),
    )
    with pytest.raises(LLMError):
        client.chat(messages=[{"role": "user", "content": "hi"}])


def test_chat_request_shape_is_openai_compatible():
    """Verify model + temperature + response_format reach the wire as
    the OpenAI-compatible API expects them."""
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content.decode())
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_FAKE_RESPONSE)

    client = OllamaClient(
        base_url="http://localhost:11434",
        model="gemma4:e4b-q8",
        transport=httpx.MockTransport(handler),
    )
    client.chat(messages=[{"role": "user", "content": "hi"}])
    assert captured["url"].endswith("/v1/chat/completions")
    body = captured["body"]
    assert body["model"] == "gemma4:e4b-q8"
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [{"role": "user", "content": "hi"}]


def test_chat_sends_num_predict_token_cap():
    """A degenerate (looping) generation must be bounded by an output-token
    ceiling, not only the 120s wall-clock timeout. Verify num_predict reaches
    the wire via Ollama's options block."""
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=_FAKE_RESPONSE)

    client = OllamaClient(
        base_url="http://localhost:11434",
        model="gemma4:e4b-q8",
        transport=httpx.MockTransport(handler),
        num_predict=1234,
    )
    client.chat(messages=[{"role": "user", "content": "hi"}])
    assert captured["body"]["options"]["num_predict"] == 1234


def test_chat_default_num_predict_is_bounded():
    """The default ceiling is finite (not unlimited) so the cap applies even
    when callers don't pass num_predict explicitly."""
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=_FAKE_RESPONSE)

    client = OllamaClient(
        base_url="http://localhost:11434",
        model="gemma4:e4b-q8",
        transport=httpx.MockTransport(handler),
    )
    client.chat(messages=[{"role": "user", "content": "hi"}])
    np = captured["body"]["options"]["num_predict"]
    assert isinstance(np, int) and 0 < np <= 8192


# --- Review 2026-10-26 H2: a reply cut off by the context window ---------
#
# Since the Ollama 0.35.1 upgrade (2026-10-06) Gemma 4 writes its thinking
# into `message.reasoning` and leaves `content: ''`, or stops at
# `finish_reason='length'` with a partial fragment. The coordinator replayed
# failing call 429 and measured prompt 3168 + output 928 = 4096, Ollama's
# default context window: raising num_predict changes nothing. Both shapes
# are DETERMINISTIC for the same inputs, so they must not be classified as a
# service outage -- `service` never suppresses in enqueue_stale_pairs, which
# is why 541 jobs and 2705 Gemma calls were burned re-running them nightly.

_OVERFLOW_USAGE = {"prompt_tokens": 3168, "completion_tokens": 928, "total_tokens": 4096}

# The reasoning text Gemma writes instead of content (live `last_error` shape).
_REASONING = (
    "Here I need to weigh the call's topic against the group's areas. "
    "The call is about hepatology, and the profile lists liver cancer, so "
)


def _overflow_envelope(finish_reason: str, content: str = "") -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "gemma4:e4b-it-q4_K_M",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "reasoning": _REASONING,
                },
            }
        ],
        "usage": _OVERFLOW_USAGE,
    }


def _chat_overflow(finish_reason: str, content: str = ""):
    """Run chat() against the overflow envelope and return the raised error."""
    client = OllamaClient(
        base_url="http://localhost:11434",
        model="gemma4:e4b-it-q4_K_M",
        transport=_mock(body=_overflow_envelope(finish_reason, content)),
    )
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - the class IS the assertion
        client.chat(messages=[{"role": "user", "content": "hi"}])
    return excinfo.value


def test_empty_content_with_reasoning_at_length_is_unusable_not_a_service_outage():
    """Red 1. The 336-of-541 live shape: thinking filled the window, so
    `content` came back empty with `finish_reason='length'`.

    Both parents matter, and for different readers: `worker.drain_queue`
    writes failure_kind='parse' only for a ParseError (a `service` row never
    suppresses), while every existing caller catches LLMError and must keep
    catching this."""
    err = _chat_overflow("length")
    assert isinstance(err, LocalUnusableReplyError)
    assert isinstance(err, ParseError), "a `service` failure_kind retries this forever"
    assert isinstance(err, LLMError), "existing `except LLMError` callers must still catch it"


def test_empty_content_with_reasoning_at_stop_is_unusable_not_a_service_outage():
    """Red 2. The same empty reply, but the server reported a clean stop:
    classification must not hang on finish_reason alone."""
    err = _chat_overflow("stop")
    assert isinstance(err, LocalUnusableReplyError)
    assert isinstance(err, ParseError)
    assert isinstance(err, LLMError)


def test_a_truncated_non_empty_reply_is_unusable_not_a_service_outage():
    """The OTHER live shape (205 of 541, 'truncated at num_predict'): the
    model emitted a partial JSON fragment and stopped at the ceiling. Already
    raised before this change, but as a plain LLMError -> `service` -> retried
    nightly, so it needs the same dual parent."""
    err = _chat_overflow("length", content='{"topic_fit": {"rat')
    assert isinstance(err, LocalUnusableReplyError)
    assert isinstance(err, ParseError)
    assert isinstance(err, LLMError)


def test_the_unusable_message_names_the_context_window_and_both_token_counts():
    """Red 3. An operator must be able to act on `last_error` alone: which
    knob to raise, and the arithmetic that says the window is the limit
    (3168 + 928 = 4096, Ollama's default)."""
    msg = str(_chat_overflow("length"))
    assert "context window" in msg, msg
    assert "OLLAMA_CONTEXT_LENGTH" in msg, msg
    assert "3168" in msg, "prompt_tokens must survive into last_error"
    assert "928" in msg, "completion_tokens must survive into last_error"
    # It must stay short enough to survive queue.py's 2000-char last_error cut
    # even after worker.py prefixes "ranking failed: ".
    assert len(msg) < 1000, len(msg)


def test_a_missing_usage_block_still_names_the_knob():
    """Ollama has omitted `usage` on some endpoints. The advice must not
    depend on the token counts being there."""
    envelope = _overflow_envelope("length")
    del envelope["usage"]
    client = OllamaClient(
        base_url="http://localhost:11434",
        model="gemma4:e4b-it-q4_K_M",
        transport=_mock(body=envelope),
    )
    with pytest.raises(LocalUnusableReplyError) as excinfo:
        client.chat(messages=[{"role": "user", "content": "hi"}])
    assert "OLLAMA_CONTEXT_LENGTH" in str(excinfo.value)


def test_an_empty_reply_with_no_reasoning_stays_a_service_failure():
    """Discriminating control. Without it, "classify every failure as parse"
    passes the four tests above -- and silently suppresses real Ollama
    malfunctions for a week (DEAD_LETTER_SUPPRESS_DAYS).

    An envelope with no content AND no thinking is a malformed reply, not the
    measured context-window overflow, so it keeps the old `service` reading
    and is retried."""
    client = OllamaClient(
        base_url="http://localhost:11434",
        model="gemma4:e4b-it-q4_K_M",
        # finish_reason "stop" explicitly, not omitted: "stop" is the value the
        # live envelopes actually carry, and the absent-key case takes the same
        # branch anyway.
        transport=_mock(
            body={
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"content": ""}}
                ]
            }
        ),
    )
    with pytest.raises(LLMError) as excinfo:
        client.chat(messages=[{"role": "user", "content": "hi"}])
    assert not isinstance(excinfo.value, ParseError)
