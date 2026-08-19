"""F3b — the semantic effect operations. Each owns its predicate, transaction
and commit; the tool that will call it holds no session at all."""

from __future__ import annotations

import uuid

import pytest

from call_manager.db.models import (
    ChatTurn,
    EvaluatorJob,
    ProposedChange,
    ReviewRunItem,
    UncitedClaimsJob,
)
from call_manager.proposal_assist.port_impl.effects import SqlEffects
from call_manager.proposal_assist.ports.effects import (
    EFFECT_MECHANISMS,
    Effects,
    ProposeArtifactEdit,
    ProposeCitationInsert,
    SubmitReview,
    SubmitScorecard,
    SubmitUncitedClaims,
)
from call_manager.proposal_assist.ports.errors import (
    EffectRejected,
    WorkspaceGenerationStale,
)
from call_manager.proposal_assist.ports.stamp import FenceStamp, Provenance
from tests.proposal_assist.conftest import make_user
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


def test_a_finished_scorecard_job_reports_the_target_not_the_resubmit(pg_session_factory):
    """REGRESSION on classifier ORDERING, the scorecard twin of
    test_a_foreign_item_that_already_has_a_review_still_reports_the_run.

    scorecard.py checks "is this THIS workspace's still-running job" BEFORE it
    checks exactly-once, so a job that has both finished AND already been
    submitted reports the target. Classifying exactly-once first changes the
    string the agent sees for a stale ctx pointing at a long-finished run."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        job_id = seed_evaluator_job(s, ws_id, generation=1)
        job = s.get(EvaluatorJob, job_id)
        job.scorecard = _CARD
        job.status = "completed"
        s.commit()

    with pytest.raises(EffectRejected) as e:
        SqlEffects(pg_session_factory).submit_scorecard(
            _stamp(ws_id, generation=1, origin_job_id=job_id),
            SubmitScorecard(scorecard={"overall_band": "gap"}),
        )
    assert e.value.reason == "scorecard target is not an active evaluator run for this workspace"

    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, job_id).scorecard == _CARD


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


def test_a_finished_claims_job_reports_the_target_not_the_resubmit(pg_session_factory):
    """REGRESSION on classifier ORDERING — the uncited-claims twin of
    test_a_finished_scorecard_job_reports_the_target_not_the_resubmit.
    uncited_claims.py checks the target before exactly-once; so must this."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        job_id = seed_uncited_job(s, ws_id, generation=1)
        job = s.get(UncitedClaimsJob, job_id)
        job.claims = []
        job.status = "completed"
        s.commit()

    with pytest.raises(EffectRejected) as e:
        SqlEffects(pg_session_factory).submit_uncited_claims(
            _stamp(ws_id, generation=1, origin_job_id=job_id),
            SubmitUncitedClaims(claims=[{"claim_text": "x", "section_index": 0, "reason": "y"}]),
        )
    assert e.value.reason == "target is not an active uncited-claims run for this workspace"

    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).claims == []


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


# ------------------------------------------------------- the propose_* inserts

_PROV = Provenance(
    origin_kind="interactive_turn",
    origin_session_id="sess-9",
    # triggering_turn_id and created_by ARE foreign keys (chat_turns.id,
    # users.id — models.py, fk_pc_turn / fk_pc_created_by). Every other test
    # in this section inserts a real row using this baseline Provenance, so
    # it has to stay FK-satisfiable, and None is the only value that is
    # without seeding a row per test. test_propose_artifact_edit_copies_the_
    # provenance_verbatim below seeds a real ChatTurn and a real User and
    # builds its own Provenance to exercise both fields non-NULL.
    triggering_turn_id=None,
    triggering_prompt="tighten the aims",
    created_by=None,
)


def _edit(**over) -> ProposeArtifactEdit:
    base = dict(
        target_artifact="draft",
        section_index=1,
        new_text="new prose",
        base_commit_sha="a" * 40,
        base_section_sha="b" * 64,
        provenance=_PROV,
    )
    return ProposeArtifactEdit(**{**base, **over})


