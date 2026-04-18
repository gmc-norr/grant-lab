from __future__ import annotations

import httpx
import pytest

from call_monitor.embeddings.client import OllamaEmbedClient
from call_monitor.ranking.ollama_client import LLMError


def _client_returning(payload: dict, *, status: int = 200) -> OllamaEmbedClient:
    def handler(_req):
        return httpx.Response(status, json=payload)
    return OllamaEmbedClient(
        base_url="http://x", model="bge-m3",
        transport=httpx.MockTransport(handler),
    )


def test_embed_returns_vector_for_single_input():
    vec = [0.1] * 1024
    client = _client_returning({"embeddings": [vec]})
    out = client.embed("hello world")
    assert out == vec
    assert len(out) == 1024


def test_embed_returns_first_vector_when_multiple_returned():
    """Ollama always returns a list-of-lists even for single input;
    the helper unwraps the first element."""
    a = [0.1] * 1024
    b = [0.2] * 1024
    client = _client_returning({"embeddings": [a, b]})
    out = client.embed("anything")
    assert out == a


def test_embed_raises_llm_error_on_http_failure():
    client = _client_returning({"error": "model not loaded"}, status=500)
    with pytest.raises(LLMError):
        client.embed("anything")


def test_embed_raises_llm_error_when_response_lacks_embeddings_key():
    client = _client_returning({"unexpected": "shape"})
    with pytest.raises(LLMError):
        client.embed("anything")


def test_embed_raises_llm_error_on_empty_embeddings_list():
    client = _client_returning({"embeddings": []})
    with pytest.raises(LLMError):
        client.embed("anything")
