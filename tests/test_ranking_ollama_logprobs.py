import httpx

from call_monitor.ranking.ollama_client import OllamaClient


def _mock(payload):
    def handler(request):
        body = request.read().decode("utf-8")
        # Validate request shape
        import json as _json
        req = _json.loads(body)
        assert req.get("logprobs") is True
        assert req.get("top_logprobs") == 6
        return httpx.Response(200, json=payload)
    return httpx.MockTransport(handler)


def test_chat_with_logprobs_returns_logprob_payload():
    """When logprobs=True, the OpenAI-shape response includes choices[0].logprobs.content."""
    body = {
        "model": "gemma4:e4b-it-q4_K_M",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": '{"topic_fit": {"rating": 4}}'},
            "logprobs": {
                "content": [
                    {"token": "4", "logprob": -0.1, "top_logprobs": [
                        {"token": "4", "logprob": -0.1},
                        {"token": "3", "logprob": -1.5},
                        {"token": "5", "logprob": -2.0},
                    ]},
                ]
            },
        }],
    }
    client = OllamaClient(
        base_url="http://x", model="gemma4:e4b-it-q4_K_M",
        transport=_mock(body),
    )
    envelope = client.chat(
        messages=[{"role": "user", "content": "x"}],
        logprobs=True, top_logprobs=6,
    )
    assert envelope["choices"][0]["logprobs"]["content"][0]["token"] == "4"


def test_chat_without_logprobs_omits_param():
    """Default behavior — no logprobs param sent — keeps backward compatibility."""
    captured = {}
    def handler(request):
        import json as _json
        captured["body"] = _json.loads(request.read().decode("utf-8"))
        return httpx.Response(200, json={
            "model": "gemma4:e4b-it-q4_K_M",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        })
    client = OllamaClient(
        base_url="http://x", model="gemma4:e4b-it-q4_K_M",
        transport=httpx.MockTransport(handler),
    )
    client.chat(messages=[{"role": "user", "content": "x"}])
    assert "logprobs" not in captured["body"]
    assert "top_logprobs" not in captured["body"]
