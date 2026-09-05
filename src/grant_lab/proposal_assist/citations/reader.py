"""Pure helpers for the citation source viewer (Tier 1, extracted text).

Numbering and evidence-span location. No git/DB/file I/O — callers own those.
"""

from __future__ import annotations

from grant_lab.proposal_assist.citations.ledger import find_by_id
from grant_lab.proposal_assist.citations.markers import find_markers
from grant_lab.proposal_assist.citations.quotes import (
    QuoteAmbiguous,
    QuoteNotFound,
    find_quote_offsets,
    normalize_for_quote_match,
)

# Mirror the Jinja reader's limits (web/citations.py _MAX_READER_CHARS /
# _WINDOW_RADIUS) so the JSON peek and the legacy HTML drawer window identically.
SOURCE_MAX_CHARS = 500_000
SOURCE_WINDOW_RADIUS = 4_000


def build_citation_numbering(proposal_text: str, ledger: dict) -> dict[str, int]:
    """Map each live, ledger-backed evidence id to its citation number.

    Numbering is per citekey (= per reference) in first-appearance order across
    the WHOLE proposal; repeat cites of a citekey share the number. Markers with
    no matching ledger row are excluded (they render inert)."""
    number_by_citekey: dict[str, int] = {}
    number_by_ev: dict[str, int] = {}
    for m in find_markers(proposal_text):
        if find_by_id(ledger, m["ev_id"]) is None:
            continue
        citekey = m["citekey"]
        if citekey not in number_by_citekey:
            number_by_citekey[citekey] = len(number_by_citekey) + 1
        number_by_ev[m["ev_id"]] = number_by_citekey[citekey]
    return number_by_ev


def evidence_order_for_reference(
    proposal_text: str, ledger: dict, reference_id: str
) -> list[str]:
    """Ordered (document order) list of live evidence ids whose ledger row cites
    ``reference_id``. Drives prev/next + 'i of N'.

    Duplicate marker ids (data anomaly) are collapsed to first occurrence so that
    ``order.index(ev_id)`` is always unambiguous."""
    out: list[str] = []
    seen: set[str] = set()
    for m in find_markers(proposal_text):
        ev_id = m["ev_id"]
        if ev_id in seen:
            continue
        row = find_by_id(ledger, ev_id)
        if row is not None and str(row.get("reference_id")) == str(reference_id):
            seen.add(ev_id)
            out.append(ev_id)
    return out


def build_citation_stances(ledger: dict) -> dict[str, str]:
    """Map each evidence id to its stance (e.g. 'supports'/'contradicts'), used
    to tint the inline citation marker. Rows without a stance are omitted."""
    return {
        e["id"]: e["stance"]
        for e in ledger.get("evidence", [])
        if e.get("id") and e.get("stance")
    }


def _all_indices(haystack: str, needle: str) -> list[int]:
    out, start = [], 0
    if not needle:
        return out
    while True:
        i = haystack.find(needle, start)
        if i == -1:
            return out
        out.append(i)
        start = i + 1


def locate_evidence(
    full_text: str, chunk_text: str | None, quote: str
) -> tuple[int, int] | None:
    """Return [start, end) of the evidence span in ``full_text``, or None.

    Recomputes live (never trusts stored offsets):
      1. If the cited chunk is available, find the quote uniquely WITHIN the
         chunk, place the chunk UNIQUELY in full_text, map the offsets, and
         accept only if in-bounds and the span normalizes to the quote.
      2. Otherwise / on any failure: find the quote uniquely in full_text.
      3. Ambiguous or not found -> None."""
    if chunk_text:
        try:
            cstart, cend = find_quote_offsets(chunk_text, quote)
        except (QuoteNotFound, QuoteAmbiguous):
            cstart = cend = None
        if cstart is not None:
            placements = _all_indices(full_text, chunk_text)
            if len(placements) == 1:
                s = placements[0] + cstart
                e = placements[0] + cend
                if 0 <= s < e <= len(full_text) and (
                    normalize_for_quote_match(full_text[s:e])
                    == normalize_for_quote_match(quote)
                ):
                    return (s, e)
    try:
        return find_quote_offsets(full_text, quote)
    except (QuoteNotFound, QuoteAmbiguous):
        return None


def build_source_window(
    full_text: str | None,
    chunk_text: str | None,
    quote: str,
    *,
    max_chars: int = SOURCE_MAX_CHARS,
    window_radius: int = SOURCE_WINDOW_RADIUS,
) -> tuple[str, tuple[int, int] | None, str | None]:
    """JSON analogue of the Jinja ``_reader_body``: return ``(text, highlight, note)``.

    ``text`` is the (possibly windowed) source to display; ``highlight`` is the
    ``[start, end)`` evidence span as offsets INTO ``text`` (NOT the original
    ``full_text``), or ``None`` when the span can't be pinpointed; ``note`` is an
    optional human explanation of any degradation. The missing-data matrix mirrors
    the shipped HTML drawer exactly:

      - full_text and chunk both absent  -> ("", None, None)
      - full_text absent, chunk present  -> (chunk, whole-span, "Showing the cited section.")
      - span not found, chunk present    -> (chunk, whole-span, "...couldn't pinpoint...")
      - span not found, no chunk         -> (full_text head, None, "Couldn't pinpoint...")
      - span found, oversized doc        -> (window, windowed-span, "...large document.")
      - span found, normal doc           -> (full_text, span, None)
    """
    if full_text is None and chunk_text is None:
        return "", None, None
    if full_text is None:
        # text.txt absent but the cited chunk is available — show the chunk whole.
        return chunk_text, (0, len(chunk_text)), "Showing the cited section."
    span = locate_evidence(full_text, chunk_text, quote)
    if span is None:
        if chunk_text is not None:
            return (
                chunk_text,
                (0, len(chunk_text)),
                "Showing the cited section — couldn't pinpoint the exact sentence "
                "in the full document.",
            )
        return full_text[: 2 * window_radius], None, "Couldn't pinpoint the cited sentence."
    s, e = span
    if len(full_text) > max_chars:
        lo = max(0, s - window_radius)
        hi = min(len(full_text), e + window_radius)
        return full_text[lo:hi], (s - lo, e - lo), "Showing the cited section of a large document."
    return full_text, (s, e), None

