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

    `num_predict` caps output tokens. The criterion JSON (3 ratings +
    rationales + quotes, plus the optional analysis block) fits well under
    this; the ceiling exists so a degenerate generation (the model looping
    on whitespace/repetition) is bounded by tokens rather than only by the
    120s wall-clock timeout, which would otherwise burn a full slot per
    runaway pair.

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
        num_predict: int = 2048,  # output-token ceiling; bounds runaway generations
    ):
        self._base_url = base_url.rstrip("/")
        self.model = model  # public so rank_pair can fall back to it
        self._transport = transport
        self._timeout = timeout
        self._num_predict = num_predict

    def chat(
        self,
        *,
        messages: list[dict],
        response_format: dict | None = None,
    ) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            # Default to JSON mode; callers pass response_format to override.
            "response_format": (
                response_format if response_format is not None else {"type": "json_object"}
            ),
            # Ollama-native generation knobs. `num_predict` caps output
            # tokens so a degenerate (looping) generation can't run out the
            # full 120s timeout. Ollama reads max-token from options.num_predict
            # on its OpenAI-compatible endpoint.
            "options": {"num_predict": self._num_predict},
        }
        try:
            with httpx.Client(
                base_url=self._base_url,
                transport=self._transport,
                timeout=self._timeout,
            ) as client:
                resp = client.post("/v1/chat/completions", json=payload)
                resp.raise_for_status()
                envelope = resp.json()
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama HTTP error: {e}") from e
        except ValueError as e:  # JSON decode of envelope
            raise LLMError(f"Ollama returned non-JSON envelope: {e}") from e

        # Validate envelope shape before returning. Callers destructure
        # envelope["choices"][0]["message"]["content"] assuming OpenAI
        # semantics; a malformed body (e.g. {"error": "..."}) would crash
        # the rank worker with a bare KeyError/IndexError that isn't LLMError
        # and dead-letters the job with a misleading trace. Fail fast here.
        choices = envelope.get("choices") if isinstance(envelope, dict) else None
        if not isinstance(choices, list) or not choices:
            raise LLMError(f"Ollama envelope missing non-empty 'choices': {envelope!r}")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise LLMError(f"Ollama envelope missing 'message' in first choice: {envelope!r}")
        content = message.get("content")
        if not isinstance(content, str) or not content:
            raise LLMError(f"Ollama envelope missing non-empty message.content: {envelope!r}")
        return envelope
