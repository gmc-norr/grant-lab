"""Canonical 'live claim sentence' extraction for citation provenance.

Single source of truth for the claim text a citation marker is attached to.
insert_citation, update_citation(reaccept_claim), and the Slice-3
validate_citations MUST all use claim_sentence_around so the stored claim and
the validator's re-extraction never diverge. Pure string function.
"""

from __future__ import annotations

_TERMINATORS = ".!?\n"


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
