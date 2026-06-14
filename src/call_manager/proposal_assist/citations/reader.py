"""Pure helpers for the citation source viewer (Tier 1, extracted text).

Numbering, marker→link transform (sentinel + post-sanitization injection),
and evidence-span location. No git/DB/file I/O — callers own those.
"""

from __future__ import annotations

from call_manager.proposal_assist.citations.ledger import find_by_id
from call_manager.proposal_assist.citations.markers import find_markers


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
    ``reference_id``. Drives prev/next + 'i of N'."""
    out: list[str] = []
    for m in find_markers(proposal_text):
        row = find_by_id(ledger, m["ev_id"])
        if row is not None and str(row.get("reference_id")) == str(reference_id):
            out.append(m["ev_id"])
    return out


def build_citation_stances(ledger: dict) -> dict[str, str]:
    """Map each evidence id to its stance (e.g. 'supports'/'contradicts'), used
    to tint the inline citation marker. Rows without a stance are omitted."""
    return {
        e["id"]: e["stance"]
        for e in ledger.get("evidence", [])
        if e.get("id") and e.get("stance")
    }
