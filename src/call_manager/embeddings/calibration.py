"""Embedding-model → threshold calibration registry + startup guard.

The cosine pre-gate thresholds (EMBEDDING_PREGATE_THRESHOLD /
EMBEDDING_HARD_FAIL_THRESHOLD) were calibrated against BGE-M3's similarity
distribution. A different embedding model has a different distribution, so
the same numeric cutoffs mean something different. This module warns when a
threshold still rests on the bge-m3 default while an unrecognized model is
configured. It never refuses to start.
"""

from __future__ import annotations

# The bge-m3-calibrated defaults, mirrored from config.py's Field defaults.
# Keep in lockstep with Settings.embedding_pregate_threshold /
# embedding_hard_fail_threshold.
BGE_M3_DEFAULTS = {"pregate": 0.30, "hard_fail": 0.20}

# Models whose calibration we have validated. Add an entry (with its own
# default pair) when a new model's distribution is characterized.
THRESHOLD_CALIBRATIONS: dict[str, dict[str, float]] = {
    "bge-m3": BGE_M3_DEFAULTS,
}


def check_threshold_calibration(*, model: str, pregate: float, hard_fail: float) -> str | None:
    """Return a warning string when `model` is unrecognized AND at least one
    threshold still equals its bge-m3 default; otherwise None.

    Per-threshold (R8): an operator who overrides only `pregate` still gets
    warned that `hard_fail` is uncalibrated, and vice versa.
    """
    if model in THRESHOLD_CALIBRATIONS:
        return None
    stale: list[str] = []
    if pregate == BGE_M3_DEFAULTS["pregate"]:
        stale.append("EMBEDDING_PREGATE_THRESHOLD")
    if hard_fail == BGE_M3_DEFAULTS["hard_fail"]:
        stale.append("EMBEDDING_HARD_FAIL_THRESHOLD")
    if not stale:
        return None
    return (
        f"Embedding model {model!r} has no validated threshold calibration; "
        f"these settings still rest on bge-m3 defaults and likely mis-gate: "
        f"{', '.join(stale)}. Set them explicitly for {model!r}."
    )
