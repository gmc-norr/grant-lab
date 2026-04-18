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
    return float(dot / math.sqrt(na * nb))


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
    """Embedding input for a profile: description ONLY.

    The structured rubric (areas, must_include, hard_exclusions) is
    keyword-exact, not semantic — embedding it would dilute the
    description's signal.
    """
    return profile.description or ""
