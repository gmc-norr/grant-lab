import json

import httpx

from call_manager.ranking.ollama_client import OllamaClient


def test_logprobs_flags_flow_into_payload():
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "{}"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    client = OllamaClient(base_url="http://x", model="m", transport=httpx.MockTransport(handler))
    client.chat(messages=[{"role": "user", "content": "hi"}], logprobs=True, top_logprobs=10)
    assert captured["logprobs"] is True
    assert captured["top_logprobs"] == 10


def test_logprobs_absent_by_default():
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "{}"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    client = OllamaClient(base_url="http://x", model="m", transport=httpx.MockTransport(handler))
    client.chat(messages=[{"role": "user", "content": "hi"}])
    assert "logprobs" not in captured
    assert "top_logprobs" not in captured
