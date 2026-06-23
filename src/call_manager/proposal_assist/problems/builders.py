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
    claim_hash: str | None = None,
) -> dict:
    # Coerce untrusted inputs (a completed scorecard's JSONB can carry null/str
    # fields) so a malformed row can never break order_problems' sort key, which
    # compares title/detail/section_index. title/detail -> str; section_index ->
    # int|None (bool excluded — bool is an int subclass). Preserves the never-500
    # contract even for a hand-corrupted scorecard.
    si = (
        section_index
        if isinstance(section_index, int) and not isinstance(section_index, bool)
        else None
    )
    target = None
    if top_tab is not None or bottom_tab is not None or si is not None:
        target = {"top_tab": top_tab, "bottom_tab": bottom_tab, "section_index": si}
    row = {
        "category": category,
        "severity": severity,
        "title": "" if title is None else str(title),
        "detail": "" if detail is None else str(detail),
        "target": target,
    }
    # Additive: only uncited_claim rows carry a claim_hash (so the panel can dismiss them).
    # Existing rows omit the key entirely.
    if claim_hash is not None:
        row["claim_hash"] = claim_hash
    return row


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

    Uses split_sections cumulative spans (the character-exact join invariant). An offset
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


_GAP_SEVERITY = {"gap": "error", "at_risk": "warning"}
_GAP_LABEL = {"gap": "gap", "at_risk": "at-risk"}


def criterion_gap_problems(scorecard: dict | None) -> list[dict]:
    """One row per scorecard criterion banded gap (error) or at_risk (warning)."""
    if not scorecard:
        return []
    rows: list[dict] = []
    for c in scorecard.get("criteria") or []:
        band = c.get("band")
        if band not in _GAP_SEVERITY:
            continue
        dl = c.get("deep_link")
        if dl and dl.get("artifact"):
            top_tab, section_index = dl.get("artifact"), dl.get("section_index")
        else:
            top_tab, section_index = "scorecard", None
        rows.append(
            _row(
                "criterion_gap",
                _GAP_SEVERITY[band],
                f"Criterion {_GAP_LABEL[band]}: {c.get('name', '')}",
                c.get("note", ""),
                top_tab=top_tab,
                section_index=section_index,
            )
        )
    return rows


def coherence_drift_problem(count: int) -> dict | None:
    """A single warning roll-up pointing at the Inbox; None when count <= 0."""
    if count <= 0:
        return None
    return _row(
        "coherence_drift",
        "warning",
        f"Coherence: {count} spec↔draft item(s) pending",
        "Open the Inbox to review.",
        bottom_tab="inbox",
    )


def _truncate(text: str, n: int = 80) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def uncited_claim_problems(claims: list[dict], dismissed: set[str], *, cap: int = 10) -> list[dict]:
    """Up to `cap` warning rows for the latest completed uncited-claims result, filtering
    out dismissed claim_hashes; if more remain, one '+ N more' roll-up tail row. Pure — no
    `stale` parameter (staleness is the separate top-level uncited_claims_stale flag)."""
    kept = [c for c in (claims or []) if c.get("claim_hash") not in dismissed]
    rows: list[dict] = []
    for c in kept[:cap]:
        rows.append(
            _row(
                "uncited_claim",
                "warning",
                _truncate(c.get("claim_text") or ""),
                c.get("reason") or "",
                top_tab="draft",
                bottom_tab="problems",
                section_index=c.get("section_index"),
                claim_hash=c.get("claim_hash"),
            )
        )
    extra = len(kept) - cap
    if extra > 0:
        rows.append(
            _row(
                "uncited_claim",
                "warning",
                f"+ {extra} more uncited claims",
                "",
            )
        )
    return rows


_SEVERITY_RANK = {"error": 0, "warning": 1}
_CATEGORY_RANK = {
    "broken_citation": 0,
    "criterion_gap": 1,
    "over_length": 2,
    "coherence_drift": 3,
    "uncited_claim": 4,
}


def _sort_key(row: dict) -> tuple:
    target = row.get("target") or {}
    si = target.get("section_index")
    # Never compare None: the `si is None` term partitions nulls last; `si or 0` is
    # only meaningful for the non-null group (0 is a safe filler for the null group).
    return (
        _SEVERITY_RANK.get(row["severity"], 9),
        _CATEGORY_RANK.get(row["category"], 9),
        si is None,
        si or 0,
        row["title"],
        row["detail"],
    )


def order_problems(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=_sort_key)


def count_by_severity(rows: list[dict]) -> dict:
    return {
        "error": sum(1 for r in rows if r["severity"] == "error"),
        "warning": sum(1 for r in rows if r["severity"] == "warning"),
    }
