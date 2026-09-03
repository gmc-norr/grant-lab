"""Fence F1 — Primitive A (`live_at_generation`) and `WorkspaceGenerationStale`.

Primitive A is a PREDICATE, not a function that runs a query. It is meant to be
dropped into the WHERE of the statement that performs the effect, so the fence
and the write are one statement (spec 3.1).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from call_manager.db.models import Workspace
from call_manager.proposal_assist.fencing import (
    WorkspaceGenerationStale,
    live_at_generation,
)
from tests.proposal_assist.conftest import make_user, make_workspace


def _seed(pg_session_factory) -> uuid.UUID:
    with pg_session_factory() as s:
        owner = make_user(s, email=f"fence-{uuid.uuid4().hex[:8]}@umu.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        return ws.id


def _matches(pg_session_factory, ws_id: uuid.UUID, generation: int) -> bool:
    """Evaluate the predicate exactly as a caller would: inside a statement."""
    with pg_session_factory() as s:
        return bool(s.execute(select(live_at_generation(ws_id, generation))).scalar())


def test_matches_a_live_workspace_at_its_current_generation(pg_session_factory):
    ws_id = _seed(pg_session_factory)
    with pg_session_factory() as s:
        gen = s.execute(
            select(Workspace.lifecycle_generation).where(Workspace.id == ws_id)
        ).scalar_one()
    assert _matches(pg_session_factory, ws_id, gen) is True


def test_does_not_match_a_different_generation(pg_session_factory):
    """The whole point: an actor holding a superseded generation is refused."""
    ws_id = _seed(pg_session_factory)
    with pg_session_factory() as s:
        gen = s.execute(
            select(Workspace.lifecycle_generation).where(Workspace.id == ws_id)
        ).scalar_one()
    assert _matches(pg_session_factory, ws_id, gen + 1) is False


def test_does_not_match_a_generation_below_the_current_one(pg_session_factory):
    """Equality, not `>=`. A LOWER generation is impossible under correct
    operation, so accepting it would wave through the corruption the fence
    exists to detect (Global Constraint 4)."""
    ws_id = _seed(pg_session_factory)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        ws.lifecycle_generation = 5
        s.commit()
    assert _matches(pg_session_factory, ws_id, 4) is False
    assert _matches(pg_session_factory, ws_id, 5) is True


def test_does_not_match_a_deleted_workspace_even_at_the_same_generation(
    pg_session_factory,
):
    """THE ANTI-MERGE PIN (Global Constraint 2).

    `jobs_retirement._still_at_generation` deliberately OMITS the liveness
    clause, because it runs during the delete fan-out when `soft_deleted_at` is
    ALREADY stamped — it must match a DELETED workspace. This predicate must
    NOT. The two look like duplicates and are opposites; adding liveness to the
    retirement one would make every cancel a no-op and silently disarm the
    retirement fan-out.

    Generation is held EQUAL here on purpose, so the only thing this test can be
    measuring is the liveness clause.
    """
    ws_id = _seed(pg_session_factory)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        gen = ws.lifecycle_generation
        ws.soft_deleted_at = datetime.now(UTC)
        s.commit()
    assert _matches(pg_session_factory, ws_id, gen) is False


def test_does_not_match_a_missing_workspace(pg_session_factory):
    """A workspace that is not there is never fenced-through."""
    assert _matches(pg_session_factory, uuid.uuid4(), 0) is False


def test_generation_zero_is_a_real_generation(pg_session_factory):
    """`lifecycle_generation` has server_default 0, so a fresh workspace IS at
    generation 0. A fence that treated 0 as 'unset' would pass everything
    (Global Constraint 5)."""
    ws_id = _seed(pg_session_factory)
    with pg_session_factory() as s:
        assert (
            s.execute(
                select(Workspace.lifecycle_generation).where(Workspace.id == ws_id)
            ).scalar_one()
            == 0
        ), "seed assumption broken: a fresh workspace is no longer at generation 0"
    assert _matches(pg_session_factory, ws_id, 0) is True


def test_generation_stale_carries_both_generations():
    """The exception must say what was expected AND what was found — a bare
    'stale' gives an operator nothing to reason about."""
    ws_id = uuid.uuid4()
    exc = WorkspaceGenerationStale(ws_id, expected=3, actual=7)
    assert exc.workspace_id == ws_id
    assert exc.expected == 3
    assert exc.actual == 7
    assert "3" in str(exc) and "7" in str(exc)


def test_generation_stale_is_not_a_workspace_deleted():
    """Distinct types: a caller must be able to tell 'your epoch ended' from
    'the workspace is gone' — they converge differently (spec 7)."""
    from call_manager.proposal_assist.drafting.locking import WorkspaceDeleted

    exc = WorkspaceGenerationStale(uuid.uuid4(), expected=1, actual=2)
    assert not isinstance(exc, WorkspaceDeleted)
    with pytest.raises(WorkspaceGenerationStale):
        raise exc


def test_the_retirement_predicate_and_the_fence_predicate_are_opposites(
    pg_session_factory,
):
    """THE ANTI-MERGE PIN, behavioural (Global Constraint 2).

    On ONE deleted workspace at generation G, held at the SAME generation so
    liveness is the only variable:

        live_at_generation(ws, G)     -> False   (fence: refuse deleted)
        _still_at_generation(ws, G)   -> True    (retirement: act on deleted)

    They are not duplicates. They are opposites. `_still_at_generation` runs
    during the delete fan-out, when soft_deleted_at is ALREADY stamped, so
    giving it a liveness clause — or rewriting its body as
    `return live_at_generation(...)` — makes every cancel a no-op and disarms
    the retirement fan-out.

    This test is BEHAVIOURAL on purpose. A structural check that greps the
    function body for "soft_deleted_at" is defeated by exactly the refactor it
    is meant to stop, because `return live_at_generation(...)` contains no such
    text. It also lives HERE rather than in test_fencing_inertness.py, because
    F2 deletes that file and this invariant is permanent.

    Importing a private helper is deliberate: this test exists to pin that
    private's contract against the public one.
    """
    from call_manager.proposal_assist.jobs_retirement import _still_at_generation

    ws_id = _seed(pg_session_factory)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        gen = ws.lifecycle_generation
        ws.soft_deleted_at = datetime.now(UTC)
        s.commit()

    with pg_session_factory() as s:
        fence = bool(s.execute(select(live_at_generation(ws_id, gen))).scalar())
        retire = bool(s.execute(select(_still_at_generation(ws_id, gen))).scalar())

    assert fence is False, "the fence predicate must REFUSE a deleted workspace"
    assert retire is True, (
        "the retirement predicate must MATCH a deleted workspace — it runs "
        "during the delete fan-out. If this is False, someone gave it a "
        "liveness clause and every cancel is now a no-op."
    )
