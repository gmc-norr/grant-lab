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
# citekey  — lowercase alnum, underscores, hyphens (covers smith2021, ref-a1b2c3d4e5f6,
#            smith2021a, etc.)
# ev_id    — ev- followed by at least 6 hex chars (ledger produces 12-char hex)
MARKER_RE = re.compile(r"\[@(?P<citekey>[A-Za-z0-9_-]+)\]<!-- ev:(?P<ev_id>ev-[0-9a-f]{6,}) -->")


def build_marker(citekey: str, ev_id: str) -> str:
    """Return the canonical marker string for a citekey + ev id pair."""
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


def insert_marker(text: str, anchor: str, citekey: str, ev_id: str) -> str:
    """Insert ``[@citekey]<!-- ev:ev_id -->`` immediately AFTER the unique ``anchor``
    substring. Raises :class:`AnchorError` if ``anchor`` occurs 0 or >1 times."""
    if not anchor:
        raise AnchorError("anchor must be a non-empty string")
    count = text.count(anchor)
    if count == 0:
        raise AnchorError("anchor not found")
    if count > 1:
        raise AnchorError(f"anchor is ambiguous (found {count} times)")
    idx = text.index(anchor) + len(anchor)
    marker = build_marker(citekey, ev_id)
    return text[:idx] + marker + text[idx:]


def remove_marker(text: str, ev_id: str) -> tuple[str, bool]:
    """Remove the marker whose ev id equals *ev_id*.

    Returns ``(new_text, removed)`` where *removed* is ``True`` when the
    marker was found and deleted, ``False`` when it was not present.
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
    """
    count = 0

    def _sub(m: re.Match) -> str:
        nonlocal count
        if m.group("citekey") == old_citekey:
            count += 1
            return build_marker(new_citekey, m.group("ev_id"))
        return m.group(0)

    new_text = MARKER_RE.sub(_sub, text)
    return new_text, count
