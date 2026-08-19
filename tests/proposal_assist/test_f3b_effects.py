"""F3b — the semantic effect operations. Each owns its predicate, transaction
and commit; the tool that will call it holds no session at all."""

from __future__ import annotations

import pytest

from call_manager.db.models import EvaluatorJob, ReviewRunItem, UncitedClaimsJob
from call_manager.proposal_assist.port_impl.effects import SqlEffects
from call_manager.proposal_assist.ports.effects import (
    EFFECT_MECHANISMS,
    Effects,
    SubmitReview,
    SubmitScorecard,
    SubmitUncitedClaims,
)
from call_manager.proposal_assist.ports.errors import (
    EffectRejected,
    WorkspaceGenerationStale,
)
from call_manager.proposal_assist.ports.stamp import FenceStamp
from tests.proposal_assist.f3b_seeds import (
    bump_generation,
    seed_evaluator_job,
    seed_review_item,
    seed_uncited_job,
    seed_workspace,
)

_CARD = {"criteria": [], "overall_band": "adequate", "summary": "ok"}


def _stamp(ws_id, *, generation, origin_job_id=None) -> FenceStamp:
    return FenceStamp(
        workspace_id=ws_id,
        generation=generation,
        active_turn_job_id=None,
        origin_job_id=origin_job_id,
    )


# --------------------------------------------------------------- the contract


def test_every_effect_declares_its_mechanism():
    """Spec 3.5: a named port does not make its fence atomic. Each operation
    states WHICH mechanism it uses, and this test is what stops the table from
    drifting away from the code as operations are added."""
    declared = set(EFFECT_MECHANISMS)
    protocol_methods = {n for n in vars(Effects) if not n.startswith("_")}
    assert declared == protocol_methods, (
        f"undeclared: {sorted(protocol_methods - declared)}; "
        f"stale: {sorted(declared - protocol_methods)}"
    )


def test_the_implementation_satisfies_the_protocol(pg_session_factory):
    assert isinstance(SqlEffects(pg_session_factory), Effects)


def test_no_effect_returns_a_session_or_an_orm_instance(pg_session_factory):
    """GC-1. A returned ORM instance carries object_session(instance), which is
    the full Session again."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        job_id = seed_evaluator_job(s, ws_id, generation=1)
    result = SqlEffects(pg_session_factory).submit_scorecard(
        _stamp(ws_id, generation=1, origin_job_id=job_id), SubmitScorecard(scorecard=_CARD)
    )
    assert result is None


# ------------------------------------------------------------ submit_scorecard


def test_submit_scorecard_lands_at_the_claimed_generation(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=4)
        job_id = seed_evaluator_job(s, ws_id, generation=4)

    SqlEffects(pg_session_factory).submit_scorecard(
        _stamp(ws_id, generation=4, origin_job_id=job_id), SubmitScorecard(scorecard=_CARD)
    )

    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, job_id).scorecard == _CARD


def test_submit_scorecard_refuses_a_stale_generation(pg_session_factory):
    """The field, not the row count. A stale write onto an existing row leaves
    rowcount unchanged, so counting rows would pass a broken implementation."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=4)
        job_id = seed_evaluator_job(s, ws_id, generation=4)
        bump_generation(s, ws_id, to=6)
        s.commit()

    with pytest.raises(WorkspaceGenerationStale) as e:
        SqlEffects(pg_session_factory).submit_scorecard(
            _stamp(ws_id, generation=4, origin_job_id=job_id), SubmitScorecard(scorecard=_CARD)
        )
    assert (e.value.expected, e.value.actual) == (4, 6)

    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, job_id).scorecard is None


def test_submit_scorecard_refuses_a_soft_deleted_workspace(pg_session_factory):
    from datetime import UTC, datetime

    from call_manager.db.models import Workspace

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=4)
        job_id = seed_evaluator_job(s, ws_id, generation=4)
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    with pytest.raises(WorkspaceGenerationStale):
        SqlEffects(pg_session_factory).submit_scorecard(
            _stamp(ws_id, generation=4, origin_job_id=job_id), SubmitScorecard(scorecard=_CARD)
        )
    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, job_id).scorecard is None


def test_submit_scorecard_is_exactly_once(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        job_id = seed_evaluator_job(s, ws_id, generation=1)

    effects = SqlEffects(pg_session_factory)
    stamp = _stamp(ws_id, generation=1, origin_job_id=job_id)
    effects.submit_scorecard(stamp, SubmitScorecard(scorecard=_CARD))
    with pytest.raises(EffectRejected) as e:
        effects.submit_scorecard(stamp, SubmitScorecard(scorecard={"overall_band": "gap"}))
    assert e.value.reason == "a scorecard has already been submitted for this run"

    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, job_id).scorecard == _CARD


