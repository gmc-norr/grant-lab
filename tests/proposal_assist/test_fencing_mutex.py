"""Fence F1 — Primitive B (`acquire_commit_mutex_at_generation`).

The interleaving test is the substance. The three sequential tests all pass
against an implementation that reads the generation off the pre-mutex instance;
only the contended one exposes it. That exact defect shipped in Slice C1 and was
fixed in 701fbb7.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, update

from call_manager.db.models import Workspace
from call_manager.proposal_assist.drafting.locking import (
    WorkspaceDeleted,
    acquire_commit_mutex,
)
from call_manager.proposal_assist.fencing import (
    WorkspaceGenerationStale,
    acquire_commit_mutex_at_generation,
)
from tests.proposal_assist.conftest import make_user, make_workspace


def _seed(pg_session_factory) -> tuple[uuid.UUID, int]:
    with pg_session_factory() as s:
        owner = make_user(s, email=f"fenceb-{uuid.uuid4().hex[:8]}@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        return ws.id, ws.lifecycle_generation


def test_acquires_and_returns_when_the_generation_matches(pg_session_factory):
    ws_id, gen = _seed(pg_session_factory)
    with pg_session_factory() as s:
        acquire_commit_mutex_at_generation(s, ws_id, generation=gen)
        s.rollback()


def test_raises_stale_when_the_generation_moved_on(pg_session_factory):
    ws_id, gen = _seed(pg_session_factory)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        ws.lifecycle_generation = gen + 3
        s.commit()

    with pg_session_factory() as s:
        with pytest.raises(WorkspaceGenerationStale) as ei:
            acquire_commit_mutex_at_generation(s, ws_id, generation=gen)
        s.rollback()
    assert ei.value.expected == gen
    assert ei.value.actual == gen + 3


def test_raises_stale_when_the_carried_generation_is_higher(pg_session_factory):
    """Equality, not `>`. A carried generation ABOVE the current one cannot
    happen under correct operation, so it is corruption, not a pass
    (Global Constraint 4)."""
    ws_id, gen = _seed(pg_session_factory)
    with pg_session_factory() as s:
        with pytest.raises(WorkspaceGenerationStale):
            acquire_commit_mutex_at_generation(s, ws_id, generation=gen + 1)
        s.rollback()


def test_raises_deleted_not_stale_for_a_deleted_workspace(pg_session_factory):
    """Deleted takes PRECEDENCE over stale, and the test must model reality to
    prove it.

    A real deletion (`soft_delete_workspace_db`) both stamps `soft_deleted_at`
    AND bumps the generation, so after one, BOTH conditions hold at once. This
    test therefore sets both. Setting only `soft_deleted_at` — leaving the
    generation equal — would make the test unfalsifiable: an implementation that
    checked generation first would still pass it, then raise
    WorkspaceGenerationStale after every real deletion and send callers down the
    quiet-abandon path instead of the 404 path.
    """
    ws_id, gen = _seed(pg_session_factory)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        ws.soft_deleted_at = datetime.now(UTC)
        ws.lifecycle_generation = gen + 1  # what a real delete does
        s.commit()

    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted):
            acquire_commit_mutex_at_generation(s, ws_id, generation=gen)
        s.rollback()


def test_raises_deleted_for_a_missing_workspace(pg_session_factory):
    """A missing workspace is never writable — same treatment as
    acquire_commit_mutex_live gives it."""
    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted):
            acquire_commit_mutex_at_generation(s, uuid.uuid4(), generation=0)
        s.rollback()


def test_the_generation_is_read_UNDER_the_mutex_not_before_it(
    pg_session_factory, migrated_engine
):
    """THE INTERLEAVING TEST — the only one that bites.

    The caller loads the workspace BEFORE blocking on the mutex (the normal
    shape: you look something up, then you go to write it). While it is blocked,
    another transaction bumps the generation and commits. The re-read must
    therefore happen UNDER the mutex and see the new value.

    The seam fires on `pg_advisory_xact_lock` — after the caller's pre-mutex
    load, before it holds the mutex. That is the only window in which the
    concurrent transition can land. The caller holds no locks at that instant,
    so the competing session takes the same mutex, commits and releases it
    inside the callback without deadlocking.

    The competing session takes the mutex FOR REAL, because that is what a real
    lifecycle transition does — `bump_lifecycle_generation` requires its caller
    to hold it, which is what makes the mutex the total order over generations.
    A plain UPDATE here would still exercise fresh-read ordering, but it would
    NOT exercise the serialization the docstring claims, and the two are
    different properties.

    Same seam and same shape as
    tests/test_workspace_restore_acceptance.py's test 11, which caught the
    identical pre-mutex-read defect in restore_workspace_db (701fbb7).
    """
    ws_id, gen = _seed(pg_session_factory)
    state = {"fired": False}

    def _bump_from_another_connection():
        # Mutex-held, exactly as a real transition is. The latch below keeps
        # this session's own pg_advisory_xact_lock from re-entering the seam.
        with pg_session_factory() as other:
            acquire_commit_mutex(other, ws_id)
            other.execute(
                update(Workspace)
                .where(Workspace.id == ws_id)
                .values(lifecycle_generation=Workspace.lifecycle_generation + 1)
            )
            other.commit()

    def _before(conn, cursor, statement, parameters, context, executemany):
        # The latch keeps the bump's own statements from recursing.
        if state["fired"] or "pg_advisory_xact_lock" not in statement:
            return
        state["fired"] = True
        _bump_from_another_connection()

    event.listen(migrated_engine, "before_cursor_execute", _before)
    try:
        with pg_session_factory() as s:
            # Pre-mutex load: this instance caches lifecycle_generation == gen.
            ws = s.get(Workspace, ws_id)
            assert ws.lifecycle_generation == gen
            with pytest.raises(WorkspaceGenerationStale) as ei:
                acquire_commit_mutex_at_generation(s, ws_id, generation=gen)
            s.rollback()
    finally:
        event.remove(migrated_engine, "before_cursor_execute", _before)

    assert state["fired"], "the trigger never matched — this test proved nothing"
    assert ei.value.actual == gen + 1, (
        f"the fence read generation {ei.value.actual} — the value cached before "
        f"the mutex — instead of the committed {gen + 1}"
    )


def test_a_deleted_workspace_observed_only_under_the_mutex_still_raises(
    pg_session_factory, migrated_engine
):
    """Same seam, deletion instead of a bump: the liveness half of the re-read
    must also happen under the mutex, not off the pre-mutex instance.

    The competing session takes the mutex, symmetrically with the bump test
    above, because that is what a real deletion does —
    `soft_delete_workspace_db` holds `acquire_commit_mutex_live` across the
    whole transaction, and that is what makes the flip a linearization point
    rather than a hope. A plain UPDATE here would still exercise fresh-read
    ordering but not the serialization, and those are different properties.

    Taking the mutex here does NOT deadlock, and any comment claiming otherwise
    is wrong: the seam fires BEFORE the caller's own `pg_advisory_xact_lock`, so
    the caller holds no lock at that instant and this session acquires, commits
    and releases inside the callback. Measured: the file passes in ~4s.
    """
    ws_id, gen = _seed(pg_session_factory)
    state = {"fired": False}

    def _delete_from_another_connection():
        # Mutex-held, exactly as soft_delete_workspace_db is. The latch below
        # keeps this session's own pg_advisory_xact_lock out of the seam.
        with pg_session_factory() as other:
            acquire_commit_mutex(other, ws_id)
            ws = other.get(Workspace, ws_id)
            ws.soft_deleted_at = datetime.now(UTC)
            other.commit()

    def _before(conn, cursor, statement, parameters, context, executemany):
        if state["fired"] or "pg_advisory_xact_lock" not in statement:
            return
        state["fired"] = True
        _delete_from_another_connection()

    event.listen(migrated_engine, "before_cursor_execute", _before)
    try:
        with pg_session_factory() as s:
            ws = s.get(Workspace, ws_id)
            assert ws.soft_deleted_at is None
            with pytest.raises(WorkspaceDeleted):
                acquire_commit_mutex_at_generation(s, ws_id, generation=gen)
            s.rollback()
    finally:
        event.remove(migrated_engine, "before_cursor_execute", _before)

    assert state["fired"], "the trigger never matched — this test proved nothing"
