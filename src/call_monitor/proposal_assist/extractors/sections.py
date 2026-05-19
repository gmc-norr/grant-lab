"""Section-aware extractor — single Opus call that splits a call body into
six named sections. Per-call, ingestion-time. Output shape is fixed; the
Phase 0 worker persists the result into `call_section_extracts` (template)
and then copies it into `workspaces.section_extracts`.
"""

from __future__ import annotations

import json
from typing import Protocol

SECTION_NAMES: tuple[str, ...] = (
    "eligibility",
    "scope",
    "budget",
    "expected_outcomes",
    "restrictions",
    "mandatory",
)

_SYSTEM_PROMPT = """You are an extractor. You receive a funding-call body and
return a JSON object that splits its content into six named sections.

Output schema — STRICTLY this shape, no other keys, no prose around the JSON:

{
  "eligibility": "...",      // who can apply
  "scope": "...",            // what the call funds
  "budget": "...",           // budget ranges, currencies, durations
  "expected_outcomes": "...", // deliverables / impact targets
  "restrictions": "...",     // exclusions, ineligibilities, dual-use rules
  "mandatory": "..."         // must-include items (forms, signatures, etc.)
}

If a section is absent from the source, return an empty string for that key —
do NOT omit the key, do NOT invent content. Quote sparingly; the goal is a
faithful extract, not a paraphrase. Each value: at most 1500 characters.
"""


class _Client(Protocol):
    model: str

    def chat(
        self,
        *,
        messages: list[dict],
        response_format: dict | None = None,
    ) -> dict: ...


def extract_sections(*, call_text: str, client: _Client) -> dict[str, str]:
    """Run the extractor against `call_text`. Returns a dict keyed by every
    name in SECTION_NAMES (missing ones backfilled to ""). Raises ValueError
    on malformed JSON or wrong value types.

    Does NOT persist — the caller is responsible for writing rows + computing
    `source_text_hash` of the input.
    """
    envelope = client.chat(
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": call_text},
        ],
        response_format={"type": "json_object"},
    )
    content = envelope["choices"][0]["message"]["content"]

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"malformed JSON from section extractor: {e}") from e

    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSON object, got {type(parsed).__name__}")

    result: dict[str, str] = {}
    for section in SECTION_NAMES:
        value = parsed.get(section, "")
        if not isinstance(value, str):
            raise ValueError(f"section {section!r}: expected str, got {type(value).__name__}")
        result[section] = value
    return result
