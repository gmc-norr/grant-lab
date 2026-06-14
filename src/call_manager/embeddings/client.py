"""Ollama embedding client — mirrors OllamaClient's ergonomics for the
/api/embed endpoint.

Reuses the shared `LLMError` exception so callers can catch a single
exception type for both chat and embed failures.
"""

from __future__ import annotations

import logging
import math

import httpx

from call_manager.ranking.ollama_client import LLMError

_logger = logging.getLogger(__name__)

# Below this length we stop shrinking and fail loud: a sub-paragraph that still
# won't embed is a real error, not a length problem worth retrying.
_MIN_EMBED_CHARS = 256


class _InputTooLong(Exception):
    """Internal sentinel: an embed attempt failed in a way that *shrinking the
    input* may fix. Ollama 0.21 fails over-long inputs two ways — a 400
    'exceeds the context length' (mxbai-embed-large) or NaN components (bge-m3)
    — both length-related. Caught by embed()'s retry loop; never escapes here.
    """


def _is_length_error(body: str) -> bool:
    b = body.lower()
    return "context length" in b or ("length" in b and "exceed" in b)


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
        expected_dim: int = 1024,
    ):
        self._base_url = base_url.rstrip("/")
        self.model = model
        self._transport = transport
        self._timeout = timeout
        self._expected_dim = expected_dim

    def embed(self, text: str) -> list[float]:
        """Embed a single string. Returns a `expected_dim`-length float vector
        (default 1024 — bge-m3's native dimension).

        Over-long inputs are halved and retried (Ollama 0.21 fails them as a
        400 'exceeds the context length' or as NaN components); below
        `_MIN_EMBED_CHARS` we give up and raise rather than embed noise."""
        attempt = text
        while True:
            try:
                return self._embed_once(attempt)
            except _InputTooLong as e:
                if len(attempt) <= _MIN_EMBED_CHARS:
                    raise LLMError(
                        f"ollama embed failed even at {len(attempt)} chars "
                        f"(input too long for model {self.model!r}): {e}"
                    ) from e
                shrunk = len(attempt) // 2
                _logger.warning(
                    "embedding input too long (%d chars) for model %s; retrying at %d",
                    len(attempt),
                    self.model,
                    shrunk,
                    extra={"event": "embed_input_shrink"},
                )
                attempt = attempt[:shrunk]

    def _embed_once(self, text: str) -> list[float]:
        """One embed attempt. Raises `_InputTooLong` for length-related
        failures (retryable by shrinking) and `LLMError` for everything else."""
        try:
            with httpx.Client(
                base_url=self._base_url,
                transport=self._transport,
                timeout=self._timeout,
            ) as client:
                resp = client.post(
                    "/api/embed",
                    json={
                        "model": self.model,
                        "input": text,
                    },
                )
                if resp.status_code == 400 and _is_length_error(resp.text):
                    raise _InputTooLong(resp.text.strip()[:200])
                resp.raise_for_status()
                data = resp.json()
        except _InputTooLong:
            raise
        except (httpx.HTTPError, ValueError) as e:
            raise LLMError(f"ollama embed failed: {e}") from e

        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or not embeddings:
            raise LLMError(f"ollama embed response missing 'embeddings': {data!r}")
        first = embeddings[0]
        if (
            not isinstance(first, list)
            or not first
            or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in first)
        ):
            raise LLMError(f"ollama embed response shape unexpected: {repr(first)[:200]}")
        # Defend against a wrong-model install: e.g. operator pulled bge-large
        # (1024-dim oh wait that's also 1024 — the real risk is bge-base/768
        # or a different family) when we expected bge-m3 / 1024. Without this
        # check, the failure surfaces only at DB commit time as an opaque
        # "pgvector dimension mismatch" error far from the root cause.
        if len(first) != self._expected_dim:
            raise LLMError(
                f"ollama embed returned wrong dimension: got {len(first)}, "
                f"expected {self._expected_dim}. Wrong model installed?"
            )
        # Reject NaN/Inf: Ollama (0.21) returns NaN components for bge-m3 on
        # long inputs. A non-finite vector would poison every cosine() it
        # touches (na/nb → NaN → comparisons silently false) and pgvector
        # stores it without complaint. Fail loud at the source instead.
        vec = [float(x) for x in first]
        if not all(math.isfinite(x) for x in vec):
            # Non-finite components are Ollama 0.21's long-input NaN bug — a
            # length problem, so signal a retry (shrink) rather than store
            # noise. If it persists below the floor, embed() raises LLMError.
            raise _InputTooLong("non-finite (NaN/Inf) components — long-input NaN bug")
        return vec
