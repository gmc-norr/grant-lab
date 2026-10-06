"""Anthropic-hosted ranking client — drop-in replacement for OllamaClient.

When the local Gemma result clears the escalation bar (score ≥
RANKING_ESCALATION_MIN_SCORE, or an eligibility-fail on a high-topic call),
rank_pair re-runs the LLM step here and the remote result replaces the local
one. This buys a stronger eligibility read + applicant-facing analysis on the
calls most likely to surface to a user.

API surface mirrors OllamaClient.chat()'s envelope shape
(`{"choices": [{"message": {"content": ...}}]}`), so rank_pair's downstream
parsing is reused without changes. The logprobs kwargs are Ollama-only and are
not present here.

Prompt caching: the system prompt carries the criteria + profile rubric
(stable across every (call, profile) pair scored against the same
profile within a 5-minute window). A `cache_control` breakpoint on the
last system block lets repeat ranks for the same profile read from
cache at ~10% of the input cost.
"""

from __future__ import annotations

import json
import logging
import re

import anthropic
import httpx

from grant_lab.ranking.ollama_client import LLMError
from grant_lab.ranking.prompt import ParseError

_logger = logging.getLogger(__name__)


class UnusableReplyError(LLMError, ParseError):
    """The model answered, but not with a usable reply: it refused, or it hit
    max_tokens mid-reply. Retrying the same inputs gets the same answer.

    Both parents on purpose. rank_pair reads a ParseError as `parse_error`,
    which counts toward an escalation's gave_up (spec D6); as a plain LLMError
    it was `llm_error`, owed again every night until the call closed. Every
    other caller catches Exception, so the second parent changes nothing for
    them, and a refusal raises this only on a schema request."""


class AnthropicTimeoutError(LLMError, TimeoutError):
    """The client gave up waiting for a reply to a request it had sent. The
    server may still have finished, and billed, the reply, so callers that
    record spend mark it `timeout` rather than a free failure (compaction counts
    it toward its retry limit; citations' `except TimeoutError` branch records
    it). Note TimeoutError is an OSError: an `except OSError` around a chat()
    call would catch this too."""


# httpx timeouts that fire before the request reached the server.
_UNSENT_TIMEOUTS = (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.WriteTimeout)


