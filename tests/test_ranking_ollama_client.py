import json

import httpx
import pytest

from call_monitor.ranking.ollama_client import LLMError, OllamaClient

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
    with pytest.raises(LLMError):
        client.chat(messages=[{"role": "user", "content": "hi"}])


def test_chat_network_failure_raises_llm_error():
    client = OllamaClient(
        base_url="http://localhost:11434",
        model="gemma4:e4b-q8",
        transport=_mock(raise_exc=httpx.ConnectError("boom")),
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
