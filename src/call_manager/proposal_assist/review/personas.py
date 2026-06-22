"""Loads review_personas.yaml — the six mock-panel role prompts keyed by
ReviewRunItem.kind. Validated at load against the CHECK-constraint kind set so a
drift between the yaml and the schema fails fast. The prompt is the LEADING
paragraph of each sub-call's INSTRUCTION (the SDK adapter has no system_prompt
parameter — see review/agent.py)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

REVIEW_KINDS: tuple[str, ...] = (
    "fit_check",
    "persona_domain_skeptic",
    "persona_methodology_critic",
    "persona_translation_skeptic",
    "persona_devils_advocate",
    "consolidation",
)
PERSONA_KINDS: tuple[str, ...] = (
    "persona_domain_skeptic",
    "persona_methodology_critic",
    "persona_translation_skeptic",
    "persona_devils_advocate",
)
WAVE1_KINDS: tuple[str, ...] = ("fit_check", *PERSONA_KINDS)

_YAML_PATH = Path(__file__).with_name("review_personas.yaml")


@lru_cache(maxsize=1)
def _load() -> dict[str, str]:
    data = yaml.safe_load(_YAML_PATH.read_text())
    if not isinstance(data, dict) or set(data) != set(REVIEW_KINDS):
        raise RuntimeError(
            f"review_personas.yaml keys {sorted(data or {})} != {sorted(REVIEW_KINDS)}"
        )
    return {k: str(v).strip() for k, v in data.items()}


def persona_prompt(kind: str) -> str:
    return _load()[kind]