class AnthropicClient:
    """Thin wrapper for anthropic.messages.create() that returns the
    OpenAI-compatible envelope shape OllamaClient produces.

    Initialize once per process (the SDK reuses an HTTP connection pool
    internally). The `model` attribute is public so rank_pair can stamp
    it onto CallRanking.model when this client was used.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "claude-sonnet-5-5",
        max_tokens: int = 4096,
        timeout: float = 120.0,
        max_retries: int = 2,
    ):
        # api_key kept off the instance so repr() doesn't accidentally
        # echo it; the SDK closes over it.
        # max_retries is the SDK's own (default 2). A caller that already
        # retries on a schedule (compaction's tick) passes 0, or each billed
        # attempt becomes three.
        self._client = anthropic.Anthropic(
            api_key=api_key, timeout=timeout, max_retries=max_retries
        )
        self.model = model
        self._max_tokens = max_tokens

    def chat(
        self,
        *,
        messages: list[dict],
        response_format: dict | None = None,
    ) -> dict:
        """Run a ranking turn. Returns the OpenAI ChatCompletion envelope.

        `messages` is OpenAI-shaped: a list of `{role, content}` dicts
        where `role` is one of "system", "user", "assistant". The first
        system message is extracted into Anthropic's top-level `system`
        param (which is where Anthropic wants it); the rest become the
        `messages` array.

        Output: the assistant's first text block is unwrapped and packed
        into `{"choices": [{"message": {"role": "assistant", "content":
        <str>}}], "model": self.model}` — the shape `rank_pair` already
        knows how to destructure.

        `response_format` takes OllamaClient's (OpenAI) shape. A
        `json_schema` one is sent as structured outputs (`output_config`),
        so the API guarantees JSON matching it; the remote ranker sends
        prompt.rank_response_format(). Anything else (`json_object`, None)
        sends nothing: Anthropic has no plain JSON mode, and proposal-assist
        and citations share this client without a schema.
        """
        system_text, user_msgs = _split_system(messages)

        try:
            # cache_control on the last system block — captures the
            # criteria + per-profile rubric, which is stable across every
            # (call, profile) pair scored against this profile in the
            # next 5 minutes. The user turn (with the variable call body)
            # falls after the breakpoint and isn't cached.
            system_blocks = (
                [
                    {
                        "type": "text",
                        "text": system_text,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                if system_text
                else []
            )

            output_config = _output_config(response_format)
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                system=system_blocks,
                messages=user_msgs,
                **({"output_config": output_config} if output_config else {}),
            )
        except anthropic.APIStatusError as e:
            # Wrap into LLMError so the worker's existing retry/dead-letter
            # logic catches it the same as a Gemma failure. Carry the
            # request_id when present — invaluable for support tickets.
            req_id = getattr(e.response, "headers", {}).get("request-id", "")
            raise LLMError(f"anthropic {e.status_code} (request_id={req_id}): {e.message}") from e
        except anthropic.APITimeoutError as e:
            # Before APIConnectionError, which it subclasses. Only a timeout
            # WAITING FOR THE REPLY may have been billed; a connect, pool or
            # write timeout never delivered the request, so it is an ordinary
            # connection error (counting it would let an outage stop
            # compaction for a day).
            if isinstance(e.__cause__, _UNSENT_TIMEOUTS):
                raise LLMError(f"anthropic connection timeout: {e}") from e
            raise AnthropicTimeoutError(f"anthropic timeout: {e}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"anthropic connection error: {e}") from e
        except anthropic.APIError as e:
            # Catch-all for APIError subclasses not covered above — e.g.
            # APIResponseValidationError derives directly from APIError and
            # would otherwise escape the LLMError wrap, breaking rank_pair's
            # keep-local escalation contract (remote failures must never
            # propagate) and failing the whole worker job.
            raise LLMError(f"anthropic API error ({type(e).__name__}): {e}") from e

        # A refusal or a max_tokens truncation: `content` is no usable reply
        # (a truncated one is a partial JSON fragment). Both are deterministic
        # enough for these inputs that retrying cannot help, so raise
        # UnusableReplyError before extracting text: rank_pair counts it as a
        # parse_error toward gave_up. The fix for a persistently too-long reply
        # is to raise max_tokens, not to keep re-billing the same call.
        # Refusal only on a schema request (the ranker): callers without one
        # (compaction, citations, extractors) keep getting the text back.
        #
        # Every raise from here on is AFTER Anthropic billed the call, so each
        # carries the usage (_billed): a caller that records spend on failure
        # (compaction) would otherwise store 0 and the global cap never see it.
        if output_config and response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise _billed(
                UnusableReplyError(
                    f"anthropic refused (category={getattr(details, 'category', None)})"
                ),
                response,
            )
        if response.stop_reason == "max_tokens":
            raise _billed(
                UnusableReplyError(
                    f"anthropic response truncated at max_tokens={self._max_tokens} "
                    f"(output_tokens={response.usage.output_tokens}); "
                    "raise AnthropicClient max_tokens — retrying cannot help"
                ),
                response,
            )

        # Extract first text block. Tool calls / thinking blocks shouldn't
        # appear here (we don't request them), but guard anyway.
        text_blocks = [b for b in response.content if b.type == "text"]
        if not text_blocks:
            raise _billed(
                LLMError(
                    f"anthropic response had no text content blocks "
                    f"(blocks: {[b.type for b in response.content]})"
                ),
                response,
            )
        # A structured-output reply is the JSON itself; the fence stripper could
        # only cut a valid one (a ``` pair inside its prose).
        text = text_blocks[0].text
        content = text if output_config else _strip_markdown_fence(text)

        return {
            "model": response.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": _map_stop_reason(response.stop_reason),
                }
            ],
            "usage": _usage(response),
        }


def _usage(response) -> dict:
    """The envelope's `usage`, in OllamaClient's (OpenAI) key names."""
    return {
        "prompt_tokens": response.usage.input_tokens,
        "completion_tokens": response.usage.output_tokens,
        "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
        "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
    }


def _billed(exc: LLMError, response) -> LLMError:
    """Attach the billed usage to an error raised after the API answered."""
    exc.usage = _usage(response)
    return exc


def _output_config(response_format: dict | None) -> dict | None:
    """An OpenAI `json_schema` response_format as Anthropic structured
    outputs; None for any other format."""
    if not response_format or response_format.get("type") != "json_schema":
        return None
    return {"format": {"type": "json_schema", "schema": response_format["json_schema"]["schema"]}}


def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """Pull the first system message into a plain string, return the rest.

    Anthropic accepts only user/assistant in `messages`. Multiple system
    messages get joined with a newline — matches how OpenAI clients
    typically build them, but we don't ship that pattern today.
    """
    system_parts: list[str] = []
    rest: list[dict] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content", "")
        if role == "system":
            if isinstance(content, str):
                system_parts.append(content)
            else:
                system_parts.append(
                    "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
                )
        else:
            # Anthropic accepts content as a string for plain text turns.
            rest.append({"role": role, "content": content})
    return "\n\n".join(system_parts), rest


def _map_stop_reason(anthropic_stop: str | None) -> str:
    """Map Anthropic's stop_reason vocabulary onto OpenAI's so any
    downstream tooling that inspects finish_reason continues to work."""
    return {
        "end_turn": "stop",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
        "refusal": "content_filter",
        "pause_turn": "stop",
    }.get(anthropic_stop or "", "stop")


# Whole-response fence: the entire (stripped) reply is a single ```...``` block.
_FENCE_WHOLE_RE = re.compile(
    r"\A\s*```(?:json|JSON)?\s*\n?(?P<body>.*?)\n?```\s*\Z",
    re.DOTALL,
)
# Embedded fence: a ```json ... ``` block sitting anywhere inside prose
# ("Here is the JSON:\n```json\n{...}\n```"). Non-greedy body so each complete
# block is captured separately; we iterate ALL of them (see below).
_FENCE_EMBEDDED_RE = re.compile(
    r"```(?:json|JSON)?\s*\n?(?P<body>.*?)```",
    re.DOTALL,
)


def _strip_markdown_fence(text: str) -> str:
    """Anthropic's instruction-tuned models often wrap structured output
    in ```json ... ``` fences, even when the prompt explicitly asks for
    raw JSON. The downstream parser uses `json.loads()` directly and
    fails on the fence prefix ("Expecting value: line 1 column 1").

    Recovery, in order:
      1. The whole reply is one fence → return its body.
      2. One or more fenced blocks sit inside prose → return the body of the
         LAST block that parses as JSON. Taking the FIRST block (B11 Defect 4b)
         could grab a pre-JSON EXAMPLE fence ("Here is the format I'll use:
         ```json{...}``` ... my answer: ```json{...}```"), discarding the real
         answer and dead-lettering a valid Sonnet reply through five paid
         retries. Preferring the last parseable block keeps the actual answer.
         If no block parses as JSON, fall back to the last block's body (the
         prior behaviour for a single non-JSON fence) so the parser still gets
         a chance and produces its own error.
    A response that's already raw JSON (no fence) is returned unchanged.
    """
    if not text:
        return text
    stripped = text.strip()
    # Enumerate every fenced block first. When several exist, prefer the LAST
    # one that parses as JSON — this both recovers the real answer past a
    # pre-JSON example fence AND avoids the whole-reply regex swallowing two
    # blocks as one (its \Z anchor would span from the first ``` to the last).
    bodies = [mt.group("body").strip() for mt in _FENCE_EMBEDDED_RE.finditer(stripped)]
    if len(bodies) >= 2:
        for body in reversed(bodies):
            if _is_json_object(body):
                return body
        return bodies[-1]
    # Single block (or none): the whole-reply fence is the precise case.
    m = _FENCE_WHOLE_RE.match(stripped)
    if m:
        return m.group("body").strip()
    if bodies:
        return bodies[0]
    return text


def _is_json_object(text: str) -> bool:
    """Best-effort JSON-object validity check. Used by _strip_markdown_fence to
    pick the last fenced block that actually parses (B11 Defect 4b)."""
    try:
        return isinstance(json.loads(text), dict)
    except (json.JSONDecodeError, TypeError):
        return False
