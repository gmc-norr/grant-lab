"""The score rule, run over a whole labelled set (spec 2026-11-10).

Each row is [n, topic_fit, eligibility, exclusions, label]: Sonnet's ratings for
profile 1 on 30 open calls (trial of 2026-11-10), and whether the call was wanted.

THE LABELS ARE CLAUDE'S DRAFTS, read and accepted unchanged by the user. They are
not blind human labels, and only 6 of the 30 are wanted. This pins the rule
against the one labelled set that exists; it does not prove the rule is right.
"""

from grant_lab.ranking.aggregate import LLMResult, aggregate
from grant_lab.ranking.criteria import GateResult, Gates, GateStatus

ROWS = [
    [1, 1, "fail", "pass", "no"],
    [2, 0, "unclear", "pass", "no"],
    [3, 0, "fail", "pass", "no"],
    [4, 0, "unclear", "pass", "no"],
    [5, 0, "fail", "pass", "no"],
    [6, 0, "unclear", "pass", "no"],
    [7, 1, "unclear", "pass", "no"],
    [8, 0, "unclear", "pass", "no"],
    [9, 1, "unclear", "pass", "maybe"],
    [10, 0, "unclear", "pass", "no"],
    [11, 1, "unclear", "pass", "no"],
    [12, 0, "unclear", "pass", "no"],
    [13, 0, "unclear", "pass", "no"],
    [14, 0, "fail", "pass", "no"],
    [15, 0, "unclear", "pass", "no"],
    [16, 1, "unclear", "pass", "no"],
    [17, 3, "unclear", "pass", "good"],
    [18, 3, "unclear", "pass", "maybe"],
    [19, 0, "unclear", "pass", "no"],
    [20, 0, "unclear", "pass", "no"],
    [21, 0, "fail", "pass", "no"],
    [22, 3, "pass", "pass", "maybe"],
    [23, 1, "unclear", "pass", "no"],
    [24, 1, "fail", "pass", "no"],
    [25, 1, "unclear", "pass", "no"],
    [26, 2, "fail", "pass", "maybe"],
    [27, 0, "unclear", "pass", "no"],
    [28, 2, "fail", "pass", "no"],
    [29, 2, "unclear", "pass", "no"],
    [30, 4, "fail", "pass", "maybe"],
]

_GATES = Gates(
    timeline=GateResult(GateStatus.PASS),
    budget=GateResult(GateStatus.PASS),
    trl=GateResult(GateStatus.PASS),
    embedding=GateResult(GateStatus.UNKNOWN),
)


def _auc(scores, wanted):
    """Chance that a wanted call outscores an unwanted one; a tie counts half."""
    pos = [s for s, w in zip(scores, wanted, strict=True) if w]
    neg = [s for s, w in zip(scores, wanted, strict=True) if not w]
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def test_the_labelled_set_is_the_one_the_trial_measured():
    wanted = [row[4] != "no" for row in ROWS]
    assert (len(ROWS), sum(wanted)) == (30, 6)


def test_wanted_calls_outscore_unwanted_ones_on_the_labelled_set():
    scores = [
        aggregate(_GATES, LLMResult(topic_argmax=t, eligibility=e, exclusions=x)).score
        for _, t, e, x, _ in ROWS
    ]
    wanted = [row[4] != "no" for row in ROWS]
    # Measured: 0.955 with this rule, 0.767 with the rule it replaced.
    assert _auc(scores, wanted) >= 0.9
