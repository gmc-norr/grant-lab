"""Pure helpers for embedding similarity + text construction.

No I/O. No DB. Easy to unit-test.
"""

from __future__ import annotations

import math

CALL_TEXT_FULL_TEXT_LIMIT = 8000  # BGE-M3 max input is 8192 tokens; 8000 chars is safe


def cosine(a: list[float] | None, b: list[float] | None) -> float | None:
    """Cosine similarity. None when either input is None, either vector
    is all-zeros, or dimensions don't match — caller treats None as
    UNKNOWN (not FAIL).
    """
    if a is None or b is None:
        return None
    if len(a) != len(b):
        return None
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return None
    # Coerce to Python float — pgvector-loaded numpy arrays would otherwise
    # promote the accumulator to numpy.float32 and we'd return numpy.float32,
    # violating the `float | None` annotation. Cheap and keeps the type
    # contract honest at the call boundary.
    result = float(dot / math.sqrt(na * nb))
    # Guard against non-finite results: a NaN/Inf component (or overflow in
    # na*nb) would produce NaN/Inf here. Treat as UNKNOWN (None) rather than
    # letting a poisoned score propagate into ranking comparisons.
    if not math.isfinite(result):
        return None
    return result


def build_call_text(call) -> str:
    """Embedding input for a call: title + summary + full_text[:8000].

    Newlines separate the parts so the model gets clear segment boundaries.
    """
    parts: list[str] = []
    if call.title:
        parts.append(call.title)
    if call.summary:
        parts.append(call.summary)
    if call.full_text:
        parts.append(call.full_text[:CALL_TEXT_FULL_TEXT_LIMIT])
    return "\n\n".join(parts)


def build_profile_text(profile) -> str:
    """Embedding input for a profile: description + `areas`.

    Codex #2: `areas` is the structured signal of what the group actually
    works on — a terse description ("Hepatology group") next to areas
    like `["liver cancer", "transplant rejection"]` should embed with
    both signals, otherwise a broad funding call on "transplant rejection"
    would cosine-miss a group that in fact does exactly that.

    Still excluded:
    - `must_include` / `hard_exclusions` — keyword-exact, handled by the
      must-include bypass + hard-exclusion gate; embedding them would
      make one tagged keyword drown out the profile's real topic surface.
    - `budget_min/max`, `trl_min/max` — numeric, handled by their own
      deterministic gates; embedding numbers is a hashing exercise.

    Changing this function changes every profile's stored vector.
    Callers that persist embeddings (daily refresh tick, profile-edit
    handler) must re-embed profiles for the new text to take effect —
    see Alembic migration 0010 which NULLs existing profile vectors so
    the daily tick picks them up.
    """
    description = (profile.description or "").strip()
    structured = profile.structured or {}
    areas = structured.get("areas") or []
    areas_text = "\n".join(a.strip() for a in areas if a and a.strip())
    parts = [p for p in (description, areas_text) if p]
    return "\n\n".join(parts)
