"""Canonical 'live claim sentence' extraction for citation provenance.

Single source of truth for the claim text a citation marker is attached to.
insert_citation, update_citation(reaccept_claim), and the Slice-3
validate_citations MUST all use claim_sentence_around so the stored claim and
the validator's re-extraction never diverge. Pure string function.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from call_manager.proposal_assist.citations.markers import MARKER_RE

_TERMINATORS = ".!?\n"

# A line is a markdown heading when, after optional leading spaces, it starts
# with one or more '#'. A fence line opens/closes a fenced code block.
_HEADING_RE = re.compile(r"^\s*#")
_FENCE_RE = re.compile(r"^\s*```")


def claim_sentence_around(text: str, marker_start: int, marker_end: int) -> str:
    """Return the claim sentence the marker at [marker_start, marker_end) trails.

    The marker span is excised first (so the ``[@key]<!-- ev -->`` text is never
    part of the claim). Markers conventionally trail the claim, so extraction
    works BACKWARD from the marker position to the start of the sentence and
    forward only as far as the claim's own closing terminator.

    Behaviour at the marker boundary:

    - ``"Recurrence is reduced[M]. Next one."`` -> ``"Recurrence is reduced"``
      (marker directly trails the claim; the period belongs to the same claim
      sentence but is stripped).
    - ``"Recurrence is reduced.[M] Next one."`` -> ``"Recurrence is reduced"``
      (the char before the marker is a terminator, so the marker trails the
      PRECEDING sentence, not "Next one").
    - ``"First. We argue X[M]. Then Y."`` -> ``"We argue X"``.

    Marker at the very start of the text (no preceding prose) returns ``""``.

    Returns a whitespace-collapsed sentence with surrounding terminators
    stripped; ``""`` if no claim sentence can be found.
    """
    # 1. Excise the marker. After excision the claim region ends at marker_start.
    without = text[:marker_start] + text[marker_end:]

    # 2. Skip any whitespace immediately before the marker position.
    pos = marker_start
    while pos > 0 and without[pos - 1] in " \t":
        pos -= 1

    # `pos` is now the index just after the last non-space char preceding the
    # marker. Whether that char is a terminator or claim prose, the backward
    # scan below treats it as the (exclusive) end of the candidate region and
    # then trims trailing terminators — so both
    #   "...reduced[M]."  (excised -> "...reduced. ...", pos before '.')
    #   "...reduced.[M] "  (excised -> "...reduced. ...", pos after '.')
    # converge on the same claim sentence.
    region_end = pos

    # 3. Trim trailing terminators of the claim's own sentence to find its last
    #    content char.
    last = region_end - 1
    while last >= 0 and without[last] in _TERMINATORS:
        last -= 1
    if last < 0:
        return ""  # nothing but terminators / empty before the marker

    # 4. Walk back to the previous terminator to find the sentence start.
    start = last
    while start >= 0 and without[start] not in _TERMINATORS:
        start -= 1
    sent_start = start + 1

    sentence = without[sent_start : last + 1]
    return " ".join(sentence.split())


def iter_proposal_sentences(text: str) -> Iterator[tuple[str, int]]:
    """Yield ``(sentence_text, char_offset)`` for each prose sentence in *text*.

    Markdown structure is skipped: heading lines (``#``-prefixed after optional
    leading spaces) and fenced code blocks (from an opening ```` ``` ```` fence
    to its closing fence, inclusive) contribute no sentences. The remaining
    prose is split on the same ``_TERMINATORS`` (``.!?\\n``) boundaries that
    :func:`claim_sentence_around` uses, so a marker-bearing sentence extracted
    by either function agrees.

    ``sentence_text`` is whitespace-collapsed with any citation markers
    (``[@key]<!-- ev:... -->``) stripped out — cleaner for a downstream
    "is this a claim?" LLM prompt — while ``char_offset`` indexes into the
    ORIGINAL *text* at the first non-whitespace character of the sentence, so
    callers can correlate it with raw marker positions.

    Fragments that are empty, whitespace-only, or — after stripping markers —
    contain no word characters (marker-only or punctuation-only) are skipped.

    Pure and deterministic: no DB / git / I/O.
    """
    in_fence = False
    # Build the set of character indices that belong to prose (non-heading,
    # non-code) lines, splitting each prose line into terminator-bounded
    # fragments while tracking offsets in the ORIGINAL text.
    line_start = 0
    for line in text.splitlines(keepends=True):
        stripped_line = line.rstrip("\n")
        next_start = line_start + len(line)

        if _FENCE_RE.match(stripped_line):
            # Toggle: the opening AND closing fence lines are both excluded.
            in_fence = not in_fence
            line_start = next_start
            continue
        if in_fence or _HEADING_RE.match(stripped_line):
            line_start = next_start
            continue

        # Prose line: split its content (without the trailing newline) on the
        # in-line terminators .!? — the newline itself is a terminator too, so
        # a fragment never spans lines.
        yield from _split_fragment(stripped_line, line_start)
        line_start = next_start


def _split_fragment(line: str, base_offset: int) -> Iterator[tuple[str, int]]:
    """Split one prose line into terminator-bounded sentences with offsets.

    ``base_offset`` is the index of ``line[0]`` within the original document.
    """
    # Marker spans must not be split on: a marker contains ``<!--`` (a '!'),
    # which would otherwise be treated as a sentence terminator.
    marker_spans = [(m.start(), m.end()) for m in MARKER_RE.finditer(line)]

    def _in_marker(idx: int) -> bool:
        return any(ms <= idx < me for ms, me in marker_spans)

    frag_start = 0
    i = 0
    n = len(line)
    while i < n:
        if line[i] in _TERMINATORS and not _in_marker(i):
            yield from _emit(line[frag_start:i], base_offset + frag_start)
            frag_start = i + 1
        i += 1
    # Trailing fragment with no terminator (the line's newline acted as the
    # terminator, or the line was the last in the document).
    if frag_start < n:
        yield from _emit(line[frag_start:n], base_offset + frag_start)


def _emit(fragment: str, frag_offset: int) -> Iterator[tuple[str, int]]:
    """Yield a cleaned sentence for *fragment*, or nothing if it is not a claim.

    Markers are stripped from the yielded text; the offset is advanced to the
    first non-whitespace character of the original fragment so it points at the
    sentence start in the document.
    """
    without_markers = MARKER_RE.sub("", fragment)
    # Skip empties, whitespace-only, marker-only, and punctuation-only frags:
    # require at least one word character once markers are gone.
    if not re.search(r"\w", without_markers):
        return
    # Advance the offset past leading whitespace AND any leading citation
    # marker(s), so the offset points at the first PROSE character — not at a
    # marker's "[" — even when a marker precedes the prose (non-trailing marker).
    # This keeps the offset usable for T7's sentence<->marker correlation.
    lead = len(fragment) - len(fragment.lstrip())
    while True:
        m = MARKER_RE.match(fragment, lead)
        if not m:
            break
        lead = m.end()
        lead += len(fragment[lead:]) - len(fragment[lead:].lstrip())
    sentence_text = " ".join(without_markers.split())
    yield sentence_text, frag_offset + lead
