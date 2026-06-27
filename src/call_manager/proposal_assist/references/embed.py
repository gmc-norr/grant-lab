"""Embedding wrapper for references.

Wraps the existing OllamaEmbedClient so ingest.py has a stable
seam and tests can inject a FakeEmbedder without touching Ollama.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from call_manager.embeddings.client import EmbedResult

if TYPE_CHECKING:
    pass


class Embedder(Protocol):
    """Stable interface used by ingest. Pluggable for tests.

    :meth:`embed` returns an :class:`~call_manager.embeddings.client.EmbedResult`
    whose ``embedded_chars`` field tells ingest how many characters from the
    start of ``text`` the returned vector actually represents.  Ingest trims
    the stored chunk text to that length to keep the stored snippet in sync
    with the vector (Defect-1 fix).
    """

    model: str

    def embed(self, text: str) -> EmbedResult: ...


class OllamaEmbedderAdapter:
    """Thin shim around the existing OllamaEmbedClient so we can match
    the Embedder protocol without the rest of the codebase importing
    embed clients directly."""

    def __init__(self, client):
        self._client = client
        self.model = getattr(client, "model", "unknown")

    def embed(self, text: str) -> EmbedResult:
        return self._client.embed(text)


class FakeEmbedder:
    """Test double — returns a deterministic :class:`EmbedResult` based on
    text length so different texts get different vectors but the same text
    always returns the same vector.  Never truncates (embedded_chars ==
    len(text)) — tests that need truncation behaviour should use a custom
    stub that returns embedded_chars < len(text)."""

    def __init__(self, model: str = "fake-embedder", dim: int = 1024):
        self.model = model
        self._dim = dim

    def embed(self, text: str) -> EmbedResult:
        # Seed a small variation based on text length and first char,
        # mostly zeros — keeps the vector valid for pgvector but trivial.
        v = [0.0] * self._dim
        if text:
            v[0] = (len(text) % 1024) / 1024.0
            v[1] = (ord(text[0]) % 256) / 256.0
        return EmbedResult(vector=v, embedded_chars=len(text))
