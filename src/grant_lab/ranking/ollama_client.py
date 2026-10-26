from __future__ import annotations

import logging

import httpx

from grant_lab.ranking.prompt import ParseError

_logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Wraps any failure from the LLM endpoint — network, HTTP, parse —
    so the ranking job processor can retry/backoff uniformly."""


class LocalUnusableReplyError(LLMError, ParseError):
    """The local model answered, but not with a usable reply: its thinking
    filled the context window and `content` came back empty, or generation
    stopped at the token ceiling mid-JSON. Retrying the same inputs at
    temperature 0 gets the same non-answer.

    Both parents on purpose, and the second one is the whole point.
    worker.drain_queue writes failure_kind='parse' for a ParseError and
    'service' for a bare LLMError, and freshness.enqueue_stale_pairs suppresses
    only a `parse` failure on unchanged inputs -- a `service` one never. So as
    a plain LLMError this failure was re-enqueued by every ingest and every
    04:00 reconcile, burning up to five Gemma runs a night per pair: review
    2026-10-26 H2 measured 541 failed jobs and 2,705 wasted calls on 15 pairs
    since the Ollama 0.35.1 upgrade, while 13 of those pairs kept displaying a
    pre-upgrade score. Every other caller of chat() catches LLMError or
    Exception, so the first parent leaves them unchanged.

    anthropic_client.UnusableReplyError is the same shape for the remote side;
    keep the two in step.
    """


def _thinking(message: dict) -> str:
    """The chain of thought Gemma 4 emits alongside (or instead of) content.

    Ollama 0.35.1 spells it `reasoning` on the OpenAI-compatible endpoint;
    `reasoning_content` is the other spelling in that ecosystem. Either one
    present with an empty `content` is the measured context-window overflow,
    not a malformed envelope."""
    for key in ("reasoning", "reasoning_content"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _cut_off_reply_message(
    *, envelope: dict, finish_reason, content, thinking: str, num_predict: int
) -> str:
    """What an operator needs from `ranking_jobs.last_error` alone: what was
    observed, then the likely cause and which knob to raise.

    The first clause REPORTS rather than diagnoses, on purpose. One reachable
    cell is "empty content + thinking + a clean `stop` at small token counts" --
    a model that emitted only thinking and no answer. Asserting "hit the
    context/output limit" there would print "prompt 100 + output 5 tokens hit
    the model's limit" and send the operator to the wrong knob. The
    classification is right in that cell either way; only the diagnosis would
    have been wrong."""
    usage = envelope.get("usage")
    usage = usage if isinstance(usage, dict) else {}

    def _n(value) -> str:
        return str(value) if isinstance(value, int) else "?"

    return (
        f"Ollama returned no usable reply: prompt {_n(usage.get('prompt_tokens'))} "
        f"+ output {_n(usage.get('completion_tokens'))} tokens "
        f"(finish_reason={finish_reason!r}, content "
        f"{len(content) if isinstance(content, str) else 0} chars, thinking "
        f"{len(thinking)} chars, num_predict={num_predict}). Most likely the "
        "reply hit the model's context/output limit: raise "
        "OLLAMA_CONTEXT_LENGTH on the Ollama service -- since 0.35.1 Gemma's "
        "thinking has to fit in the same context window as the prompt -- or "
        "raise num_predict if the reply itself is longer than the ceiling. If "
        "the token counts above are small, the model emitted only thinking and "
        "no answer, and the window is not the problem. Either way, retrying "
        "the same inputs cannot help."
    )


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
        logprobs: bool = False,
        top_logprobs: int | None = None,
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
        if logprobs:
            payload["logprobs"] = True
            if top_logprobs is not None:
                payload["top_logprobs"] = top_logprobs
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
        finish_reason = choices[0].get("finish_reason")
        thinking = _thinking(message)
        usable = isinstance(content, str) and bool(content)

        # A reply the model never finished (review 2026-10-26 H2). Two shapes,
        # one measured cause: the coordinator replayed a failing 12,127-char
        # call and got prompt 3,168 + output 928 = 4,096 tokens — Ollama's
        # DEFAULT context window — at num_predict 2048 AND 8192 alike. So
        # either the thinking fills the window and `content` arrives empty
        # (336 of the 541 live failures), or generation stops at the ceiling
        # with a partial, unparseable JSON fragment (the other 205). Letting
        # either flow on would fail parse_response and dead-letter on a
        # misleading trace.
        #
        # Both recur identically for the same inputs at temperature 0, so this
        # raises a ParseError (via LocalUnusableReplyError) and NOT a bare
        # LLMError: the worker writes failure_kind='parse', which
        # enqueue_stale_pairs suppresses while the inputs are unchanged, and a
        # corrected call is still retried at once. A bare LLMError reads as a
        # service outage and is re-enqueued every night forever.
        #
        # The cure is OLLAMA_CONTEXT_LENGTH on the ollama.service drop-in, not
        # a code change: prompt.CALL_TEXT_LIMIT (16,000 chars) bounds the
        # prompt, so an 8,192-token window covers prompt plus thinking.
        if finish_reason == "length" or (not usable and thinking):
            raise LocalUnusableReplyError(
                _cut_off_reply_message(
                    envelope=envelope,
                    finish_reason=finish_reason,
                    content=content,
                    thinking=thinking,
                    num_predict=self._num_predict,
                )
            )
        # No content AND no thinking: a malformed envelope, not the overflow
        # above. Deliberately still a bare LLMError -> `service` -> retried. A
        # genuine Ollama malfunction must not silence its pairs for the seven
        # days freshness.DEAD_LETTER_SUPPRESS_DAYS suppresses a parse failure.
        if not usable:
            raise LLMError(f"Ollama envelope missing non-empty message.content: {envelope!r}")
        return envelope
