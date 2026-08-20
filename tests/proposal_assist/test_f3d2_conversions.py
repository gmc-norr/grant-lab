"""F3d2 — the submitters and the reads. Each converted site against its port, and
each with the compensation disabled.

Spec 2026-08-18 revision 8 §5: "a conversion test that passes with the fence removed
is measuring the compensation, not the fence".
"""

from __future__ import annotations

import uuid

import pytest

from call_manager.proposal_assist.tools.registry import ToolContext, snapshot_invocation
from tests.proposal_assist.f3b_seeds import (
    bump_generation,
    seed_evaluator_job,
    seed_review_item,
    seed_running_turn,
    seed_uncited_job,
    seed_workspace,
)

_G = 4  # the generation every actor in this file claims at
_NEXT = 5  # what a lifecycle transition moves it to


def _ctx_and_invocation(workspace_id, **extra):
    """A ToolContext at generation _G plus its frozen Invocation.

    ports=None deliberately: a converted tool receives its bundle as a KEYWORD from
    _safe_tool, not through the context, and these tests call the impls directly.
    """
    ctx = ToolContext(
        workspace_id=workspace_id,
        repo_dir=None,
        session_factory=None,
        settings=None,
        lifecycle_generation=_G,
        **extra,
    )
    return ctx, snapshot_invocation(ctx)


class _ExplodingPorts:
    """Fails the test if the tool reaches for a capability at all. Used wherever a
    tool must refuse a bad payload BEFORE touching a port."""

    def __getattr__(self, name):
        raise AssertionError(f"the port was touched: {name}")


def _criterion(index, name="A"):
    return {"index": index, "name": name, "band": "adequate", "note": "n"}


def test_the_band_rollup_is_reachable_from_ports():
    """A converted submit_scorecard_impl still has to compute the overall band —
    Effects.submit_scorecard returns None and the tool reports the band back to the
    agent — and it may import only stdlib and ports."""
    from call_manager.proposal_assist.ports.scoring import compute_overall_band

    assert callable(compute_overall_band)


def test_the_old_scoring_module_re_exports_the_same_function():
    """GC-4 of F3d1, still binding: one source of truth, and the moved object stays
    the SAME object so no caller can drift onto a second copy."""
    from call_manager.proposal_assist.evaluator import scoring as old
    from call_manager.proposal_assist.ports import scoring as new

    assert old.compute_overall_band is new.compute_overall_band


@pytest.mark.asyncio
async def test_scorecard_writes_through_the_port(pg_session_factory):
    """The conversion itself: the tool no longer opens a session, and the row lands."""
    from call_manager.db.models import EvaluatorJob
    from call_manager.proposal_assist.port_impl import build_ports
    from call_manager.proposal_assist.tools.scorecard import submit_scorecard_impl

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)
        job_id = seed_evaluator_job(s, ws_id, generation=_G)

    ctx, invocation = _ctx_and_invocation(
        ws_id, origin_job_id=job_id, eval_rubric_criteria=[{"name": "A"}]
    )
    out = await submit_scorecard_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=invocation,
        criteria=[_criterion(0)],
        summary="ok",
    )

    assert out["stored"] is True
    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, job_id).scorecard is not None


@pytest.mark.asyncio
async def test_scorecard_does_not_land_after_the_generation_moves(pg_session_factory):
    """GC-4 — THE COMPENSATION IS DISABLED. The tool's own guard (job is this
    workspace's still-running evaluator run) is left SATISFIED: the job stays
    running and owned. Only the generation moves. If the write still lands, the
    fence is decorative and this test is the only thing that would have said so."""
    from call_manager.db.models import EvaluatorJob
    from call_manager.proposal_assist.port_impl import build_ports
    from call_manager.proposal_assist.tools.scorecard import submit_scorecard_impl

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)
        job_id = seed_evaluator_job(s, ws_id, generation=_G)

    # Stamp taken at _G — the actor's live claim.
    ctx, invocation = _ctx_and_invocation(
        ws_id, origin_job_id=job_id, eval_rubric_criteria=[{"name": "A"}]
    )
    # The workspace moves on. The job is untouched and still perfectly claimable.
    with pg_session_factory() as s:
        bump_generation(s, ws_id, to=_NEXT)
        s.commit()

    out = await submit_scorecard_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=invocation,
        criteria=[_criterion(0)],
        summary="ok",
    )

    assert "error" in out
    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, job_id).scorecard is None