def test_submit_scorecard_refuses_another_workspaces_job(pg_session_factory):
    with pg_session_factory() as s:
        mine = seed_workspace(s, generation=1)
        theirs = seed_workspace(s, generation=1)
        their_job = seed_evaluator_job(s, theirs, generation=1)

    with pytest.raises(EffectRejected) as e:
        SqlEffects(pg_session_factory).submit_scorecard(
            _stamp(mine, generation=1, origin_job_id=their_job),
            SubmitScorecard(scorecard=_CARD),
        )
    assert e.value.reason == (
        "scorecard target is not an active evaluator run for this workspace"
    )
    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, their_job).scorecard is None


def test_submit_scorecard_refuses_a_job_that_is_no_longer_running(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        job_id = seed_evaluator_job(s, ws_id, generation=1)
        # ck_evaluator_jobs_status only permits queued/running/completed/failed —
        # "cancelled" is not a legal EvaluatorJob status, so "completed" stands
        # in for "no longer running".
        s.get(EvaluatorJob, job_id).status = "completed"
        s.commit()

    with pytest.raises(EffectRejected) as e:
        SqlEffects(pg_session_factory).submit_scorecard(
            _stamp(ws_id, generation=1, origin_job_id=job_id), SubmitScorecard(scorecard=_CARD)
        )
    assert e.value.reason == "scorecard target is not an active evaluator run for this workspace"

    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, job_id).scorecard is None


# -------------------------------------------------------------- submit_review


def test_submit_review_lands_on_the_running_item(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=2)
        run_id, item_id = seed_review_item(s, ws_id, generation=2)

    SqlEffects(pg_session_factory).submit_review(
        _stamp(ws_id, generation=2, origin_job_id=run_id),
        SubmitReview(review_run_item_id=item_id, markdown="a review"),
    )

    with pg_session_factory() as s:
        assert s.get(ReviewRunItem, item_id).raw_response_json == {"markdown": "a review"}


def test_submit_review_refuses_a_stale_generation(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=2)
        run_id, item_id = seed_review_item(s, ws_id, generation=2)
        bump_generation(s, ws_id, to=3)
        s.commit()

    with pytest.raises(WorkspaceGenerationStale):
        SqlEffects(pg_session_factory).submit_review(
            _stamp(ws_id, generation=2, origin_job_id=run_id),
            SubmitReview(review_run_item_id=item_id, markdown="a review"),
        )
    with pg_session_factory() as s:
        assert s.get(ReviewRunItem, item_id).raw_response_json is None


def test_submit_review_refuses_an_item_from_another_workspaces_run(pg_session_factory):
    """Ownership runs through the PARENT run — review_run_items has no
    workspace_id of its own."""
    with pg_session_factory() as s:
        mine = seed_workspace(s, generation=1)
        theirs = seed_workspace(s, generation=1)
        their_run, their_item = seed_review_item(s, theirs, generation=1)

    with pytest.raises(EffectRejected) as e:
        SqlEffects(pg_session_factory).submit_review(
            _stamp(mine, generation=1, origin_job_id=their_run),
            SubmitReview(review_run_item_id=their_item, markdown="x"),
        )
    assert e.value.reason == "submit_review target run is not active for this workspace"
    with pg_session_factory() as s:
        assert s.get(ReviewRunItem, their_item).raw_response_json is None