def test_propose_artifact_edit_inserts_a_pending_row(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=5)

    pc_id = SqlEffects(pg_session_factory).propose_artifact_edit(
        _stamp(ws_id, generation=5), _edit()
    )

    with pg_session_factory() as s:
        pc = s.get(ProposedChange, pc_id)
        assert (pc.workspace_id, pc.status, pc.kind) == (ws_id, "pending", "prose_edit")
        assert (pc.target_artifact, pc.section_index) == ("draft", 1)
        assert (pc.base_commit_sha, pc.base_section_sha) == ("a" * 40, "b" * 64)
        assert pc.payload == {"new_text": "new prose"}
        assert pc.created_at is not None


def test_propose_artifact_edit_copies_the_provenance_verbatim(pg_session_factory):
    """These fields are durable attribution. A snapshot taken late — or dropped
    on the way through the port — misattributes stored data.

    triggering_turn_id and created_by are foreign keys, so — unlike _PROV's
    module-level baseline, which stays FK-free to keep every other propose_*
    test's INSERT valid — this test seeds a real ChatTurn and a real User and
    builds its own Provenance with all five fields populated.

    origin_job_id is asserted separately, against the STAMP rather than
    against this Provenance: Provenance no longer carries the field at all
    (ports/stamp.py) — there is exactly one source of truth, the same one the
    fence predicate authorized the INSERT against, so nothing can misattribute
    it. The stamp built here carries its own real origin_job_id, distinct from
    every FK-safe None elsewhere in this Provenance, so the assertion below is
    only satisfiable if the write really reads the stamp.
    """
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        turn = ChatTurn(workspace_id=ws_id, role="user", content={"type": "text", "text": "hi"})
        s.add(turn)
        s.flush()
        turn_id = turn.id
        author_id = make_user(s, email="provenance-author@ki.se").id
        s.commit()

    prov = Provenance(
        origin_kind=_PROV.origin_kind,
        origin_session_id=_PROV.origin_session_id,
        triggering_turn_id=turn_id,
        triggering_prompt=_PROV.triggering_prompt,
        created_by=author_id,
    )
    origin_job = uuid.uuid4()
    stamp = _stamp(ws_id, generation=1, origin_job_id=origin_job)

    pc_id = SqlEffects(pg_session_factory).propose_artifact_edit(stamp, _edit(provenance=prov))

    with pg_session_factory() as s:
        pc = s.get(ProposedChange, pc_id)
        assert pc.origin_kind == "interactive_turn"
        assert pc.origin_session_id == "sess-9"
        assert pc.triggering_prompt == "tighten the aims"
        assert pc.origin_job_id == origin_job
        assert pc.triggering_turn_id == turn_id
        assert pc.created_by == author_id


def _citation(**over) -> ProposeCitationInsert:
    base = dict(
        section_index=0,
        base_commit_sha="c" * 40,
        base_section_sha="d" * 64,
        reference_id=uuid.uuid4(),
        anchor="a",
        chunk_index=0,
        evidence_quote="q",
        stance="supports",
        confidence=None,
        provenance=_PROV,
    )
    return ProposeCitationInsert(**{**base, **over})


def test_propose_artifact_edit_writes_the_stamps_origin_job_id(pg_session_factory):
    """The background-origin case: the stamp carries a real origin_job_id, and
    the written row must track IT — not any field on Provenance, which no
    longer has one."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
    origin_job = uuid.uuid4()

    pc_id = SqlEffects(pg_session_factory).propose_artifact_edit(
        _stamp(ws_id, generation=1, origin_job_id=origin_job), _edit()
    )

    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).origin_job_id == origin_job


def test_propose_citation_insert_writes_the_stamps_origin_job_id(pg_session_factory):
    """Same claim as the artifact-edit case above, for the other propose_*
    effect — both share _insert_proposed_change, but each is its own public
    port method and deserves its own direct assertion."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
    origin_job = uuid.uuid4()

    pc_id = SqlEffects(pg_session_factory).propose_citation_insert(
        _stamp(ws_id, generation=1, origin_job_id=origin_job), _citation()
    )

    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).origin_job_id == origin_job


