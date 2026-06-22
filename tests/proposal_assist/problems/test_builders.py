from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.problems.builders import (
    broken_citation_problems,
    coherence_drift_problem,
    count_by_severity,
    criterion_gap_problems,
    ledger_unreadable_problem,
    order_problems,
    over_length_problems,
    section_index_for_offset,
)


def test_over_length_flags_only_sections_above_budget():
    doc = "## A\n" + "x" * 50 + "\n## B\n" + "y" * 5 + "\n"
    sections = split_sections(doc)  # [0]="" preamble, [1]="## A\n…", [2]="## B\n…"
    rows = over_length_problems(sections, budget=40)
    assert len(rows) == 1
    r = rows[0]
    assert r["category"] == "over_length"
    assert r["severity"] == "warning"
    assert r["title"] == "Over-length §1"
    assert r["detail"] == "56 chars (budget 40)"
    assert r["target"] == {"top_tab": "draft", "bottom_tab": None, "section_index": 1}


def test_over_length_budget_zero_or_negative_disables():
    sections = split_sections("## A\n" + "x" * 999 + "\n")
    assert over_length_problems(sections, budget=0) == []
    assert over_length_problems(sections, budget=-1) == []


def test_section_index_for_offset_maps_offset_to_section():
    doc = "preamble\n## Methods\nblah [@x] more\n"  # preamble=§0 (9 chars), §1 from 9
    assert section_index_for_offset(doc, 0) == 0
    assert section_index_for_offset(doc, doc.index("[@x]")) == 1
    assert section_index_for_offset(doc, None) is None


def test_broken_citation_maps_orphan_marker_with_section_and_orphan_row_unplaced():
    doc = "preamble\n## Methods\nblah [@x] more\n"
    issues = [
        {
            "type": "orphan_marker",
            "severity": "error",
            "citekey": "x",
            "evidence_id": "e1",
            "location": f"char {doc.index('[@x]')}",
            "detail": "inline citation marker has no matching ledger row",
        },
        {
            "type": "orphan_row",
            "severity": "error",
            "citekey": "y",
            "evidence_id": "e2",
            "location": "citekey y",
            "detail": "ledger row has no inline marker in proposal.md",
        },
    ]
    rows = broken_citation_problems(issues, doc)
    assert len(rows) == 2
    marker, orphan = rows
    assert marker["category"] == "broken_citation" and marker["severity"] == "error"
    assert marker["title"] == "Broken citation in §1"
    assert marker["target"] == {"top_tab": "draft", "bottom_tab": None, "section_index": 1}
    assert orphan["title"] == "Broken citation in (unplaced)"
    assert orphan["target"] == {"top_tab": "draft", "bottom_tab": None, "section_index": None}


def test_ledger_unreadable_is_one_error_row():
    r = ledger_unreadable_problem("citations.json is not valid JSON")
    assert r["category"] == "broken_citation" and r["severity"] == "error"
    assert r["title"] == "Citations ledger unreadable"
    assert r["detail"] == "citations.json is not valid JSON"
    assert r["target"] == {"top_tab": "draft", "bottom_tab": None, "section_index": None}


def test_criterion_gap_only_gap_and_at_risk_with_targets():
    scorecard = {
        "criteria": [
            {
                "index": 0,
                "name": "Quality",
                "band": "strong",
                "note": "ok",
                "deep_link": None,
                "weight": 0.5,
                "effective_weight": 0.5,
            },
            {
                "index": 1,
                "name": "Impact",
                "band": "gap",
                "note": "No KPIs.",
                "deep_link": {"artifact": "draft", "section_index": 2},
                "weight": 0.3,
                "effective_weight": 0.3,
            },
            {
                "index": 2,
                "name": "Feasibility",
                "band": "at_risk",
                "note": "Thin plan.",
                "deep_link": None,
                "weight": 0.2,
                "effective_weight": 0.2,
            },
        ],
        "overall_band": "at_risk",
        "summary": "",
    }
    rows = criterion_gap_problems(scorecard)
    assert len(rows) == 2
    gap, at_risk = rows
    assert gap["severity"] == "error"
    assert gap["title"] == "Criterion gap: Impact"
    assert gap["detail"] == "No KPIs."
    assert gap["target"] == {"top_tab": "draft", "bottom_tab": None, "section_index": 2}
    assert at_risk["severity"] == "warning"
    assert at_risk["title"] == "Criterion at-risk: Feasibility"
    # no deep_link -> Scorecard tab fallback, never top_tab None
    assert at_risk["target"] == {"top_tab": "scorecard", "bottom_tab": None, "section_index": None}


def test_criterion_gap_no_scorecard_is_empty():
    assert criterion_gap_problems(None) == []
    assert criterion_gap_problems({"criteria": []}) == []


def test_coherence_drift_single_rollup_or_none():
    assert coherence_drift_problem(0) is None
    r = coherence_drift_problem(3)
    assert r["category"] == "coherence_drift" and r["severity"] == "warning"
    assert r["title"] == "Coherence: 3 spec↔draft item(s) pending"
    assert r["target"] == {"top_tab": None, "bottom_tab": "inbox", "section_index": None}


def test_order_problems_errors_first_then_category_nulls_last_no_crash():
    rows = [
        coherence_drift_problem(2),  # warning, category 3
        {
            "category": "criterion_gap",
            "severity": "error",
            "title": "b",
            "detail": "",
            "target": {"top_tab": "scorecard", "bottom_tab": None, "section_index": None},
        },
        {
            "category": "broken_citation",
            "severity": "error",
            "title": "a",
            "detail": "",
            "target": {"top_tab": "draft", "bottom_tab": None, "section_index": 5},
        },
        {
            "category": "broken_citation",
            "severity": "error",
            "title": "a",
            "detail": "",
            "target": {"top_tab": "draft", "bottom_tab": None, "section_index": None},
        },
    ]
    ordered = order_problems(rows)  # must NOT raise on mixed int/None section_index
    # errors first; within errors, category order (broken_citation < criterion_gap);
    # within broken_citation, the int section_index sorts before the null one.
    assert [r["category"] for r in ordered] == [
        "broken_citation",
        "broken_citation",
        "criterion_gap",
        "coherence_drift",
    ]
    assert ordered[0]["target"]["section_index"] == 5
    assert ordered[1]["target"]["section_index"] is None


def test_count_by_severity():
    rows = [
        {"severity": "error"},
        {"severity": "error"},
        {"severity": "warning"},
    ]
    assert count_by_severity(rows) == {"error": 2, "warning": 1}


def test_row_coerces_malformed_scorecard_fields_so_ordering_cannot_500():
    # A completed scorecard's JSONB can carry null/str fields (hand-edited DB, future
    # bug). null note -> "" and a string section_index -> None, so the sort key never
    # compares None/str (the bug Codex reproduced).
    scorecard = {
        "criteria": [
            {
                "index": 0,
                "name": "A",
                "band": "gap",
                "note": None,
                "deep_link": {"artifact": "draft", "section_index": "2"},
                "weight": 1,
                "effective_weight": 1,
            },
            {
                "index": 1,
                "name": "B",
                "band": "at_risk",
                "note": "ok",
                "deep_link": None,
                "weight": 1,
                "effective_weight": 1,
            },
        ],
    }
    rows = criterion_gap_problems(scorecard)
    assert rows[0]["detail"] == ""  # null note coerced
    assert rows[0]["target"]["section_index"] is None  # string "2" coerced to None
    # The real regression: ordering these rows must NOT raise.
    ordered = order_problems(rows)
    assert len(ordered) == 2
