from call_manager.embeddings.calibration import (
    BGE_M3_DEFAULTS,
    check_threshold_calibration,
)


def test_known_model_never_warns():
    out = check_threshold_calibration(model="bge-m3", pregate=0.30, hard_fail=0.20)
    assert out is None


def test_unknown_model_both_defaults_warns_both():
    out = check_threshold_calibration(
        model="mxbai-embed-large",
        pregate=BGE_M3_DEFAULTS["pregate"],
        hard_fail=BGE_M3_DEFAULTS["hard_fail"],
    )
    assert out is not None
    assert "mxbai-embed-large" in out
    assert "EMBEDDING_PREGATE_THRESHOLD" in out
    assert "EMBEDDING_HARD_FAIL_THRESHOLD" in out


def test_unknown_model_one_overridden_warns_only_other():
    """R8: overriding one threshold must not suppress the warning for the
    other still resting on the bge-m3 default."""
    out = check_threshold_calibration(
        model="mxbai-embed-large", pregate=0.55, hard_fail=BGE_M3_DEFAULTS["hard_fail"]
    )
    assert out is not None
    assert "EMBEDDING_HARD_FAIL_THRESHOLD" in out
    assert "EMBEDDING_PREGATE_THRESHOLD" not in out


def test_unknown_model_both_overridden_no_warn():
    out = check_threshold_calibration(model="mxbai-embed-large", pregate=0.55, hard_fail=0.40)
    assert out is None


def test_tagged_bge_m3_resolves_to_same_calibration():
    """B11 Defect 4c: a tagged model name like 'bge-m3:567m' must resolve to the
    same calibration as the bare 'bge-m3' — otherwise it spuriously warns and
    nulls the cosine gate. The registry is keyed/looked-up on the normalized
    (tag-stripped) name."""
    out = check_threshold_calibration(model="bge-m3:567m", pregate=0.30, hard_fail=0.20)
    assert out is None


def test_tagged_unknown_model_still_warns():
    """Tag-stripping must not over-normalize: an unknown model with a tag still
    warns when thresholds rest on bge-m3 defaults."""
    out = check_threshold_calibration(
        model="mxbai-embed-large:latest", pregate=0.30, hard_fail=0.20
    )
    assert out is not None
    assert "mxbai-embed-large:latest" in out
