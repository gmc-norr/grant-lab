"""Ollama embedding client — mirrors OllamaClient's ergonomics for the
/api/embed endpoint.

Reuses the shared `LLMError` exception so callers can catch a single
exception type for both chat and embed failures.
"""
from __future__ import annotations

import logging

import httpx

from call_monitor.ranking.ollama_client import LLMError

_logger = logging.getLogger(__name__)


class OllamaEmbedClient:
    """POST /api/embed wrapper.

    Ollama returns `{"embeddings": [[...]]}` — always a list-of-lists,
    even for single-string input. `embed()` unwraps the first vector.

    Vendor swap: Ollama's /api/embed is also HTTP+JSON like OpenAI's
    /v1/embeddings, so swapping endpoint URLs + payload key names is
    a small adapter change.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
    ):
        self._base_url = base_url.rstrip("/")
        self.model = model
        self._transport = transport
        self._timeout = timeout

    def embed(self, text: str) -> list[float]:
        """Embed a single string. Returns the 1024-dim float vector."""
        try:
            with httpx.Client(
                base_url=self._base_url,
                transport=self._transport,
                timeout=self._timeout,
            ) as client:
                resp = client.post("/api/embed", json={
                    "model": self.model,
                    "input": text,
                })
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise LLMError(f"ollama embed failed: {e}") from e

        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or not embeddings:
            raise LLMError(f"ollama embed response missing 'embeddings': {data!r}")
        first = embeddings[0]
        if not isinstance(first, list) or not all(isinstance(x, (int, float)) for x in first):
            raise LLMError(f"ollama embed response shape unexpected: {first!r:.200}")
        return [float(x) for x in first]