def test_submit_review_refuses_an_item_that_is_not_running(pg_session_factory):
    """The item exists under the correct, active run for this workspace, but its
    own status has already moved past "running" — ck_review_run_items_status
    permits 'done' as a legal non-running value."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        run_id, item_id = seed_review_item(s, ws_id, generation=1)
        s.get(ReviewRunItem, item_id).status = "done"
        s.commit()

    with pytest.raises(EffectRejected) as e:
        SqlEffects(pg_session_factory).submit_review(
            _stamp(ws_id, generation=1, origin_job_id=run_id),
            SubmitReview(review_run_item_id=item_id, markdown="x"),
        )
    assert e.value.reason == "submit_review target is not a running item for this run"

    with pg_session_factory() as s:
        assert s.get(ReviewRunItem, item_id).raw_response_json is None


def test_submit_review_is_exactly_once(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        run_id, item_id = seed_review_item(s, ws_id, generation=1)

    effects = SqlEffects(pg_session_factory)
    stamp = _stamp(ws_id, generation=1, origin_job_id=run_id)
    effects.submit_review(stamp, SubmitReview(review_run_item_id=item_id, markdown="first"))
    with pytest.raises(EffectRejected) as e:
        effects.submit_review(stamp, SubmitReview(review_run_item_id=item_id, markdown="second"))
    assert e.value.reason == "a review has already been submitted for this item"

    with pg_session_factory() as s:
        assert s.get(ReviewRunItem, item_id).raw_response_json == {"markdown": "first"}



def test_a_foreign_item_that_already_has_a_review_still_reports_the_run(
    pg_session_factory,
):
    """REGRESSION on classifier ORDERING. Checking raw_response_json before the
    parent run's ownership would confirm, to a caller holding another
    workspace's item id, that a review already exists there — and would change
    the message review_submit.py returns today."""
    with pg_session_factory() as s:
        mine = seed_workspace(s, generation=1)
        theirs = seed_workspace(s, generation=1)
        their_run, their_item = seed_review_item(s, theirs, generation=1)
        s.get(ReviewRunItem, their_item).raw_response_json = {"markdown": "theirs"}
        s.commit()

    with pytest.raises(EffectRejected) as e:
        SqlEffects(pg_session_factory).submit_review(
            _stamp(mine, generation=1, origin_job_id=their_run),
            SubmitReview(review_run_item_id=their_item, markdown="x"),
        )
    assert e.value.reason == "submit_review target run is not active for this workspace"

    with pg_session_factory() as s:
        assert s.get(ReviewRunItem, their_item).raw_response_json == {"markdown": "theirs"}

# ------------------------------------------------------- submit_uncited_claims


def test_submit_uncited_claims_lands_at_the_claimed_generation(pg_session_factory):
    claims = [{"claim_text": "x", "section_index": 0, "reason": "y"}]
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=9)
        job_id = seed_uncited_job(s, ws_id, generation=9)

    SqlEffects(pg_session_factory).submit_uncited_claims(
        _stamp(ws_id, generation=9, origin_job_id=job_id), SubmitUncitedClaims(claims=claims)
    )

    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).claims == claims


def test_submit_uncited_claims_refuses_a_stale_generation(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=9)
        job_id = seed_uncited_job(s, ws_id, generation=9)
        bump_generation(s, ws_id, to=10)
        s.commit()

    with pytest.raises(WorkspaceGenerationStale):
        SqlEffects(pg_session_factory).submit_uncited_claims(
            _stamp(ws_id, generation=9, origin_job_id=job_id), SubmitUncitedClaims(claims=[])
        )
    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).claims is None


def test_submit_uncited_claims_refuses_another_workspaces_job(pg_session_factory):
    with pg_session_factory() as s:
        mine = seed_workspace(s, generation=1)
        theirs = seed_workspace(s, generation=1)
        their_job = seed_uncited_job(s, theirs, generation=1)

    with pytest.raises(EffectRejected) as e:
        SqlEffects(pg_session_factory).submit_uncited_claims(
            _stamp(mine, generation=1, origin_job_id=their_job),
            SubmitUncitedClaims(claims=[]),
        )
    assert e.value.reason == "target is not an active uncited-claims run for this workspace"
    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, their_job).claims is None


def test_submit_uncited_claims_accepts_an_empty_list_exactly_once(pg_session_factory):
    """An empty list is a real result — "I found nothing" — and must not be
    confused with "not submitted yet"."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=0)
        job_id = seed_uncited_job(s, ws_id, generation=0)

    effects = SqlEffects(pg_session_factory)
    stamp = _stamp(ws_id, generation=0, origin_job_id=job_id)
    effects.submit_uncited_claims(stamp, SubmitUncitedClaims(claims=[]))
    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).claims == []
    with pytest.raises(EffectRejected) as e:
        effects.submit_uncited_claims(stamp, SubmitUncitedClaims(claims=[]))
    assert e.value.reason == "claims have already been submitted for this run"


def test_generation_zero_is_fenced_like_any_other(pg_session_factory):
    """Workspace.lifecycle_generation has server_default 0. A `not generation`
    check anywhere in the chain would wave a fresh workspace straight through."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=0)
        job_id = seed_uncited_job(s, ws_id, generation=0)
        bump_generation(s, ws_id, to=1)
        s.commit()

    with pytest.raises(WorkspaceGenerationStale) as e:
        SqlEffects(pg_session_factory).submit_uncited_claims(
            _stamp(ws_id, generation=0, origin_job_id=job_id), SubmitUncitedClaims(claims=[])
        )
    assert (e.value.expected, e.value.actual) == (0, 1)

    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).claims is None
