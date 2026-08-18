"""F3a — every tool-originating actor records the generation it claimed at."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from call_manager.db.models import (
    ChatTurn,
    CoherenceJob,
    EvaluatorJob,
    ReviewRun,
    TurnJob,
    UncitedClaimsJob,
    Workspace,
)
from call_manager.proposal_assist.coherence.jobs import claim_next_coherence_job
from call_manager.proposal_assist.evaluator.jobs import claim_next_evaluator_job
from call_manager.proposal_assist.review.jobs import claim_next_review_run
from call_manager.proposal_assist.uncited_claims.jobs import claim_next_uncited_claims_job
from tests.proposal_assist.conftest import make_workspace


def _seed_queued_turn(session, *, generation: int):
    """make_workspace is a FUNCTION taking a session (tests/proposal_assist/
    conftest.py), not a fixture. TurnJob needs a ChatTurn — see the canonical
    seed at tests/test_workspaces_service.py:33-54."""
    ws = make_workspace(session)
    session.flush()
    session.execute(
        update(Workspace).where(Workspace.id == ws.id).values(lifecycle_generation=generation)
    )
    turn = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
    session.add(turn)
    session.flush()
    job = TurnJob(
        workspace_id=ws.id,
        user_turn_id=turn.id,
        submitted_by_user_id=ws.owner_id,
        status="queued",
        queued_at=datetime.now(UTC),
    )
    session.add(job)
    session.commit()
    return job.id


def test_the_interactive_claim_stores_the_workspaces_current_generation(pg_session_factory):
    """The claim is the ONLY safe capture point. A read after it lets a
    delete/restore land in between, and the actor then adopts a generation it
    never claimed at."""
    with pg_session_factory() as s:
        job_id = _seed_queued_turn(s, generation=7)

    from call_manager.proposal_assist.turns.processor import claim_turn_job

    claimed = claim_turn_job(pg_session_factory, job_id)

    assert claimed is not None
    assert claimed.claimed_at_generation == 7
    with pg_session_factory() as s:
        stored = s.scalar(select(TurnJob.claimed_at_generation).where(TurnJob.id == job_id))
    assert stored == 7, "the generation must be persisted by the claim, not just returned"


def test_a_second_claim_of_the_same_turn_job_returns_none(pg_session_factory):
    """The claim's status guard is unchanged by F3a. Without this, adding a
    column to .values() could quietly widen what the statement matches."""
    with pg_session_factory() as s:
        job_id = _seed_queued_turn(s, generation=0)

    from call_manager.proposal_assist.turns.processor import claim_turn_job

    assert claim_turn_job(pg_session_factory, job_id) is not None
    assert claim_turn_job(pg_session_factory, job_id) is None


def test_the_claim_writes_the_generation_in_ONE_statement(pg_session_factory):
    """Global Constraint 2, made testable. A separate SELECT takes a FRESH
    snapshot under READ COMMITTED and can therefore see a NEWER generation —
    the actor then adopts an epoch it never claimed at and passes its fence."""
    from sqlalchemy import event

    statements: list[str] = []

    with pg_session_factory() as s:
        job_id = _seed_queued_turn(s, generation=2)

    from call_manager.proposal_assist.turns.processor import claim_turn_job

    engine = pg_session_factory.kw["bind"]

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, params, context, executemany):
        statements.append(" ".join(statement.split()).upper())

    try:
        claim_turn_job(pg_session_factory, job_id)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    writes = [q for q in statements if "CLAIMED_AT_GENERATION" in q]
    assert len(writes) == 1, f"expected one claim statement, got {len(writes)}"
    assert writes[0].startswith("UPDATE TURN_JOBS"), writes[0]
    assert "RETURNING" in writes[0]


#: ReviewRun.draft_sha is NOT NULL with no server_default — the other three need
#: only workspace_id and status. Constructors verified against the canonical seed
#: at tests/test_workspaces_service.py:37-42.
_BACKGROUND_CLAIMS = [
    (
        "coherence",
        lambda ws_id: CoherenceJob(workspace_id=ws_id, status="queued"),
        claim_next_coherence_job,
    ),
    (
        "evaluator",
        lambda ws_id: EvaluatorJob(workspace_id=ws_id, status="queued"),
        claim_next_evaluator_job,
    ),
    (
        "review",
        lambda ws_id: ReviewRun(workspace_id=ws_id, status="queued", draft_sha="a" * 64),
        claim_next_review_run,
    ),
    (
        "uncited",
        lambda ws_id: UncitedClaimsJob(workspace_id=ws_id, status="queued"),
        claim_next_uncited_claims_job,
    ),
]


@pytest.mark.parametrize(
    "name,make_job,claim_fn", _BACKGROUND_CLAIMS, ids=[c[0] for c in _BACKGROUND_CLAIMS]
)
def test_every_background_claim_captures_the_generation(
    pg_session_factory, name, make_job, claim_fn
):
    """All four build a ToolContext and therefore originate tool calls. A
    context without a generation cannot be fenced by F3b, so the claim is where
    the value has to come from."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.execute(
            update(Workspace).where(Workspace.id == ws.id).values(lifecycle_generation=4)
        )
        s.add(make_job(ws.id))
        s.commit()

    with pg_session_factory() as s:
        job = claim_fn(s)
        assert job is not None
        assert job.claimed_at_generation == 4
        s.commit()
