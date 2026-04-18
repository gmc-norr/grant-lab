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


def test_embed_raises_llm_error_when_vector_contains_non_numeric():
    """Vector with strings/None/etc. is rejected — Ollama shouldn't return this,
    but defend against malformed responses."""
    client = _client_returning({"embeddings": [["a", "b", "c"]]})
    with pytest.raises(LLMError):
        client.embed("anything")


def test_embed_raises_llm_error_on_inner_empty_vector():
    """An inner empty list is meaningless as an embedding — reject it."""
    client = _client_returning({"embeddings": [[]]})
    with pytest.raises(LLMError):
        client.embed("anything")


def test_embed_rejects_vector_of_bools():
    """`isinstance(True, int)` is True in Python; a list of bools would
    otherwise sneak past validation. Tighten to require real numerics."""
    client = _client_returning({"embeddings": [[True, False, True, False]]})
    with pytest.raises(LLMError):
        client.embed("anything")


def test_embed_raises_llm_error_on_wrong_dimension():
    """Defensive: if Ollama returns a 768-dim vector when we expected 1024
    (operator pulled bge-base/bge-large by mistake), fail with a clear error
    rather than silently committing a vector that pgvector will reject at
    INSERT time with a far less actionable error message."""
    wrong_dim = [0.5] * 768
    client = _client_returning({"embeddings": [wrong_dim]})
    with pytest.raises(LLMError) as exc:
        client.embed("anything")
    msg = str(exc.value).lower()
    assert "wrong dimension" in msg or "768" in msg


def test_embed_accepts_custom_expected_dim():
    """The dimension check is configurable via the constructor — useful
    when an operator wires the client to a model with a non-1024 native
    dim (e.g. bge-base at 768) without having to monkeypatch."""
    vec = [0.1] * 768
    def handler(_req):
        return httpx.Response(200, json={"embeddings": [vec]})
    client = OllamaEmbedClient(
        base_url="http://x", model="bge-base",
        transport=httpx.MockTransport(handler),
        expected_dim=768,
    )
    out = client.embed("anything")
    assert len(out) == 768