@pytest.mark.asyncio
async def test_scorecard_refusal_messages_are_unchanged(pg_session_factory):
    """GC-6. The port reproduces the tool's strings verbatim and in the same order:
    a stale ctx pointing at a finished run must still get the TARGET message, not
    'already submitted'."""
    from call_manager.db.models import EvaluatorJob
    from call_manager.proposal_assist.port_impl import build_ports
    from call_manager.proposal_assist.tools.scorecard import submit_scorecard_impl

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)
        job_id = seed_evaluator_job(s, ws_id, generation=_G)

    ctx, invocation = _ctx_and_invocation(
        ws_id, origin_job_id=job_id, eval_rubric_criteria=[{"name": "A"}]
    )
    with pg_session_factory() as s:
        s.get(EvaluatorJob, job_id).status = "completed"
        s.commit()

    out = await submit_scorecard_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=invocation,
        criteria=[_criterion(0)],
        summary="ok",
    )
    assert out["error"] == (
        "scorecard target is not an active evaluator run for this workspace"
    )


@pytest.mark.asyncio
async def test_scorecard_validation_still_runs_before_any_port_call():
    """The validation half is pure and unchanged. A bad payload must be refused
    without the port being touched at all — a converted tool that reaches for a
    capability before validating has widened its blast radius. No database here."""
    from call_manager.proposal_assist.tools.scorecard import submit_scorecard_impl

    ctx, invocation = _ctx_and_invocation(
        uuid.uuid4(), origin_job_id=uuid.uuid4(), eval_rubric_criteria=[{"name": "A"}]
    )
    out = await submit_scorecard_impl(
        ctx,
        ports=_ExplodingPorts(),
        invocation=invocation,
        criteria=[],  # covers zero of the one required index
        summary="ok",
    )
    assert "must cover criterion indexes" in out["error"]


@pytest.mark.asyncio
async def test_review_writes_through_the_port(pg_session_factory):
    from call_manager.db.models import ReviewRunItem
    from call_manager.proposal_assist.port_impl import build_ports
    from call_manager.proposal_assist.tools.review_submit import submit_review_impl

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)
        run_id, item_id = seed_review_item(s, ws_id, generation=_G)

    ctx, invocation = _ctx_and_invocation(
        ws_id, origin_job_id=run_id, review_run_item_id=item_id
    )
    out = await submit_review_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=invocation,
        markdown="  a body  ",
    )

    assert out["stored"] is True
    with pg_session_factory() as s:
        stored = s.get(ReviewRunItem, item_id).raw_response_json
    # STRIPPED by the tool, not the port: ports/effects.py:32-36 says the port
    # stores verbatim and the tool owns the strip-and-reject.
    assert stored == {"markdown": "a body"}


@pytest.mark.asyncio
async def test_review_still_rejects_an_empty_body_without_touching_the_port():
    """The strip-and-reject step lives in the tool. A conversion that hands an
    empty string to the port loses it silently — the port stores verbatim."""
    from call_manager.proposal_assist.tools.review_submit import submit_review_impl

    ctx, invocation = _ctx_and_invocation(
        uuid.uuid4(), origin_job_id=uuid.uuid4(), review_run_item_id=uuid.uuid4()
    )
    out = await submit_review_impl(
        ctx, ports=_ExplodingPorts(), invocation=invocation, markdown="   "
    )
    assert out["error"] == "markdown must be a non-empty string"


@pytest.mark.asyncio
async def test_review_does_not_land_after_the_generation_moves(pg_session_factory):
    """GC-4 — THE COMPENSATION IS DISABLED. The item stays running and owned by a
    running run in this workspace, so every guard the tool used to apply is
    SATISFIED. Only the generation moves."""
    from call_manager.db.models import ReviewRunItem
    from call_manager.proposal_assist.port_impl import build_ports
    from call_manager.proposal_assist.tools.review_submit import submit_review_impl

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)
        run_id, item_id = seed_review_item(s, ws_id, generation=_G)

    ctx, invocation = _ctx_and_invocation(
        ws_id, origin_job_id=run_id, review_run_item_id=item_id
    )
    with pg_session_factory() as s:
        bump_generation(s, ws_id, to=_NEXT)
        s.commit()

    out = await submit_review_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=invocation,
        markdown="a body",
    )

    assert "error" in out
    with pg_session_factory() as s:
        assert s.get(ReviewRunItem, item_id).raw_response_json is None


@pytest.mark.asyncio
async def test_uncited_claims_writes_through_the_port(pg_session_factory):
    from call_manager.db.models import UncitedClaimsJob
    from call_manager.proposal_assist.port_impl import build_ports
    from call_manager.proposal_assist.tools.uncited_claims import (
        submit_uncited_claims_impl,
    )

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)
        job_id = seed_uncited_job(s, ws_id, generation=_G)

    ctx, invocation = _ctx_and_invocation(
        ws_id, origin_job_id=job_id, uncited_draft_section_count=2
    )
    out = await submit_uncited_claims_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=invocation,
        claims=[{"claim_text": "c", "section_index": 0, "reason": "r"}],
    )

    assert out == {"stored": True, "count": 1}
    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).claims is not None


