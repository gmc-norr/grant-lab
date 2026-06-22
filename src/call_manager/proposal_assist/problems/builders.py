"""Pure signal builders for the Problems panel (SP2b-3b).

Each builder maps already-loaded inputs to a flat list of ProblemRow dicts. No
DB, no I/O — the orchestrator (aggregate.py) loads inputs and calls these. A
ProblemRow:

    {"category": "broken_citation"|"over_length"|"criterion_gap"|"coherence_drift",
     "severity": "error"|"warning",
     "title": str,
     "detail": str,
     "target": {"top_tab": "draft"|"spec"|"scorecard"|None,
                "bottom_tab": "inbox"|"problems"|None,
                "section_index": int|None} | None}
"""

from __future__ import annotations

import re

from call_manager.proposal_assist.editing.sectionizer import Section, split_sections


def _row(
    category: str,
    severity: str,
    title: str,
    detail: str,
    *,
    top_tab: str | None = None,
    bottom_tab: str | None = None,
    section_index: int | None = None,
) -> dict:
    target = None
    if top_tab is not None or bottom_tab is not None or section_index is not None:
        target = {"top_tab": top_tab, "bottom_tab": bottom_tab, "section_index": section_index}
    return {
        "category": category,
        "severity": severity,
        "title": title,
        "detail": detail,
        "target": target,
    }


def over_length_problems(sections: list[Section], budget: int) -> list[dict]:
    """One warning row per draft section whose character length exceeds `budget`.

    `budget <= 0` disables the check (returns []).
    """
    if budget <= 0:
        return []
    rows: list[dict] = []
    for s in sections:
        n = len(s.text)
        if n > budget:
            rows.append(
                _row(
                    "over_length",
                    "warning",
                    f"Over-length §{s.index}",
                    f"{n} chars (budget {budget})",
                    top_tab="draft",
                    section_index=s.index,
                )
            )
    return rows


_CHAR_RE = re.compile(r"char (\d+)")


def section_index_for_offset(proposal_text: str, offset: int | None) -> int | None:
    """Map a char offset in the draft to its section index, or None if offset is None.

    Uses split_sections cumulative spans (the byte-exact join invariant). An offset
    at/after the document end clamps to the last section; None offset -> None.
    """
    if offset is None:
        return None
    sections = split_sections(proposal_text)
    if not sections:
        return None
    pos = 0
    for s in sections:
        end = pos + len(s.text)
        if pos <= offset < end:
            return s.index
        pos = end
    return sections[-1].index  # clamp offsets at/after the end


def _offset_from_location(location: str | None) -> int | None:
    if not location:
        return None
    m = _CHAR_RE.search(location)
    return int(m.group(1)) if m else None


def broken_citation_problems(issues: list[dict], proposal_text: str) -> list[dict]:
    """Map each deterministic citation issue to a broken_citation ProblemRow."""
    rows: list[dict] = []
    for issue in issues:
        idx = section_index_for_offset(proposal_text, _offset_from_location(issue.get("location")))
        where = f"§{idx}" if idx is not None else "(unplaced)"
        rows.append(
            _row(
                "broken_citation",
                issue.get("severity", "error"),
                f"Broken citation in {where}",
                issue.get("detail", ""),
                top_tab="draft",
                section_index=idx,
            )
        )
    return rows


def ledger_unreadable_problem(message: str) -> dict:
    """The single error row emitted when load_ledger raises on a corrupt ledger."""
    return _row(
        "broken_citation",
        "error",
        "Citations ledger unreadable",
        message,
        top_tab="draft",
    )
