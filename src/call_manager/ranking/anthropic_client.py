"""Anthropic-hosted ranking client — drop-in replacement for OllamaClient.

When the embedding pre-gate scores a (call, profile) pair above
`Settings.remote_rank_cosine_threshold`, the worker routes the LLM step
here instead of to the local Gemma. This buys richer rationale + tighter
adherence to the JSON output schema on the calls that are most likely
to actually surface to a user in the digest — the cases where a slightly
better explanation is worth a few cents per pair.

API surface mirrors OllamaClient.chat() byte-for-byte, including the
returned envelope shape (`{"choices": [{"message": {"content": ...}}]}`),
so rank_pair's downstream parsing is reused without changes.

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

from call_manager.ranking.ollama_client import LLMError

_logger = logging.getLogger(__name__)


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
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 4096,
        timeout: float = 120.0,
    ):
        # api_key kept off the instance so repr() doesn't accidentally
        # echo it; the SDK closes over it.
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
        self.model = model
        self._max_tokens = max_tokens

    def chat(
        self,
        *,
        messages: list[dict],
        response_format: dict | None = None,  # accepted for parity, currently unused
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

        `response_format` is accepted to match OllamaClient's signature
        but ignored — Anthropic doesn't expose an OpenAI-style JSON-mode
        toggle, and the existing prompt already pins JSON output via
        explicit schema instructions.
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

            response = self._client.messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                system=system_blocks,
                messages=user_msgs,
            )
        except anthropic.APIStatusError as e:
            # Wrap into LLMError so the worker's existing retry/dead-letter
            # logic catches it the same as a Gemma failure. Carry the
            # request_id when present — invaluable for support tickets.
            req_id = getattr(e.response, "headers", {}).get("request-id", "")
            raise LLMError(f"anthropic {e.status_code} (request_id={req_id}): {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"anthropic connection error: {e}") from e

        # max_tokens truncation: the model hit the output cap mid-generation,
        # so `content` is a partial (almost always invalid) JSON fragment.
        # Letting it flow on would fail parse_response → ParseError → 5 paid
        # retries → dead-letter, none of which can help: the truncation is
        # deterministic for this prompt + max_tokens. Surface as LLMError now,
        # before extracting text. (LLMError still retries via the worker, but
        # the fix for a persistently-too-long response is to raise max_tokens,
        # not to keep re-billing the same truncating call. Logged loudly.)
        if response.stop_reason == "max_tokens":
            raise LLMError(
                f"anthropic response truncated at max_tokens={self._max_tokens} "
                f"(output_tokens={response.usage.output_tokens}); "
                "raise AnthropicClient max_tokens — retrying cannot help"
            )

        # Extract first text block. Tool calls / thinking blocks shouldn't
        # appear here (we don't request them), but guard anyway.
        text_blocks = [b for b in response.content if b.type == "text"]
        if not text_blocks:
            raise LLMError(
                f"anthropic response had no text content blocks "
                f"(blocks: {[b.type for b in response.content]})"
            )
        content = _strip_markdown_fence(text_blocks[0].text)

        return {
            "model": response.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": _map_stop_reason(response.stop_reason),
                }
            ],
            "usage": {
                "prompt_tokens": response.usage.input_tokens,
                "completion_tokens": response.usage.output_tokens,
                "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
                "cache_creation_input_tokens": getattr(
                    response.usage, "cache_creation_input_tokens", 0
                ),
            },
        }


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


_FENCE_RE = re.compile(
    r"\A\s*```(?:json|JSON)?\s*\n?(?P<body>.*?)\n?```\s*\Z",
    re.DOTALL,
)


def _strip_markdown_fence(text: str) -> str:
    """Anthropic's instruction-tuned models often wrap structured output
    in ```json ... ``` fences, even when the prompt explicitly asks for
    raw JSON. The downstream parser uses `json.loads()` directly and
    fails on the fence prefix ("Expecting value: line 1 column 1").

    Strip a single outer fence if present; leave everything else alone
    so a response that's already raw JSON is unaffected. We deliberately
    don't try to extract JSON from prose preambles — that's a different
    failure mode the prompt should handle, not silent recovery code.
    """
    if not text:
        return text
    m = _FENCE_RE.match(text.strip())
    if m:
        return m.group("body").strip()
    return text


def _is_json_object(text: str) -> bool:
    """Best-effort JSON-object validity check. Used only by tests."""
    try:
        return isinstance(json.loads(text), dict)
    except (json.JSONDecodeError, TypeError):
        return False
