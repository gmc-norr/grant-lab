"""Row seeding for the F3b port tests, shared by the unit and lock-case suites.

make_workspace is a FUNCTION taking a session (tests/proposal_assist/conftest.py),
not a fixture.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import update

from call_manager.db.models import (
    ChatTurn,
    EvaluatorJob,
    ReviewRun,
    ReviewRunItem,
    TurnJob,
    UncitedClaimsJob,
    Workspace,
)
from tests.proposal_assist.conftest import make_user, make_workspace


def seed_workspace(session, *, generation: int = 0) -> uuid.UUID:
    # make_workspace(session) with no owner falls back to make_user's HARDCODED
    # default email ("owner@ki.se" — its docstring claims per-call uniqueness,
    # the implementation does not deliver it), so two seed_workspace calls in
    # one test/session collide on uq_users_email. A fresh, uniquely-emailed
    # owner per call is what the mine/theirs test pattern actually needs.
    owner = make_user(session, email=f"f3b-seed-{uuid.uuid4()}@ki.se")
    ws = make_workspace(session, owner=owner)
    session.flush()
    session.execute(
        update(Workspace).where(Workspace.id == ws.id).values(lifecycle_generation=generation)
    )
    session.commit()
    return ws.id


def bump_generation(session, workspace_id: uuid.UUID, *, to: int) -> None:
    session.execute(
        update(Workspace).where(Workspace.id == workspace_id).values(lifecycle_generation=to)
    )


def seed_evaluator_job(session, workspace_id: uuid.UUID, *, generation: int) -> uuid.UUID:
    job = EvaluatorJob(
        workspace_id=workspace_id,
        status="running",
        claimed_at_generation=generation,
        started_at=datetime.now(UTC),
    )
    session.add(job)
    session.commit()
    return job.id


def seed_uncited_job(session, workspace_id: uuid.UUID, *, generation: int) -> uuid.UUID:
    job = UncitedClaimsJob(
        workspace_id=workspace_id,
        status="running",
        claimed_at_generation=generation,
        started_at=datetime.now(UTC),
    )
    session.add(job)
    session.commit()
    return job.id


def seed_review_item(
    session, workspace_id: uuid.UUID, *, generation: int
) -> tuple[uuid.UUID, uuid.UUID]:
    """Returns (review_run_id, review_run_item_id). ReviewRun requires draft_sha."""
    run = ReviewRun(
        workspace_id=workspace_id,
        status="running",
        claimed_at_generation=generation,
        draft_sha="0" * 40,
    )
    session.add(run)
    session.flush()
    item = ReviewRunItem(review_run_id=run.id, kind="fit_check", status="running")
    session.add(item)
    session.commit()
    return run.id, item.id


def seed_running_turn(session, workspace_id: uuid.UUID) -> uuid.UUID:
    """A running TurnJob for the accounting tests. TurnJob needs a ChatTurn."""
    ws = session.get(Workspace, workspace_id)
    turn = ChatTurn(workspace_id=workspace_id, role="user", content={"type": "text", "text": "hi"})
    session.add(turn)
    session.flush()
    job = TurnJob(
        workspace_id=workspace_id,
        user_turn_id=turn.id,
        submitted_by_user_id=ws.owner_id,
        status="running",
        queued_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
    )
    session.add(job)
    session.commit()
    return job.id