@pytest.mark.asyncio
async def test_uncited_claims_does_not_land_after_the_generation_moves(
    pg_session_factory,
):
    """GC-4 — THE COMPENSATION IS DISABLED. The job stays running and owned; only
    the generation moves."""
    from call_manager.db.models import UncitedClaimsJob
    from call_manager.proposal_assist.port_impl import build_ports
    from call_manager.proposal_assist.tools.uncited_claims import (
        submit_uncited_claims_impl,
    )

    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)
        job_id = seed_uncited_job(s, ws_id, generation=_G)

    ctx, invocation = _ctx_and_invocation(
        ws_id, origin_job_id=job_id, uncited_draft_section_count=2
    )
    with pg_session_factory() as s:
        bump_generation(s, ws_id, to=_NEXT)
        s.commit()

    out = await submit_uncited_claims_impl(
        ctx,
        ports=build_ports(pg_session_factory),
        invocation=invocation,
        claims=[{"claim_text": "c", "section_index": 0, "reason": "r"}],
    )

    assert "error" in out
    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).claims is None


@pytest.mark.asyncio
async def test_uncited_claims_validation_still_runs_before_any_port_call():
    """The section-index bound comes from ctx.uncited_draft_section_count, which the
    converted tool still reads. A bad index must be refused without the port."""
    from call_manager.proposal_assist.tools.uncited_claims import (
        submit_uncited_claims_impl,
    )

    ctx, invocation = _ctx_and_invocation(
        uuid.uuid4(), origin_job_id=uuid.uuid4(), uncited_draft_section_count=1
    )
    out = await submit_uncited_claims_impl(
        ctx,
        ports=_ExplodingPorts(),
        invocation=invocation,
        claims=[{"claim_text": "c", "section_index": 5, "reason": "r"}],
    )
    assert "section_index must be an int in [0,1)" in out["error"]


@pytest.mark.asyncio
async def test_the_refund_does_not_touch_a_later_turns_row(pg_session_factory):
    """GC-5, the interleaving accounting exists for (spec §5): reserve a slot at
    generation G, delete and restore, start a NEW turn at G+1, and assert the old
    actor's refund does not touch the new turn's row.

    TWO schema facts shape this and neither is optional. `uq_turn_jobs_running`
    (models.py:887-891) is a partial unique index on workspace_id WHERE
    status='running', so the old turn MUST leave 'running' before the new one can
    exist — the delete/restore is what does that. And refund_fetch_slot requires
    `TurnJob.status == "running"` (port_impl/accounting.py:38-45), so the dead
    actor's refund is a no-op on its own row too.

    The claim under test is therefore about the NEW turn: a late refund from an
    actor the workspace has moved past must not decrement the live turn's count.
    That is what binding to active_turn_job_id buys, and what the code this
    replaces (references.py:465-473, matching workspace + status) did NOT.
    """
    from call_manager.db.models import TurnJob
    from call_manager.proposal_assist.port_impl import build_ports

    ports = build_ports(pg_session_factory)
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)
        old_turn = seed_running_turn(s, ws_id)

    assert ports.accounting.reserve_fetch_slot(old_turn) == 1

    with pg_session_factory() as s:  # delete + restore ends the old turn
        s.get(TurnJob, old_turn).status = "failed"
        bump_generation(s, ws_id, to=_NEXT)
        s.commit()
        new_turn = seed_running_turn(s, ws_id)  # only now is this insertable

    assert ports.accounting.reserve_fetch_slot(new_turn) == 1

    # The old actor, still holding its slot, finally gives it back.
    ports.accounting.refund_fetch_slot(old_turn)

    with pg_session_factory() as s:
        # THE CLAIM: the live turn is untouched.
        assert s.get(TurnJob, new_turn).fetch_count == 1
        # The dead turn keeps its count: the status guard refuses to refund a turn
        # that is no longer running, and the row is terminal anyway.
        assert s.get(TurnJob, old_turn).fetch_count == 1


@pytest.mark.asyncio
async def test_the_reserve_is_bound_to_the_turn_not_the_workspace(pg_session_factory):
    """The tightening the port makes over the code it replaces. A reserve against a
    turn id that is not running returns None even though ANOTHER turn in the same
    workspace is running and under cap."""
    from call_manager.db.models import TurnJob
    from call_manager.proposal_assist.port_impl import build_ports

    ports = build_ports(pg_session_factory)
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=_G)
        dead_turn = seed_running_turn(s, ws_id)
        s.get(TurnJob, dead_turn).status = "completed"
        s.commit()
        seed_running_turn(s, ws_id)  # a live turn, under cap

    assert ports.accounting.reserve_fetch_slot(dead_turn) is None


async def test_the_cap_constant_is_the_one_the_tool_reports():
    """The tool tells the agent 'fetch cap reached (3 per turn)'. That 3 was a
    literal at references.py:469 while the port carried its own constant — two
    numbers that must agree and nothing making them. No database here."""
    from call_manager.proposal_assist.ports.accounting import MAX_FETCHES_PER_TURN

    assert MAX_FETCHES_PER_TURN == 3
