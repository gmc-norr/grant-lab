from __future__ import annotations

import json

import httpx
import pytest

from call_manager.embeddings.client import EmbedResult, OllamaEmbedClient
from call_manager.ranking.ollama_client import LLMError


def _client_returning(payload: dict, *, status: int = 200) -> OllamaEmbedClient:
    def handler(_req):
        return httpx.Response(status, json=payload)

    return OllamaEmbedClient(
        base_url="http://x",
        model="bge-m3",
        transport=httpx.MockTransport(handler),
    )


def test_embed_returns_vector_for_single_input():
    vec = [0.1] * 1024
    client = _client_returning({"embeddings": [vec]})
    text = "hello world"
    result = client.embed(text)
    assert isinstance(result, EmbedResult)
    assert result.vector == vec
    assert len(result.vector) == 1024
    assert result.embedded_chars == len(text)


def test_embed_returns_first_vector_when_multiple_returned():
    """Ollama always returns a list-of-lists even for single input;
    the helper unwraps the first element."""
    a = [0.1] * 1024
    b = [0.2] * 1024
    client = _client_returning({"embeddings": [a, b]})
    result = client.embed("anything")
    assert isinstance(result, EmbedResult)
    assert result.vector == a


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


def test_embed_rejects_nan_vector():
    """Ollama (0.21) returns NaN components for bge-m3 on long inputs. A
    non-finite vector poisons every cosine() it touches and pgvector stores
    it silently — reject at the source with LLMError."""
    vec = [0.1] * 1023 + [float("nan")]
    client = _client_returning({"embeddings": [vec]})
    with pytest.raises(LLMError):
        client.embed("a very long input that triggers the NaN bug")


def test_embed_rejects_inf_vector():
    """Infinity is equally non-finite — also rejected."""
    vec = [0.1] * 1023 + [float("inf")]
    client = _client_returning({"embeddings": [vec]})
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
        base_url="http://x",
        model="bge-base",
        transport=httpx.MockTransport(handler),
        expected_dim=768,
    )
    result = client.embed("anything")
    assert isinstance(result, EmbedResult)
    assert len(result.vector) == 768


# --------------------------------------------------------------------------
# Shrink-and-retry on over-long inputs. Ollama 0.21 fails over-long inputs two
# ways: a 400 "exceeds the context length" (mxbai-embed-large) or NaN
# components (bge-m3). Both are length problems, so embed() halves the input
# and retries down to a floor rather than hard-failing the whole reference.
# --------------------------------------------------------------------------


def _input_len(req: httpx.Request) -> int:
    return len(json.loads(req.content.decode())["input"])


def _len_error_400() -> httpx.Response:
    return httpx.Response(400, json={"error": "the input length exceeds the context length"})


def _ok(vec: list[float]) -> httpx.Response:
    return httpx.Response(200, json={"embeddings": [vec]})


def _nan_ok(vec: list[float]) -> httpx.Response:
    # httpx's json= encoder rejects NaN, but Ollama really does emit a literal
    # NaN, which json.loads (resp.json()) parses — so build the body by hand.
    body = json.dumps({"embeddings": [vec]}).encode()
    return httpx.Response(200, content=body, headers={"content-type": "application/json"})


def test_embed_shrinks_and_retries_on_context_length_400():
    vec = [0.1] * 1024
    seen: list[int] = []

    def handler(req):
        n = _input_len(req)
        seen.append(n)
        return _len_error_400() if n > 1000 else _ok(vec)

    client = OllamaEmbedClient(
        base_url="http://x", model="mxbai-embed-large", transport=httpx.MockTransport(handler)
    )
    text = "z" * 2500
    result = client.embed(text)
    assert isinstance(result, EmbedResult)
    assert result.vector == vec
    assert len(seen) >= 2  # retried at least once
    assert seen[-1] <= 1000  # the accepted attempt fit the context window
    # embedded_chars must reflect the truncated length, not the original
    assert result.embedded_chars < len(text)
    assert result.embedded_chars <= 1000


def test_embed_retries_on_nonfinite_for_long_input():
    """The bge-m3 NaN-on-long-input bug is also length-related, so it's
    retried by shrinking — not an immediate hard fail."""
    good = [0.1] * 1024
    bad = [0.1] * 1023 + [float("nan")]

    def handler(req):
        return _nan_ok(bad) if _input_len(req) > 1000 else _ok(good)

    client = OllamaEmbedClient(
        base_url="http://x", model="bge-m3", transport=httpx.MockTransport(handler)
    )
    text = "z" * 2500
    result = client.embed(text)
    assert isinstance(result, EmbedResult)
    assert result.vector == good
    # Truncation occurred: embedded_chars < original length
    assert result.embedded_chars < len(text)


def test_embed_gives_up_when_length_error_persists():
    """If even a short input still 400s, stop shrinking and raise — a
    sub-paragraph that won't embed is a real error, not a length problem."""
    calls: list[int] = []

    def handler(req):
        calls.append(_input_len(req))
        return _len_error_400()

    client = OllamaEmbedClient(
        base_url="http://x", model="mxbai-embed-large", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(LLMError):
        client.embed("z" * 2000)
    assert len(calls) >= 2  # it shrank and retried before giving up


def test_embed_does_not_retry_on_non_length_400():
    """A 400 that isn't about length (e.g. unknown model) is a real error —
    fail immediately, don't waste attempts shrinking."""
    calls: list[int] = []

    def handler(req):
        calls.append(_input_len(req))
        return httpx.Response(400, json={"error": "model 'foo' not found"})

    client = OllamaEmbedClient(
        base_url="http://x", model="foo", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(LLMError):
        client.embed("z" * 2000)
    assert len(calls) == 1  # not retried
