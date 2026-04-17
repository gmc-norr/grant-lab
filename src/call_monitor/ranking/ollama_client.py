from __future__ import annotations

import logging

import httpx

_logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Wraps any failure from the LLM endpoint — network, HTTP, parse —
    so the ranking job processor can retry/backoff uniformly."""


class OllamaClient:
    """Thin HTTP client for Ollama's OpenAI-compatible /v1/chat/completions.

    Returns the raw OpenAI ChatCompletion dict; the caller is responsible
    for extracting and parsing the assistant message's JSON content. We
    keep the raw response so `call_rankings.raw_response` retains the full
    payload for debugging (per spec 'LLM reliability').

    `temperature=0` is hardcoded — the spec mandates it for run-to-run
    repeatability. `response_format={"type": "json_object"}` requests JSON
    mode; combined with Gemma 4's native function-calling/JSON support,
    this near-eliminates parse failures.

    Vendor swap: pointing this at OpenAI/Anthropic-compatible endpoints
    (Together, Groq, etc.) is a `base_url` + `model` config change.

    PERF NOTE (deferred): we open a fresh `httpx.Client` per `chat()`
    call. For Ollama on localhost the TCP handshake is sub-millisecond,
    so the per-call cost is negligible at the daily-ingest scale Plan 3
    targets. Lifting to a persistent `httpx.Client` instance attribute
    is straightforward when we either (a) move to a remote LLM or
    (b) need to ranking thousands of pairs in tight bursts. Doing it
    now would force test-time lifecycle plumbing (close on shutdown)
    that we'd rather not pay for the marginal benefit.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 120.0,  # 120s: cold model load on 2080 Ti can take 30-60s
    ):
        self._base_url = base_url.rstrip("/")
        self.model = model  # public so rank_pair can fall back to it
        self._transport = transport
        self._timeout = timeout

    def chat(self, *, messages: list[dict]) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        try:
            with httpx.Client(
                base_url=self._base_url,
                transport=self._transport,
                timeout=self._timeout,
            ) as client:
                resp = client.post("/v1/chat/completions", json=payload)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama HTTP error: {e}") from e
        except ValueError as e:  # JSON decode of envelope
            raise LLMError(f"Ollama returned non-JSON envelope: {e}") from e
