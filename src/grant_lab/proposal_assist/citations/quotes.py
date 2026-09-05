"""Whitespace-tolerant quote matching for citation evidence.

Shared by insert_citation (Slice 2) and validate_citations (Slice 3) so both
agree on how an evidence_quote is matched against a chunk and how offsets are
recorded. Pure functions — no git/DB/IO.

Contract:
- Matching collapses any run of whitespace to a single space and trims the
  quote ends, so a quote copied across PDF line breaks still matches.
- Returned offsets are [start, end) indices into the RAW chunk text, so
  ``chunk_text[start:end]`` is the raw span (callers hash the raw chunk text
  separately for chunk_content_hash).
- A match must be unique: 0 → NotFound, >1 → Ambiguous.
"""

from __future__ import annotations


class QuoteNotFound(ValueError):
    """The evidence quote does not occur in the chunk."""


class QuoteAmbiguous(ValueError):
    """The evidence quote occurs more than once in the chunk."""


def normalize_for_quote_match(s: str) -> str:
    """Collapse all whitespace runs to single spaces and strip ends."""
    return " ".join(s.split())


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Return (normalized_text, index_map) where index_map[i] is the index in
    ``text`` of normalized_text[i].  Whitespace runs collapse to one space
    mapped to the FIRST whitespace character of the run.  Leading/trailing
    whitespace is preserved here as a single space so that searches anchored
    inside the text work; the quote side is trimmed via
    ``normalize_for_quote_match`` before searching.

    The returned ``index_map`` has exactly ``len(normalized_text)`` entries,
    so ``index_map[i]`` is always a valid index into ``text``.
    """
    norm: list[str] = []
    idx_map: list[int] = []
    prev_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if not prev_space:
                norm.append(" ")
                idx_map.append(i)
                prev_space = True
        else:
            norm.append(ch)
            idx_map.append(i)
            prev_space = False
    return "".join(norm), idx_map


def find_quote_offsets(chunk_text: str, quote: str) -> tuple[int, int]:
    """Find ``quote`` in ``chunk_text`` whitespace-tolerantly and return the
    RAW ``[start, end)`` offsets of the unique matching span.

    Raises:
        QuoteNotFound: zero occurrences (including empty / whitespace-only
            quotes).
        QuoteAmbiguous: more than one occurrence.
    """
    nquote = normalize_for_quote_match(quote)
    if not nquote:
        raise QuoteNotFound("empty quote")

    ntext, idx_map = _normalize_with_map(chunk_text)

    # Locate every occurrence (including overlapping) so duplicates are caught.
    starts: list[int] = []
    search_from = 0
    while True:
        pos = ntext.find(nquote, search_from)
        if pos == -1:
            break
        starts.append(pos)
        search_from = pos + 1  # +1 allows overlapping matches to be detected

    if not starts:
        raise QuoteNotFound("quote not found in chunk")
    if len(starts) > 1:
        raise QuoteAmbiguous(f"quote occurs {len(starts)} times in chunk")

    ns = starts[0]
    ne = ns + len(nquote)  # exclusive index in normalized space

    raw_start = idx_map[ns]

    # ne points one past the last matched character in normalized space.
    # idx_map[ne - 1] is the raw index of the last matched character; adding 1
    # gives the exclusive raw end.  This is correct even for multi-byte Unicode
    # because Python string indexing is by code point, not byte.
    raw_end = idx_map[ne - 1] + 1

    return (raw_start, raw_end)
