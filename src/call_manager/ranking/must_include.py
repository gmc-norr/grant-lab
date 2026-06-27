"""Shared must_include matching semantics for the ranking and digest paths.

`must_include` is a profile's force-include escape hatch: a matching call
bypasses the timeline/embedding gates so it still reaches the local Gemma
ranker (and sometimes a billed Sonnet escalation), AND the same matching feeds
the weekly email digest. The two paths MUST stay semantically identical or a
call could force-include on one path and not the other.

Two rules, defined here so neither path can drift:

  1. Minimum term length. Terms shorter than ``MUST_INCLUDE_MIN_LEN`` are
     ignored entirely. A 2-char term like "ai"/"rna"/"eu" is far too broad to
     be a useful escape hatch and (on the digest side) also too short for the
     pg_trgm trigram index to help.

  2. Word-boundary matching. A term matches only at WORD boundaries, never as a
     bare substring — so "ai" no longer matches inside "training"/"brain", nor
     "eu" inside "queue". The ranking path uses a Python ``\\b`` regex; the
     digest path uses Postgres' POSIX ``~*`` with ``\\m``/``\\M`` word
     boundaries. ``compile_term_pattern`` / ``pg_word_boundary_pattern`` below
     produce those two forms from the SAME escaped term so the boundary
     semantics match.

     Boundaries are anchored *adaptively*: a boundary is only added on an edge
     whose adjacent term character is a word character (``[A-Za-z0-9_]``). This
     keeps "rna" anchored (no substring trap) while letting a term that begins
     or ends in punctuation — "100%", "c++" — still match literally (a word
     boundary can't sit next to a non-word edge char, so a fixed ``\\b`` there
     would never match). Both paths apply the same rule so they cannot drift.
"""

from __future__ import annotations

import re

# Terms shorter than this are dropped on BOTH the ranking and digest paths.
MUST_INCLUDE_MIN_LEN = 3

# A term edge is "anchorable" only when its boundary character is a word char.
_WORD_CHAR_RE = re.compile(r"\w")


def normalize_must_terms(raw_terms) -> list[str]:
    """Lower-case, strip, drop empties and sub-minimum-length terms.

    Shared by both paths so the SAME set of terms is considered on each.
    """
    terms: list[str] = []
    for t in raw_terms or []:
        if not t:
            continue
        cleaned = t.strip().lower()
        if len(cleaned) < MUST_INCLUDE_MIN_LEN:
            continue
        terms.append(cleaned)
    return terms


def _anchor_left(term: str) -> bool:
    """True iff the term's first char is a word char (so a left boundary is
    meaningful)."""
    return bool(term) and bool(_WORD_CHAR_RE.match(term[0]))


def _anchor_right(term: str) -> bool:
    """True iff the term's last char is a word char (so a right boundary is
    meaningful)."""
    return bool(term) and bool(_WORD_CHAR_RE.match(term[-1]))


def compile_term_pattern(term: str) -> re.Pattern[str]:
    """Compile a case-insensitive, adaptively word-boundaried regex (Python)."""
    left = r"\b" if _anchor_left(term) else ""
    right = r"\b" if _anchor_right(term) else ""
    return re.compile(left + re.escape(term) + right, re.IGNORECASE)


def pg_word_boundary_pattern(term: str) -> str:
    """Build a Postgres POSIX regex (for the ``~*`` operator) that matches
    `term` at adaptive word boundaries. ``\\m`` / ``\\M`` anchor the start/end
    of a word; regex metacharacters in the term are escaped so an admin term
    like "c++" or "100%" matches literally."""
    left = r"\m" if _anchor_left(term) else ""
    right = r"\M" if _anchor_right(term) else ""
    return left + re.escape(term) + right
