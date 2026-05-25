"""Embedding wrapper for references.

Wraps the existing OllamaEmbedClient so ingest.py has a stable
seam and tests can inject a FakeEmbedder without touching Ollama.
"""

from __future__ import annotations

from typing import Protocol


class Embedder(Protocol):
    """Stable interface used by ingest. Pluggable for tests."""

    model: str

    def embed(self, text: str) -> list[float]: ...


class OllamaEmbedderAdapter:
    """Thin shim around the existing OllamaEmbedClient so we can match
    the Embedder protocol without the rest of the codebase importing
    embed clients directly."""

    def __init__(self, client):
        self._client = client
        self.model = getattr(client, "model", "unknown")

    def embed(self, text: str) -> list[float]:
        return self._client.embed(text)


class FakeEmbedder:
    """Test double — returns a deterministic vector based on text length
    so different texts get different vectors but the same text always
    returns the same vector."""

    def __init__(self, model: str = "fake-embedder", dim: int = 1024):
        self.model = model
        self._dim = dim

    def embed(self, text: str) -> list[float]:
        # Seed a small variation based on text length and first char,
        # mostly zeros — keeps the vector valid for pgvector but trivial.
        v = [0.0] * self._dim
        if text:
            v[0] = (len(text) % 1024) / 1024.0
            v[1] = (ord(text[0]) % 256) / 256.0
        return v
