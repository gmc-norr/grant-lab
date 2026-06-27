"""Ollama embedding client — mirrors OllamaClient's ergonomics for the
/api/embed endpoint.

Reuses the shared `LLMError` exception so callers can catch a single
exception type for both chat and embed failures.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import httpx

from call_manager.ranking.ollama_client import LLMError


@dataclass(frozen=True, slots=True)
class EmbedResult:
    """Return value of :meth:`OllamaEmbedClient.embed`.

    ``vector`` — the embedding vector (``expected_dim`` floats).
    ``embedded_chars`` — number of characters from the START of the
      input that the vector actually represents.  Equal to
      ``len(original_text)`` on the happy path; less than that when the
      shrink-and-retry path fired (Ollama 0.21 long-input NaN bug) and
      the text was halved one or more times.

    Callers that store both the text and the vector MUST use
    ``embedded_chars`` to trim the stored text to the embedded prefix;
    otherwise the stored snippet is longer than what the vector encodes,
    which silently degrades RAG retrieval quality.
    """

    vector: list[float]
    embedded_chars: int

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

    def embed(self, text: str, *, _client: httpx.Client | None = None) -> EmbedResult:
        """Embed a single string. Returns an :class:`EmbedResult` whose
        ``vector`` is a ``expected_dim``-length float list (default 1024 —
        bge-m3's native dimension) and whose ``embedded_chars`` records how
        many characters from the START of ``text`` the vector encodes.

        Over-long inputs are halved and retried (Ollama 0.21 fails them as a
        400 'exceeds the context length' or as NaN components); below
        `_MIN_EMBED_CHARS` we give up and raise rather than embed noise.

        When shrinking fires, ``embedded_chars < len(text)`` — callers that
        store the text alongside the vector MUST trim to ``embedded_chars``
        to keep the stored snippet in sync with what the vector represents.

        ``_client`` — for batch callers: pass a persistent :class:`httpx.Client`
        (created via :meth:`session`) to avoid opening a new TCP connection per
        call. The caller is responsible for closing it. For single-call sites,
        omit this argument and a fresh client is opened and closed per attempt."""
        attempt = text
        while True:
            try:
                vector = self._embed_once(attempt, _client=_client)
                return EmbedResult(vector=vector, embedded_chars=len(attempt))
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

    def session(self) -> httpx.Client:
        """Return a persistent :class:`httpx.Client` for batch callers.

        Use as a context manager::

            with embed_client.session() as http:
                for text in batch:
                    result = embed_client.embed(text, _client=http)

        This re-uses the same TCP connection across all calls in the batch,
        avoiding the per-call connection setup overhead. The client is closed
        when the ``with`` block exits.  Single-call sites should just call
        :meth:`embed` without this — it opens and closes its own client.
        """
        return httpx.Client(
            base_url=self._base_url,
            transport=self._transport,
            timeout=self._timeout,
        )

    def _embed_once(self, text: str, *, _client: httpx.Client | None = None) -> list[float]:
        """One embed attempt. Raises `_InputTooLong` for length-related
        failures (retryable by shrinking) and `LLMError` for everything else.

        ``_client`` — optional caller-supplied persistent client (see
        :meth:`session`). When supplied, the client is NOT closed here; the
        caller owns its lifetime. When ``None`` (the default) a fresh client
        is created per attempt, which is safe for single-call sites."""
        try:
            if _client is not None:
                # Persistent-client fast path: caller manages lifetime.
                resp = _client.post(
                    "/api/embed",
                    json={"model": self.model, "input": text},
                )
            else:
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
