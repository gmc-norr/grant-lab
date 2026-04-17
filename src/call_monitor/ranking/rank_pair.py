from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from call_monitor.db.models import Call, Profile
from call_monitor.ranking.ollama_client import LLMError, OllamaClient
from call_monitor.ranking.prompt import PROMPT_VERSION, build_messages

_logger = logging.getLogger(__name__)
_RATIONALE_MAX = 1000


@dataclass(slots=True)
class RankResult:
    score: int
    rationale: str
    model: str
    prompt_version: str
    raw_response: dict


def _extract_assistant_content(envelope: dict) -> str:
    try:
        message = envelope["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"Ollama envelope missing choices[0].message: {e}") from e
    # Gemma may emit tool_calls instead of textual content under JSON mode;
    # in that case content is None or absent. Surface as LLMError so the
    # worker dead-letters cleanly instead of TypeError-ing on json.loads(None).
    content = message.get("content")
    if not content:
        raise LLMError(
            "Ollama returned empty assistant content "
            "(model may have used tool_calls instead of plain content)"
        )
    # Future-proofing: a multimodal provider may return content as a list
    # of parts (e.g., [{"type":"text","text":"..."}]). Guard now so a
    # provider swap fails at LLMError (retried + eventually dead-lettered)
    # rather than TypeError on json.loads([...]).
    if not isinstance(content, str):
        raise LLMError(
            f"Ollama returned non-string content: {type(content).__name__}"
        )
    return content


def _parse_score_rationale(content: str) -> tuple[int, str]:
    try:
        parsed = json.loads(content)
    except ValueError as e:
        raise LLMError(f"assistant content is invalid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise LLMError(f"assistant content is not a JSON object: {type(parsed).__name__}")
    if "score" not in parsed:
        raise LLMError("assistant content missing required key 'score'")
    score = parsed["score"]
    # `isinstance(True, int)` is True in Python (bool subclasses int), so
    # a model returning {"score": true} would otherwise coerce to 1 and
    # silently persist a nonsense ranking. Reject booleans explicitly so
    # malformed LLM output is retried / dead-lettered instead.
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise LLMError(f"score must be an integer in 0–100; got {score!r}")
    rationale = str(parsed.get("rationale") or "")
    if len(rationale) > _RATIONALE_MAX:
        rationale = rationale[:_RATIONALE_MAX]
    return score, rationale


def rank_pair(
    *, client: OllamaClient, profile: Profile, call: Call
) -> RankResult:
    """Score one (call, profile) pair via the LLM.

    Raises LLMError on any HTTP / parse / validation failure; the queue
    worker turns those into job retries (with backoff) and dead-letters
    after the attempt cap.
    """
    messages = build_messages(profile, call)
    envelope = client.chat(messages=messages)
    content = _extract_assistant_content(envelope)
    score, rationale = _parse_score_rationale(content)
    model = envelope.get("model") or client.model  # fallback for sparse responses
    return RankResult(
        score=score,
        rationale=rationale,
        model=model,
        prompt_version=PROMPT_VERSION,
        raw_response=envelope,
    )
