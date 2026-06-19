"""Pure helpers for inline citation markers in proposal.md.

A marker is `[@<citekey>]<!-- ev:<ev_id> -->`. The visible `[@citekey]` renders
via pandoc; the HTML comment binds the occurrence to its ledger row by ev id,
so repeated cites of the same citekey are individually addressable. All
functions are pure string transforms — no git/DB/file I/O.
"""

from __future__ import annotations

import re


class AnchorError(ValueError):
    """The anchor text was not found exactly once in the document."""


# A full marker: [@citekey]<!-- ev:ev-<hex> -->
# citekey  — alnum (upper or lower), underscores, hyphens (covers smith2021,
#            ref-a1b2c3d4e5f6, smith2021a, etc.)
# ev_id    — ev- followed by at least 6 hex chars (ledger ev ids are 12 hex,
#            but the regex floor is 6)
MARKER_RE = re.compile(r"\[@(?P<citekey>[A-Za-z0-9_-]+)\]<!-- ev:(?P<ev_id>ev-[0-9a-f]{6,}) -->")

# Anchored validators so callers can't create un-removable markers.
# CITEKEY_RE is the single canonical citekey alphabet — exported so other modules
# (e.g. the rename tool) validate against the SAME compiled pattern.
CITEKEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_EV_ID_RE = re.compile(r"^ev-[0-9a-f]{6,}$")


def build_marker(citekey: str, ev_id: str) -> str:
    """Return the canonical marker string for a citekey + ev id pair.

    Raises :class:`ValueError` if ``citekey`` or ``ev_id`` do not match their
    respective allowed patterns.
    """
    if not CITEKEY_RE.match(citekey):
        raise ValueError(f"invalid citekey {citekey!r}")
    if not _EV_ID_RE.match(ev_id):
        raise ValueError(f"invalid ev_id {ev_id!r}")
    return f"[@{citekey}]<!-- ev:{ev_id} -->"


def find_markers(text: str) -> list[dict]:
    """Return [{citekey, ev_id, start, end}] for each marker, in document order."""
    return [
        {
            "citekey": m.group("citekey"),
            "ev_id": m.group("ev_id"),
            "start": m.start(),
            "end": m.end(),
        }
        for m in MARKER_RE.finditer(text)
    ]


def anchor_occurrences(text: str, anchor: str) -> list[int]:
    """Start indices where ``anchor`` occurs as PLAIN TEXT — i.e. NOT overlapping
    an existing marker span. This is the exact occurrence set ``insert_marker``
    uses to decide not-found / unique / ambiguous. Callers that must prove an
    anchor belongs to a given section MUST use this (a raw ``in`` would count a
    marker-internal occurrence and mis-locate the insert)."""
    if not anchor:
        return []
    marker_spans = [(m["start"], m["end"]) for m in find_markers(text)]

    def _overlaps_marker(pos: int, end: int) -> bool:
        return any(ms < end and pos < me for ms, me in marker_spans)

    occurrences: list[int] = []
    start = 0
    while True:
        pos = text.find(anchor, start)
        if pos == -1:
            break
        if not _overlaps_marker(pos, pos + len(anchor)):
            occurrences.append(pos)
        start = pos + 1
    return occurrences


def insert_marker(text: str, anchor: str, citekey: str, ev_id: str) -> str:
    """Insert ``[@citekey]<!-- ev:ev_id -->`` immediately AFTER the unique ``anchor``
    substring. Raises :class:`AnchorError` if ``anchor`` occurs 0 or >1 times.

    Anchor occurrences that overlap an existing marker span are ignored, so that
    a citekey substring appearing only inside ``[@citekey]<!-- ev:... -->`` does
    not falsely match.
    """
    if not anchor:
        raise AnchorError("anchor must be a non-empty string")
    occurrences = anchor_occurrences(text, anchor)
    if not occurrences:
        raise AnchorError("anchor not found")
    if len(occurrences) > 1:
        raise AnchorError(f"anchor is ambiguous (found {len(occurrences)} times)")
    idx = occurrences[0] + len(anchor)
    return text[:idx] + build_marker(citekey, ev_id) + text[idx:]


def remove_marker(text: str, ev_id: str) -> tuple[str, bool]:
    """Remove the marker whose ev id equals *ev_id*.

    Returns ``(new_text, removed)`` where *removed* is ``True`` when the
    marker was found and deleted, ``False`` when it was not present.
    The marker is replaced with an empty string; surrounding whitespace in the
    original text is left as-is (callers may normalise if needed).
    """
    removed = False

    def _sub(m: re.Match) -> str:
        nonlocal removed
        if m.group("ev_id") == ev_id:
            removed = True
            return ""
        return m.group(0)

    new_text = MARKER_RE.sub(_sub, text)
    return new_text, removed


def rename_marker_citekey(text: str, old_citekey: str, new_citekey: str) -> tuple[str, int]:
    """Rewrite the citekey of every marker whose citekey equals *old_citekey*.

    Each marker's ev id is preserved. Returns ``(new_text, count_rewritten)``
    where *count_rewritten* is 0 when *old_citekey* was not present.

    Raises :class:`ValueError` if ``new_citekey`` does not match the allowed
    citekey pattern (prevents creating un-removable markers).
    """
    if not CITEKEY_RE.match(new_citekey):
        raise ValueError(f"invalid citekey {new_citekey!r}")
    count = 0

    def _sub(m: re.Match) -> str:
        nonlocal count
        if m.group("citekey") == old_citekey:
            count += 1
            return build_marker(new_citekey, m.group("ev_id"))
        return m.group(0)

    new_text = MARKER_RE.sub(_sub, text)
    return new_text, count


# A visible citekey marker NOT followed by the canonical ev-comment. Users
# hand-typing [@key] in the section editor produce these; they are invisible
# to find_markers (and so to deterministic validation) and block composed
# export. Detected at Tier-1 save validation.
# The lookahead must require a FULLY canonical comment (same ev-id pattern as
# MARKER_RE) — a weaker `(?!<!-- ev:)` would let near-canonical garbage like
# `[@k]<!-- ev:123 -->` or uppercase hex escape BOTH find_markers and this scan.
# Pandoc-style multi/prefixed citations (`[@a; @b]`, `[see @k]`) are out of
# scope for this scan.
_BARE_RE = re.compile(r"\[@(?P<citekey>[A-Za-z0-9_-]+)\](?!<!-- ev:ev-[0-9a-f]{6,} -->)")


def find_bare_markers(text: str) -> list[dict]:
    """Return [{citekey, start}] for each bare ``[@key]`` lacking the
    ``<!-- ev:... -->`` evidence comment, in document order."""
    return [{"citekey": m.group("citekey"), "start": m.start()} for m in _BARE_RE.finditer(text)]
