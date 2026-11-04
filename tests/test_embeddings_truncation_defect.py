"""OllamaEmbedClient.embed() returns EmbedResult; after a shrink-and-retry,
embedded_chars is the truncated length."""

from __future__ import annotations

import json

import httpx

from grant_lab.embeddings.client import EmbedResult, OllamaEmbedClient

# ---------------------------------------------------------------------------
# OllamaEmbedClient.embed() — EmbedResult shape
# ---------------------------------------------------------------------------


def _client_returning(payload: dict, *, status: int = 200) -> OllamaEmbedClient:
    def handler(_req):
        return httpx.Response(status, json=payload)

    return OllamaEmbedClient(
        base_url="http://x",
        model="bge-m3",
        transport=httpx.MockTransport(handler),
    )


def test_embed_returns_embed_result_with_full_chars_on_success():
    """When no truncation occurs, embedded_chars == len(original text)."""
    vec = [0.1] * 1024
    client = _client_returning({"embeddings": [vec]})
    text = "short input"
    result = client.embed(text)
    assert isinstance(result, EmbedResult)
    assert result.vector == vec
    assert result.embedded_chars == len(text)


def test_embed_returns_reduced_embedded_chars_after_shrink():
    """After a shrink-and-retry, embedded_chars reflects the truncated length."""
    good = [0.1] * 1024
    bad_body = json.dumps({"embeddings": [[0.1] * 1023 + [float("nan")]]}).encode()

    # Inputs >1000 chars get NaN, shorter get good vector.
    def handler(req):
        body = json.loads(req.content.decode())
        n = len(body["input"])
        if n > 1000:
            return httpx.Response(
                200,
                content=bad_body,
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, json={"embeddings": [good]})

    client = OllamaEmbedClient(
        base_url="http://x",
        model="bge-m3",
        transport=httpx.MockTransport(handler),
    )
    text = "z" * 2500
    result = client.embed(text)
    assert isinstance(result, EmbedResult)
    assert result.vector == good
    # embedded_chars must be less than the original length (truncation occurred)
    assert result.embedded_chars < len(text)
    # and must equal the final accepted length (<=1000 since that's where good starts)
    assert result.embedded_chars <= 1000


def test_embed_no_shrink_embedded_chars_equals_input_length():
    """Happy path — no shrink — embedded_chars == len(text)."""
    vec = [0.5] * 1024
    text = "Hello, embedding world!"
    client = _client_returning({"embeddings": [vec]})
    result = client.embed(text)
    assert result.embedded_chars == len(text)
