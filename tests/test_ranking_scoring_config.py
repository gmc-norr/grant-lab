"""Spec D3 and D6 (pure parts). Gates G8 (unit), G24 (unit), G25."""

from __future__ import annotations

import dataclasses
import inspect
from types import SimpleNamespace

import pytest

from grant_lab.profiles.fingerprint import FINGERPRINT_VERSION
from grant_lab.ranking import freshness as F
from grant_lab.ranking.freshness import ScoringConfig, next_escalation_state
from grant_lab.ranking.worker import drain_queue

# G25: every drain_queue parameter in exactly one class (spec D3's table).
SCORING_PARAMS = {
    "client",
    "embedding_threshold",
    "embedding_hard_fail_threshold",
    "escalation_min_score",
    "logprobs_enabled",
}
ESCALATION_PARAMS = {"remote_client", "remote_daily_cap"}
NO_EFFECT_PARAMS = {"session_factory", "batch_size", "tick_budget_seconds", "_clock"}


def _cfg(**over):
    base = dict(
        client=SimpleNamespace(model="gemma4:e4b-it-q4_K_M"),
        embedding_threshold=0.30,
        embedding_hard_fail_threshold=0.20,
        escalation_min_score=45,
        logprobs_enabled=False,
    )
    base.update(over)
    return ScoringConfig.for_drain(**base)


def test_every_drain_queue_parameter_is_classified_exactly_once():
    params = set(inspect.signature(drain_queue).parameters)
    classes = (SCORING_PARAMS, ESCALATION_PARAMS, NO_EFFECT_PARAMS)
    assert all(classes), "an empty class makes this gate pass for free"
    assert params == SCORING_PARAMS | ESCALATION_PARAMS | NO_EFFECT_PARAMS, params ^ (
        SCORING_PARAMS | ESCALATION_PARAMS | NO_EFFECT_PARAMS
    )
    assert sum(len(c) for c in classes) == len(params), "a parameter is in two classes"


def test_the_payload_class_is_exactly_what_for_drain_hashes():
    assert set(inspect.signature(ScoringConfig.for_drain).parameters) == SCORING_PARAMS


def test_it_carries_the_version_tag():
    assert _cfg().fingerprint().startswith(f"{FINGERPRINT_VERSION}:")


@pytest.mark.parametrize(
    "over",
    [
        {"client": SimpleNamespace(model="gemma4:e4b-q8")},
        {"embedding_threshold": 0.31},
        {"embedding_hard_fail_threshold": 0.10},  # G24, unit level
        {"escalation_min_score": 50},
        {"logprobs_enabled": True},
    ],
)
def test_every_scoring_setting_changes_it(over):
    assert _cfg(**over).fingerprint() != _cfg().fingerprint()


def test_every_dataclass_field_reaches_the_fingerprint():
    """A field added to ScoringConfig but left out of fingerprint() is a
    setting that changes nothing. Perturb each field directly."""
    base = _cfg()
    for f in dataclasses.fields(ScoringConfig):
        v = getattr(base, f.name)
        bumped = (
            (not v) if isinstance(v, bool) else (v + 1 if isinstance(v, (int, float)) else f"{v}-x")
        )
        assert dataclasses.replace(base, **{f.name: bumped}).fingerprint() != base.fingerprint(), (
            f.name
        )


def test_a_prompt_version_bump_changes_it(monkeypatch):
    """G8, unit level."""
    before = _cfg().fingerprint()
    monkeypatch.setattr(F, "PROMPT_VERSION", "v999")
    assert _cfg().fingerprint() != before


def test_a_scoring_version_bump_changes_it(monkeypatch):
    before = _cfg().fingerprint()
    monkeypatch.setattr(F, "SCORING_VERSION", "999")
    assert _cfg().fingerprint() != before


def test_a_missing_hard_fail_threshold_means_the_single_cut():
    """drain_queue's None collapses to the pregate threshold (criteria.py);
    the fingerprint must say the same thing either way."""
    assert (
        _cfg(embedding_threshold=0.3, embedding_hard_fail_threshold=None).fingerprint()
        == _cfg(embedding_threshold=0.3, embedding_hard_fail_threshold=0.3).fingerprint()
    )


def test_from_settings_matches_the_mapping_main_py_uses():
    """The ingest CLI has no scheduler, so it builds from Settings. main.py maps
    these Settings fields one-to-one into build_scheduler."""
    s = SimpleNamespace(
        gemma_model="gemma4:e4b-it-q4_K_M",
        embedding_pregate_threshold=0.30,
        embedding_hard_fail_threshold=0.20,
        ranking_escalation_min_score=45,
        ranking_logprobs_enabled=False,
    )
    assert ScoringConfig.from_settings(s) == _cfg()


@pytest.mark.parametrize(
    "kw, expected",
    [
        # not wanted / escalated / no remote configured -> nothing outstanding
        (
            dict(
                wanted=False,
                escalated=False,
                remote_configured=True,
                remote_withheld=False,
                outcome=None,
                prev_failures=2,
            ),
            (None, 0),
        ),
        (
            dict(
                wanted=True,
                escalated=True,
                remote_configured=True,
                remote_withheld=False,
                outcome="success",
                prev_failures=2,
            ),
            (None, 0),
        ),
        (
            dict(
                wanted=True,
                escalated=False,
                remote_configured=False,
                remote_withheld=False,
                outcome=None,
                prev_failures=0,
            ),
            (None, 0),
        ),  # G21
        # withheld by the cap -> pending, failures unchanged (G17)
        (
            dict(
                wanted=True,
                escalated=False,
                remote_configured=True,
                remote_withheld=True,
                outcome=None,
                prev_failures=1,
            ),
            ("pending", 1),
        ),
        # service failure -> pending, failures unchanged (G18)
        (
            dict(
                wanted=True,
                escalated=False,
                remote_configured=True,
                remote_withheld=False,
                outcome="llm_error",
                prev_failures=2,
            ),
            ("pending", 2),
        ),
        # this pair's response failed -> +1, gave_up at 3 (G19)
        (
            dict(
                wanted=True,
                escalated=False,
                remote_configured=True,
                remote_withheld=False,
                outcome="parse_error",
                prev_failures=0,
            ),
            ("pending", 1),
        ),
        (
            dict(
                wanted=True,
                escalated=False,
                remote_configured=True,
                remote_withheld=False,
                outcome="parse_error",
                prev_failures=2,
            ),
            ("gave_up", 3),
        ),
    ],
)
def test_next_escalation_state(kw, expected):
    assert next_escalation_state(**kw) == expected
