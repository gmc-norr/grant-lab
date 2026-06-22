"""Deterministic overall-readiness roll-up. The agent judges each criterion's
band; the server computes ONE overall band from the per-criterion bands weighted
by effective weight, with a gap floor (a pure average can read 'strong' while a
meaningful criterion is a gap). Pure — no DB, no LLM, no I/O."""

from __future__ import annotations

BAND_ORDINAL = {"gap": 0, "at_risk": 1, "adequate": 2, "strong": 3}
_ORDINAL_BAND = ["gap", "at_risk", "adequate", "strong"]

# Tunable threshold block. Score in [0,3] -> provisional band.
_THRESHOLDS = ((0.75, "gap"), (1.75, "at_risk"), (2.50, "adequate"))
# A gap on a criterion whose normalized effective weight is >= this caps overall
# at at_risk; any gap at all caps at adequate.
GAP_FLOOR_NORM_WEIGHT = 0.25


def _provisional(score: float) -> str:
    for upper, band in _THRESHOLDS:
        if score < upper:
            return band
    return "strong"


def compute_overall_band(items: list[tuple[str, float]]) -> str:
    """items: list of (band, effective_weight) where effective_weight > 0.
    Empty -> 'gap'."""
    if not items:
        return "gap"
    total_w = sum(w for _, w in items)
    score = sum(BAND_ORDINAL[band] * w for band, w in items) / total_w
    cap = BAND_ORDINAL["strong"]
    for band, w in items:
        if band == "gap":
            cap = min(cap, BAND_ORDINAL["adequate"])
            if (w / total_w) >= GAP_FLOOR_NORM_WEIGHT:
                cap = min(cap, BAND_ORDINAL["at_risk"])
    return _ORDINAL_BAND[min(BAND_ORDINAL[_provisional(score)], cap)]
