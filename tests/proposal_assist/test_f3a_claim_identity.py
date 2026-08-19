"""F3a — every tool-originating actor records the generation it claimed at."""

from __future__ import annotations

import uuid
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


def test_a_one_shot_context_carries_the_generation_at_construction(tmp_path, monkeypatch):
    """One-shot contexts KNOW their generation when they are built, so they must
    carry it then. Only the retained interactive context may omit it, because
    launch precedes the claim."""
    from call_manager.proposal_assist.evaluator import agent as evaluator_agent

    captured: dict = {}

    # tools_factory returns build_proposal_assist_mcp_server(ctx) — an MCP
    # server config, NOT the ToolContext. Capture the factory's INPUT.
    import call_manager.proposal_assist.evaluator.agent as agent_mod

    def _capture(ctx):
        captured["ctx"] = ctx
        return object()

    monkeypatch.setattr(agent_mod, "build_proposal_assist_mcp_server", _capture)

    class _FakeSDK:
        def __init__(self, *, tools_factory=None, **kwargs):
            tools_factory(tmp_path, uuid.uuid4())

    evaluator_agent.build_evaluator_adapter(
        settings=None,
        session_factory=lambda: None,
        workspace_id=uuid.uuid4(),
        requested_by=1,
        job_id=uuid.uuid4(),
        draft_section_count=0,
        spec_section_count=0,
        rubric_criteria=[],
        lifecycle_generation=11,
        _sdk_factory=_FakeSDK,
    )
    assert captured["ctx"].lifecycle_generation == 11


def test_a_one_shot_builder_REQUIRES_the_generation():
    """A default would let a caller silently build a context with no belief,
    which is the failure F3b cannot detect and the fence cannot survive."""
    import inspect

    from call_manager.proposal_assist.evaluator.agent import build_evaluator_adapter

    param = inspect.signature(build_evaluator_adapter).parameters["lifecycle_generation"]
    assert param.default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# The deterministic interleaving regression. This is the check that actually
# proves Global Constraint 2, and it covers all FIVE claims.
#
# The value-only tests above pass against the REJECTED implementation (claim the
# job, then read the generation in a second statement) because a single-threaded
# test never lets a transition land in the gap. The statement-shape test above is
# also weaker than its name: an unsafe claim could UPDATE status first, SELECT the
# generation second, then issue a SECOND `UPDATE ... claimed_at_generation ...
# RETURNING` — only that one statement mentions the column, so it passes.
#
# This test creates the gap on purpose. A second connection advances the
# workspace to generation 99 the moment the claim's locking statement returns.
# A one-statement claim already holds its value and stores the original; a
# claim-then-read implementation reads 99 and stores an epoch it never claimed at.
# ---------------------------------------------------------------------------

_ADVANCED_TO = 99


def _advance_generation_on_another_connection(engine, workspace_id) -> None:
    """Runs on its OWN connection, so it is a genuinely concurrent transition.
    It does not block: the claim holds FOR UPDATE OF the JOB table only, and this
    updates `workspaces` — which is exactly why the job row's lock cannot protect
    the generation."""
    with engine.begin() as conn:
        conn.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(lifecycle_generation=_ADVANCED_TO)
        )


def test_the_interactive_claim_is_immune_to_a_generation_advance_mid_claim(
    pg_session_factory,
):
    from sqlalchemy import event

    from call_manager.proposal_assist.turns.processor import claim_turn_job

    with pg_session_factory() as s:
        job_id = _seed_queued_turn(s, generation=5)
        ws_id = s.scalar(select(TurnJob.workspace_id).where(TurnJob.id == job_id))

    engine = pg_session_factory.kw["bind"]
    fired: list[bool] = []

    @event.listens_for(engine, "after_cursor_execute")
    def _advance(conn, cursor, statement, params, context, executemany):
        if fired or "turn_jobs" not in statement.lower():
            return
        fired.append(True)
        _advance_generation_on_another_connection(engine, ws_id)

    try:
        claimed = claim_turn_job(pg_session_factory, job_id)
    finally:
        event.remove(engine, "after_cursor_execute", _advance)

    assert fired, "the probe never fired — it is not testing the interleaving"
    assert claimed is not None
    with pg_session_factory() as s:
        stored = s.scalar(select(TurnJob.claimed_at_generation).where(TurnJob.id == job_id))
        live = s.scalar(select(Workspace.lifecycle_generation).where(Workspace.id == ws_id))
    assert live == _ADVANCED_TO, "the probe did not actually advance the workspace"
    assert stored == 5, (
        f"the claim stored generation {stored} — it read the workspace AFTER the "
        "transition and adopted an epoch it never claimed at"
    )


@pytest.mark.parametrize(
    "name,make_job,claim_fn", _BACKGROUND_CLAIMS, ids=[c[0] for c in _BACKGROUND_CLAIMS]
)
def test_every_background_claim_is_immune_to_a_generation_advance_mid_claim(
    pg_session_factory, name, make_job, claim_fn
):
    from sqlalchemy import event

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.execute(
            update(Workspace).where(Workspace.id == ws.id).values(lifecycle_generation=6)
        )
        s.add(make_job(ws.id))
        s.commit()
        ws_id = ws.id

    engine = pg_session_factory.kw["bind"]
    fired: list[bool] = []

    @event.listens_for(engine, "after_cursor_execute")
    def _advance(conn, cursor, statement, params, context, executemany):
        # Fire the instant the locking claim SELECT returns — the exact window a
        # second statement would read in.
        if fired or "FOR UPDATE OF" not in statement.upper():
            return
        fired.append(True)
        _advance_generation_on_another_connection(engine, ws_id)

    try:
        with pg_session_factory() as s:
            job = claim_fn(s)
            assert job is not None
            s.commit()
            job_id = job.id
    finally:
        event.remove(engine, "after_cursor_execute", _advance)

    assert fired, "the probe never fired — no FOR UPDATE OF statement was seen"
    with pg_session_factory() as s:
        stored = s.scalar(
            select(type(job).claimed_at_generation).where(type(job).id == job_id)
        )
        live = s.scalar(select(Workspace.lifecycle_generation).where(Workspace.id == ws_id))
    assert live == _ADVANCED_TO, "the probe did not actually advance the workspace"
    assert stored == 6, (
        f"{name} stored generation {stored} — it read the workspace AFTER the "
        "transition and adopted an epoch it never claimed at"
    )
