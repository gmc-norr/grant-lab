from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.problems.builders import over_length_problems


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
