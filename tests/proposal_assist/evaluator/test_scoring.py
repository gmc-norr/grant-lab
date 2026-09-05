from grant_lab.proposal_assist.evaluator.scoring import compute_overall_band


def test_empty_is_gap():
    assert compute_overall_band([]) == "gap"


def test_all_strong_is_strong():
    assert compute_overall_band([("strong", 1.0), ("strong", 1.0)]) == "strong"


def test_all_gap_is_gap():
    assert compute_overall_band([("gap", 1.0)]) == "gap"  # score 0 (< 0.75)


def test_threshold_2_50_inclusive_is_strong():
    # (2 + 3)/2 = 2.50 -> strong (>= 2.50). No gap, so the floor is inert.
    assert compute_overall_band([("adequate", 1.0), ("strong", 1.0)]) == "strong"


def test_just_below_2_50_is_adequate():
    # (2*3 + 3*1)/4 = 2.25 -> adequate
    assert compute_overall_band([("adequate", 3.0), ("strong", 1.0)]) == "adequate"


def test_threshold_1_75_inclusive_is_adequate():
    # 1*0.25 + 2*0.75 = 1.75 -> adequate (>= 1.75). Fractional weights, no gap.
    assert compute_overall_band([("at_risk", 0.25), ("adequate", 0.75)]) == "adequate"


def test_just_below_1_75_is_at_risk():
    # (1 + 2)/2 = 1.50 -> at_risk
    assert compute_overall_band([("at_risk", 1.0), ("adequate", 1.0)]) == "at_risk"


def test_low_weight_gap_caps_at_adequate():
    # avg ~ (3*0.9 + 0*0.1) = 2.7 -> provisional strong; one gap (norm 0.1 < 0.25)
    # -> capped at adequate.
    assert compute_overall_band([("strong", 0.9), ("gap", 0.1)]) == "adequate"


def test_high_weight_gap_caps_at_at_risk():
    # one gap at norm weight 0.4 (>= 0.25) -> capped at at_risk even though the
    # weighted average is high.
    assert compute_overall_band([("strong", 0.6), ("gap", 0.4)]) == "at_risk"


def test_weight_fallback_is_handled_by_caller():
    # compute_overall_band receives already-effective weights (> 0); equal weights work.
    assert compute_overall_band([("adequate", 1.0), ("adequate", 1.0)]) == "adequate"