def test_propose_artifact_edit_leaves_origin_job_id_null_for_interactive_stamps(
    pg_session_factory,
):
    """registry.py leaves origin_job_id None for interactive contexts BY
    DESIGN (tools/registry.py ToolContext.origin_job_id). The fix must not
    accidentally start populating this column from anywhere else."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)

    pc_id = SqlEffects(pg_session_factory).propose_artifact_edit(
        _stamp(ws_id, generation=1), _edit()
    )

    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).origin_job_id is None


def test_propose_citation_insert_leaves_origin_job_id_null_for_interactive_stamps(
    pg_session_factory,
):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)

    pc_id = SqlEffects(pg_session_factory).propose_citation_insert(
        _stamp(ws_id, generation=1), _citation()
    )

    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).origin_job_id is None


def test_a_draft_edit_folds_the_selection_into_the_payload(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)

    pc_id = SqlEffects(pg_session_factory).propose_artifact_edit(
        _stamp(ws_id, generation=1),
        _edit(selection={"anchor": "aims", "from_offset": 10, "to_offset": 20}),
    )

    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).payload == {
            "new_text": "new prose",
            "selection_anchor": "aims",
            "selection_from": 10,
            "selection_to": 20,
        }


def test_a_spec_edit_ignores_the_selection(pg_session_factory):
    """Selection provenance is a draft-editor concept; the spec editor has none."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)

    pc_id = SqlEffects(pg_session_factory).propose_artifact_edit(
        _stamp(ws_id, generation=1),
        _edit(target_artifact="spec", selection={"anchor": "x", "from_offset": 1, "to_offset": 2}),
    )

    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).payload == {"new_text": "new prose"}


def test_propose_artifact_edit_refuses_a_stale_generation(pg_session_factory):
    """The INSERT case, which has no target row and therefore no READ COMMITTED
    re-check to mask a missing fence."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=5)
        bump_generation(s, ws_id, to=7)
        s.commit()

    with pytest.raises(WorkspaceGenerationStale) as e:
        SqlEffects(pg_session_factory).propose_artifact_edit(
            _stamp(ws_id, generation=5), _edit()
        )
    assert (e.value.expected, e.value.actual) == (5, 7)

    with pg_session_factory() as s:
        assert s.query(ProposedChange).count() == 0


def test_propose_artifact_edit_refuses_a_soft_deleted_workspace(pg_session_factory):
    from datetime import UTC, datetime

    from call_manager.db.models import Workspace

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=5)
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    with pytest.raises(WorkspaceGenerationStale):
        SqlEffects(pg_session_factory).propose_artifact_edit(
            _stamp(ws_id, generation=5), _edit()
        )
    with pg_session_factory() as s:
        assert s.query(ProposedChange).count() == 0


def test_propose_citation_insert_writes_the_accept_time_payload(pg_session_factory):
    """ev_id / citekey / offsets are minted authoritatively at ACCEPT time, not
    stored here — the payload mirrors the tool's arguments and nothing more."""
    ref_id = uuid.uuid4()
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=2)

    pc_id = SqlEffects(pg_session_factory).propose_citation_insert(
        _stamp(ws_id, generation=2),
        ProposeCitationInsert(
            section_index=3,
            base_commit_sha="c" * 40,
            base_section_sha="d" * 64,
            reference_id=ref_id,
            anchor="as shown",
            chunk_index=4,
            evidence_quote="the quote",
            stance="supports",
            confidence=None,
            provenance=_PROV,
        ),
    )

    with pg_session_factory() as s:
        pc = s.get(ProposedChange, pc_id)
        assert (pc.kind, pc.target_artifact, pc.status) == (
            "citation_insert",
            "draft",
            "pending",
        )
        assert pc.payload == {
            "reference_id": str(ref_id),
            "anchor": "as shown",
            "chunk_index": 4,
            "evidence_quote": "the quote",
            "stance": "supports",
            "confidence": None,
        }


def test_propose_citation_insert_refuses_a_stale_generation(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=2)
        bump_generation(s, ws_id, to=3)
        s.commit()

    with pytest.raises(WorkspaceGenerationStale):
        SqlEffects(pg_session_factory).propose_citation_insert(
            _stamp(ws_id, generation=2),
            ProposeCitationInsert(
                section_index=0,
                base_commit_sha="c" * 40,
                base_section_sha="d" * 64,
                reference_id=uuid.uuid4(),
                anchor="a",
                chunk_index=0,
                evidence_quote="q",
                stance="supports",
                confidence=None,
                provenance=_PROV,
            ),
        )
    with pg_session_factory() as s:
        assert s.query(ProposedChange).count() == 0
