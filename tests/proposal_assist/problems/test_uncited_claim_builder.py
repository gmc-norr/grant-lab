from call_manager.proposal_assist.problems.builders import uncited_claim_problems


def _claim(h, text="some claim", si=1, reason="needs cite"):
    return {"claim_text": text, "section_index": si, "reason": reason, "claim_hash": h}


def test_emits_warning_rows_with_hash_and_target():
    rows = uncited_claim_problems([_claim("h1")], set())
    assert len(rows) == 1
    r = rows[0]
    assert r["category"] == "uncited_claim" and r["severity"] == "warning"
    assert r["claim_hash"] == "h1"
    assert r["target"] == {"top_tab": "draft", "bottom_tab": "problems", "section_index": 1}


def test_dismissed_are_filtered_out():
    rows = uncited_claim_problems([_claim("h1"), _claim("h2")], {"h1"})
    assert [r["claim_hash"] for r in rows] == ["h2"]


def test_caps_and_appends_rollup_tail():
    claims = [_claim(f"h{i}") for i in range(13)]
    rows = uncited_claim_problems(claims, set(), cap=10)
    assert len(rows) == 11  # 10 capped rows + 1 roll-up
    tail = rows[-1]
    assert tail["title"] == "+ 3 more uncited claims"
    assert "claim_hash" not in tail and tail["target"] is None


def test_truncates_long_title():
    long = "x" * 200
    rows = uncited_claim_problems([_claim("h1", text=long)], set())
    assert len(rows[0]["title"]) <= 80
