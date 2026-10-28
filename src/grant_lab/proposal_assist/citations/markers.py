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

# Anchored validators so callers can't create markers that MARKER_RE would then
# fail to match (which would make them invisible to find_markers and to composed
# export). CITEKEY_RE is the single canonical citekey alphabet.
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


def _link_tail_end(text: str, pos: int) -> int | None:
    """End index (exclusive) of an inline link's ``(destination "title")`` tail
    starting at ``pos``, or ``None`` when ``pos`` does not begin one.

    Deliberately conservative — anything it cannot parse returns ``None``, which
    leaves :func:`insert_marker` at its original insertion point.

    A NEWLINE anywhere in the tail aborts the scan. CommonMark lets the
    destination-whitespace and the title span lines, so without this the marker
    could be moved across a ``## `` line — past a section boundary, into text the
    caller never validated (``human_cite`` proves the anchor belongs to the
    validated section and then relies on this function's placement). Our own
    serializer never emits a multi-line link tail, so refusing one costs nothing.
    """
    n = len(text)
    if pos >= n or text[pos] != "(":
        return None
    i = pos + 1
    while i < n and text[i] in " \t":
        i += 1
    if i < n and text[i] == "<":  # <destination>
        i += 1
        while i < n and text[i] not in ">\n":
            i += 2 if text[i] == "\\" else 1
        if i >= n or text[i] != ">":
            return None
        i += 1
    else:  # bare destination: balanced parens
        depth = 0
        while i < n:
            ch = text[i]
            if ch == "\\":
                i += 2
                continue
            if ch in " \t\n":
                break
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            i += 1
    while i < n and text[i] in " \t":
        i += 1
    if i < n and text[i] in "\"'(":  # optional title
        close = ")" if text[i] == "(" else text[i]
        i += 1
        while i < n and text[i] not in (close, "\n"):
            i += 2 if text[i] == "\\" else 1
        if i >= n or text[i] != close:
            return None
        i += 1
        while i < n and text[i] in " \t":
            i += 1
    return i + 1 if i < n and text[i] == ")" else None


def _enclosing_link_end(text: str, idx: int) -> int | None:
    """End index (exclusive) of the OUTERMOST inline link whose LABEL contains
    ``idx``, or ``None`` when ``idx`` is not inside a link label.

    Bracket matching, outermost-first, so an image nested in a link
    (``[![alt](a.png)](https://…)``) yields the outer link's end. Code spans are
    skipped because a ``[`` inside one is not a link label.
    """
    stack: list[int] = []
    best: int | None = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            run = i
            while run < n and text[run] == "`":
                run += 1
            fence = text[i:run]
            close = text.find(fence, run)
            i = (close + len(fence)) if close != -1 else run
            continue
        if ch == "[":
            stack.append(i)
        elif ch == "]" and stack:
            label_start = stack.pop()
            end = _link_tail_end(text, i + 1)
            # The anchor ends INSIDE this label when the insertion point sits
            # strictly after the "[" and at or before the "]".
            if end is not None and label_start < idx <= i:
                best = end if best is None else max(best, end)
        i += 1
    return best


def insert_marker(text: str, anchor: str, citekey: str, ev_id: str) -> str:
    """Insert ``[@citekey]<!-- ev:ev_id -->`` immediately AFTER the unique ``anchor``
    substring. Raises :class:`AnchorError` if ``anchor`` occurs 0 or >1 times.

    Anchor occurrences that overlap an existing marker span are ignored, so that
    a citekey substring appearing only inside ``[@citekey]<!-- ev:... -->`` does
    not falsely match.

    When the anchor ends inside a LINK LABEL the marker is placed after the link's
    closing ``)`` instead. Putting it inside the label destroyed the link: the
    editor's selection anchor is ``textBetween``, so selecting linked text and
    citing it produced
    ``[CRISPR screen[@ref-…]<!-- ev:… -->]\\(https://doi.org/10.1/x)`` — zero nodes
    still carrying the link mark — because markdown-it's link-label parser treats
    the ``[``-started citation token as a nested link and gives up (review
    2026-10-26 M-PA-7). The marker is cosmetically one word later; the link
    survives.

    ONLY an inline ``[text](dest)`` link is recognised. A REFERENCE-style label
    (``[text][ref]`` / the collapsed ``[text][]`` / the shortcut ``[text]``) still
    takes the marker INSIDE the label and is destroyed the same way. Our serializer
    never emits a reference link — ``_link_destination`` always writes an inline
    destination — so this is reachable only for markdown that arrived in the repo
    by another route (a hand-edited ``proposal.md``, an imported draft). Handling it
    needs the refdef table, which this pure-text helper does not have (review
    2026-10-26 fix pass, Minor 7).
    """
    if not anchor:
        raise AnchorError("anchor must be a non-empty string")
    occurrences = anchor_occurrences(text, anchor)
    if not occurrences:
        raise AnchorError("anchor not found")
    if len(occurrences) > 1:
        raise AnchorError(f"anchor is ambiguous (found {len(occurrences)} times)")
    idx = occurrences[0] + len(anchor)
    # `or idx` would silently treat a 0 end as "not in a link". _enclosing_link_end
    # cannot return 0 today (an end index is always past the "[") but the sentinel
    # is None, so test it as None — the `or` form bites the first time the helper's
    # bounds change (review 2026-10-26 fix pass, Minor 6).
    link_end = _enclosing_link_end(text, idx)
    if link_end is not None:
        idx = link_end
    return text[:idx] + build_marker(citekey, ev_id) + text[idx:]


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
