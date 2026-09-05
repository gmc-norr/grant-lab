"""Rubric extractor — single Opus call that turns a funder's evaluation
criteria (usually buried in the call body) into a structured JSON rubric.

Per-call, ingestion-time. The Phase 0 worker copies the result into
workspaces.rubric_criteria + workspaces.rubric_funder_notes.
"""

from __future__ import annotations

import json
from typing import Protocol, TypedDict

_REQUIRED_CRITERION_FIELDS = ("name", "description", "weight", "scale_max")

_SYSTEM_PROMPT = """You are an extractor. You receive a funding-call body and
return a JSON object that captures the FUNDER'S OWN evaluation criteria — the
rubric their reviewers will use to score proposals.

Output schema — STRICTLY this shape:

{
  "criteria": [
    {
      "name": "<short name, e.g. Scientific excellence>",
      "description": "<one sentence explaining what this criterion measures>",
      "weight": <float 0..1; if the funder publishes percentages, normalize so
                 weights across criteria sum to ~1.0>,
      "scale_max": <int; the upper end of the rating scale, usually 5>,
      "subcriteria": [   // optional
        {"name": "...", "description": "..."}
      ]
    },
    ...
  ],
  "funder_notes": "<free text capturing eligibility-adjacent notes the rubric
                    consumer should see (FAQ links, late-submission policy,
                    review-panel composition). Empty string if none.>"
}

Return at least one criterion. If the call body doesn't publish explicit
criteria, infer the rubric from what the call asks proposers to demonstrate.
Do not invent weights — if the funder doesn't publish them, distribute evenly.
No prose around the JSON.
"""


class _Client(Protocol):
    # `model` is not read by extract_rubric itself, but the persistence
    # caller (e.g. ensure_rubric_fresh) stamps it onto the
    # call_rubrics.extracted_by_model column.
    model: str

    def chat(
        self,
        *,
        messages: list[dict],
        response_format: dict | None = None,
    ) -> dict: ...


class RubricPayload(TypedDict):
    criteria: list[dict]
    funder_notes: str


def extract_rubric(*, call_text: str, client: _Client) -> RubricPayload:
    """Run the extractor against `call_text`. Returns `{criteria, funder_notes}`.
    Raises ValueError on malformed JSON, empty criteria, or missing required
    criterion fields. Does NOT persist.
    """
    envelope = client.chat(
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": call_text},
        ],
        response_format={"type": "json_object"},
    )
    try:
        content = envelope["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"unexpected envelope shape from LLM client: {e}") from e

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"malformed JSON from rubric extractor: {e}") from e

    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSON object, got {type(parsed).__name__}")

    criteria = parsed.get("criteria")
    if not isinstance(criteria, list) or len(criteria) == 0:
        raise ValueError("rubric must have at least one criterion")

    for i, crit in enumerate(criteria):
        if not isinstance(crit, dict):
            raise ValueError(f"criterion {i}: expected object, got {type(crit).__name__}")
        for required in _REQUIRED_CRITERION_FIELDS:
            if required not in crit:
                raise ValueError(f"criterion {i}: missing required field {required!r}")

    notes = parsed.get("funder_notes", "")
    if not isinstance(notes, str):
        raise ValueError(f"funder_notes: expected str, got {type(notes).__name__}")

    return {"criteria": criteria, "funder_notes": notes}
