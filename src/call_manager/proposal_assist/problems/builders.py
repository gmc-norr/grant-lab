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

from call_manager.proposal_assist.editing.sectionizer import Section


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
    """One warning row per draft section whose byte length exceeds `budget`.

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
