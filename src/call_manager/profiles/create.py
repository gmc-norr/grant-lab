from __future__ import annotations

from call_manager.profiles.fields import MAX_LIST_ENTRIES, MAX_TAG_LEN, sanitize_structured
from call_manager.profiles.seed import DraftProfile

_NAMESPACES = ("eligibility", "org", "funding", "geo")
# Rubric fields the LLM suggests but the create form does NOT collect — without
# these the worker would lose the pre-fill the old synchronous path provided.
_RUBRIC_SCALARS = ("budget_min", "budget_max", "trl_min", "trl_max")
_RUBRIC_LISTS = ("must_include", "hard_exclusions")


def merge_bootstrap_into_profile(profile, draft: DraftProfile) -> None:
    """Write the LLM draft into an existing profile WITHOUT overwriting any
    field the user already filled. Safe because the profile is read-only while
    its bootstrap job is non-terminal (the worker is the sole writer).

    Rubric scalars/lists (budget_min/max, trl_min/max, must_include,
    hard_exclusions) are read from the raw draft — they are not kind-namespaced.
    Namespace fields (eligibility/org/funding/geo) use the sanitized draft_clean
    as the base so that kind-inappropriate LLM fields (e.g. career_stage on an
    organizational_unit) are never persisted. User values always win on conflict."""
    if not (profile.description or "").strip():
        profile.description = draft.description

    # Sanitize the draft namespaces — kind-inappropriate fields are dropped here.
    draft_clean = sanitize_structured(kind=profile.kind, structured=draft.structured)

    current = dict(profile.structured or {})

    if not current.get("areas"):
        draft_areas = [a for a in draft.structured.get("areas", []) if len(str(a)) <= MAX_TAG_LEN]
        current["areas"] = draft_areas[:MAX_LIST_ENTRIES]

    # Rubric scalars/lists (budget/TRL/must_include/hard_exclusions): fill where blank.
    for key in _RUBRIC_SCALARS:
        if current.get(key) in (None, "") and draft.structured.get(key) is not None:
            current[key] = draft.structured.get(key)
    for key in _RUBRIC_LISTS:
        if not current.get(key) and draft.structured.get(key):
            current[key] = [str(v) for v in draft.structured.get(key, [])][:MAX_LIST_ENTRIES]

    # Namespace merging: use sanitized draft_clean as base so kind-inappropriate
    # fields are never persisted. User values win on conflict.
    for ns in _NAMESPACES:
        base_ns = draft_clean.get(ns, {})
        user_ns = {k: v for k, v in (current.get(ns) or {}).items() if v not in (None, [], "")}
        if base_ns or user_ns:
            current[ns] = {**base_ns, **user_ns}  # user values win
    profile.structured = current
