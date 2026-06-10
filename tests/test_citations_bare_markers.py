from call_manager.proposal_assist.citations.markers import find_bare_markers


def test_bare_marker_detected():
    out = find_bare_markers("A claim [@smith2021]. More text.")
    assert [(m["citekey"], m["start"]) for m in out] == [("smith2021", 8)]


def test_canonical_marker_not_flagged():
    text = "A claim [@smith2021]<!-- ev:ev-abcdef123456 -->."
    assert find_bare_markers(text) == []


def test_mixed():
    text = "Good [@a1]<!-- ev:ev-aaaaaa111111 --> and bad [@b2] and also [@c-3]."
    assert [m["citekey"] for m in find_bare_markers(text)] == ["b2", "c-3"]
