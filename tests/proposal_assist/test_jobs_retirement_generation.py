"""The jobs hook's work escapes every lock, so the SQL is the linearization point.

Retention Slice C0 (spec 4.6). build_jobs_retirement_hook runs its DB work as
wait_for(to_thread(...)), and the timeout cancels the AWAIT, not the thread —
this repo documents that at tests/proposal_assist/test_phase0_retirement.py:317.
The thread therefore escapes the coordinator's lifecycle lock exactly as it
escapes the timeout, so the coordinator's gate does NOT cover this hook: an
orphaned thread can wake up long after a later transition committed. A stale
check before the thread starts proves nothing about when it runs; the predicate
has to be in the statement itself.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import event

import call_manager.proposal_assist.jobs_retirement as jr
from call_manager.db.models import ClaimAnalysisJob, Workspace, WorkspaceSetupJob
from call_manager.proposal_assist.jobs_retirement import build_jobs_retirement_hook
from tests.proposal_assist.conftest import make_workspace


def _seed(pg_session_factory, *, generation: int):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = ws.id
        s.get(Workspace, ws_id).lifecycle_generation = generation
        s.commit()
    return ws_id


def _status(pg_session_factory, ws_id):
    with pg_session_factory() as s:
        return s.query(WorkspaceSetupJob).filter_by(workspace_id=ws_id).one().status


async def test_the_hook_cancels_at_its_own_generation(pg_session_factory):
    ws_id = _seed(pg_session_factory, generation=1)
    await build_jobs_retirement_hook(pg_session_factory)(ws_id, 1)
    assert _status(pg_session_factory, ws_id) == "cancelled"


async def test_a_stale_generation_cancels_nothing(pg_session_factory):
    """The orphaned-thread case, reduced: the hook runs with generation 1 while
    the row has already moved to 2. It must update ZERO rows — asserted on the
    row, not on whether the hook was called."""
    ws_id = _seed(pg_session_factory, generation=2)
    await build_jobs_retirement_hook(pg_session_factory)(ws_id, 1)
    assert _status(pg_session_factory, ws_id) == "queued", (
        "a superseded retirement cancelled a setup job that a later transition "
        "had brought back"
    )


async def test_a_thread_orphaned_by_the_timeout_changes_nothing(
    pg_session_factory, monkeypatch
):
    """The real shape of spec 4.6, in the real order:

      1. park the hook's DB thread and WAIT until it is genuinely parked;
      2. let the hook's own wait_for bound (_JOBS_CANCEL_SECONDS = 1.0) expire,
         so the thread is orphaned rather than merely slow;
      3. commit a later generation;
      4. release the orphan and wait for it to finish its transaction.

    Step 1 is what makes this different from the previous test. Scheduling the
    hook and then doing synchronous work never yields to the loop, so the hook
    would not have started at all — the thread would find the release already
    set and the test would silently degrade into a duplicate of the one above.
    """
    ws_id = _seed(pg_session_factory, generation=1)

    entered = threading.Event()
    release = threading.Event()
    real = jr.cancel_setup_jobs_for_workspace

    def _blocking(session, workspace_id, generation):
        entered.set()
        release.wait(timeout=30)   # bounded, so a failing test cannot hang the suite
        return real(session, workspace_id, generation)

    monkeypatch.setattr(jr, "cancel_setup_jobs_for_workspace", _blocking)

    task = asyncio.ensure_future(build_jobs_retirement_hook(pg_session_factory)(ws_id, 1))

    for _ in range(1000):          # wait for the CONDITION, not for a duration
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the hook's DB thread never parked; this test proves nothing")

    with pytest.raises(TimeoutError):   # asyncio.TimeoutError is this on 3.11+
        await task

    with pg_session_factory() as s:
        s.get(Workspace, ws_id).lifecycle_generation = 2
        s.commit()

    release.set()
    # to_thread gives no handle on its thread. This is the only boundary that
    # includes _work()'s COMMIT: an Event set by the wrapper fires before the
    # commit, and the assertion below would race it.
    await asyncio.get_running_loop().shutdown_default_executor()

    assert _status(pg_session_factory, ws_id) == "queued", (
        "an orphaned retirement thread cancelled a setup job after a later "
        "transition had moved the generation on"
    )


def _bump_generation_from_another_connection(pg_session_factory, ws_id, generation):
    """Commit a new lifecycle_generation on a SEPARATE connection.

    The helper under test locks only JOB rows (FOR UPDATE SKIP LOCKED), never the
    workspaces row, so this commit cannot block behind it — which is exactly why
    the window it opens is real.
    """
    with pg_session_factory() as other:
        other.get(Workspace, ws_id).lifecycle_generation = generation
        other.commit()


def _commit_generation_just_before_the_write(
    migrated_engine, pg_session_factory, ws_id, *, table: str, generation: int
):
    """Install a one-shot hook that commits *generation* immediately BEFORE the
    statement that WRITES to *table* executes.

    The match is `UPDATE <table>`, not the bare table name. Matching the table
    alone fires on the candidate SELECT instead, so the bump lands BEFORE that
    read — where the existing predicate already catches it — and the test passes
    against the very defect it is meant to catch. `FOR UPDATE` in the locking
    SELECT is why matching bare "UPDATE" does not work either.

    This targets the seam the mutation must not have. With the cancel split into
    SELECT-then-UPDATE, the bump lands in the gap between them and the UPDATE —
    which filters only on the id list — still terminalizes rows belonging to a
    superseded generation. With the cancel written as ONE statement, the write IS
    the only statement, so the bump commits before its snapshot is taken and the
    generation predicate excludes every row.

    Returns (state, remove) — state["fired"] records whether the hook ran at all,
    so a test can never silently pass because its trigger never matched, and
    remove() detaches the listener. The listener must NOT remove itself from
    inside the callback: SQLAlchemy is iterating its listener collection at that
    moment and mutating it raises "deque mutated during iteration". The engine is
    shared across the module's tests, so detaching in a finally is not optional.
    """
    state = {"fired": False}

    def _before(conn, cursor, statement, parameters, context, executemany):
        if state["fired"] or f"UPDATE {table}" not in statement:
            return
        state["fired"] = True
        _bump_generation_from_another_connection(pg_session_factory, ws_id, generation)

    event.listen(migrated_engine, "before_cursor_execute", _before)

    def _remove():
        event.remove(migrated_engine, "before_cursor_execute", _before)

    return state, _remove


async def test_a_generation_committed_before_the_mutation_cancels_nothing(
    pg_session_factory, migrated_engine
):
    """A restore that commits between candidate selection and the write.

    The orphaned-thread tests above pause BEFORE the helper begins, so its
    predicate already sees the new generation and they cannot observe this
    window. Here the generation moves while the helper is mid-flight — which is
    the far likelier shape, because the helper holds its own transaction open
    across both statements.

    The requirement: once the later generation has COMMITTED, no statement
    carrying a superseded generation may terminalize a row. Full serialization is
    NOT the bar and never was — this hook's thread escapes every lock the process
    holds. Reaching that bar needs the predicate to govern the MUTATION, not just
    the read that feeds it.
    """
    ws_id = _seed(pg_session_factory, generation=1)
    fired, remove = _commit_generation_just_before_the_write(
        migrated_engine,
        pg_session_factory,
        ws_id,
        table="workspace_setup_jobs",
        generation=2,
    )
    try:
        await build_jobs_retirement_hook(pg_session_factory)(ws_id, 1)
    finally:
        remove()

    assert fired["fired"], "the trigger never matched — this test proved nothing"
    assert _status(pg_session_factory, ws_id) == "queued", (
        "a superseded generation terminalized a setup job because the generation "
        "predicate governed the candidate SELECT, not the UPDATE that wrote"
    )


async def test_claim_analysis_also_gates_the_mutation_not_just_the_read(
    pg_session_factory, migrated_engine
):
    """The claim-analysis helper carries the identical shape and needs the
    identical guarantee — asserted separately so a fix to one cannot be mistaken
    for a fix to both."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        # section_index and commit_sha are NOT NULL with no server default.
        s.add(
            ClaimAnalysisJob(
                workspace_id=ws.id,
                section_index=0,
                section_sha="a" * 40,
                commit_sha="b" * 40,
                status="queued",
            )
        )
        s.commit()
        ws_id = ws.id
        s.get(Workspace, ws_id).lifecycle_generation = 1
        s.commit()

    fired, remove = _commit_generation_just_before_the_write(
        migrated_engine,
        pg_session_factory,
        ws_id,
        table="claim_analysis_jobs",
        generation=2,
    )
    try:
        with pg_session_factory() as s:
            jr.cancel_claim_analysis_jobs_for_workspace(s, ws_id, 1)
            s.commit()
    finally:
        remove()

    assert fired["fired"], "the trigger never matched — this test proved nothing"
    with pg_session_factory() as s:
        job = s.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id).one()
    assert job.status == "queued", (
        "a superseded generation terminalized a claim-analysis job because the "
        "generation predicate governed the candidate SELECT, not the UPDATE"
    )


def _restore_from_another_connection(pg_session_factory, ws_id):
    """Clear soft_deleted_at on a SEPARATE connection, exactly as the restore
    route's transaction will. reconcile locks only JOB rows, never the
    workspaces row, so this cannot block behind it — which is why the window is
    real."""
    with pg_session_factory() as other:
        other.get(Workspace, ws_id).soft_deleted_at = None
        other.commit()


def _setup_status(pg_session_factory, ws_id):
    with pg_session_factory() as s:
        return s.query(WorkspaceSetupJob).filter_by(workspace_id=ws_id).one().status


def _claim_status(pg_session_factory, ws_id):
    with pg_session_factory() as s:
        return s.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id).one().status


def test_reconcile_does_not_cancel_a_workspace_restored_mid_statement(
    pg_session_factory, migrated_engine
):
    """The same defect 28abbc4 fixed in the cancel helpers, in the last place it
    survived. Inert until C1 ships a restore; this test is what stops it being
    live the moment it does.
    """
    ws_id = _seed(pg_session_factory, generation=1)
    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    state = {"fired": False}

    def _before(conn, cursor, statement, parameters, context, executemany):
        # `UPDATE <table>`, not the bare name: the locking select contains
        # "FOR UPDATE", so a bare match fires on the READ and the bump lands
        # where the predicate already catches it — passing against the defect.
        if state["fired"] or "UPDATE workspace_setup_jobs" not in statement:
            return
        state["fired"] = True
        _restore_from_another_connection(pg_session_factory, ws_id)

    event.listen(migrated_engine, "before_cursor_execute", _before)
    try:
        with pg_session_factory() as s:
            jr.reconcile_cancelled_jobs(s)
            s.commit()
    finally:
        # NOT from inside the callback: SQLAlchemy is iterating its listener
        # collection there and mutating it raises "deque mutated during
        # iteration". The engine is shared across this module's tests.
        event.remove(migrated_engine, "before_cursor_execute", _before)

    assert state["fired"], "the trigger never matched — this test proved nothing"
    assert _setup_status(pg_session_factory, ws_id) == "queued", (
        "reconcile cancelled a setup job belonging to a workspace that had "
        "already been restored"
    )


def test_reconcile_claim_analysis_does_not_cancel_a_workspace_restored_mid_statement(
    pg_session_factory, migrated_engine
):
    """The claim-analysis half of reconcile_cancelled_jobs carries the identical
    two-statement defect as the setup half. Asserted separately so a fix to one
    cannot be mistaken for a fix to both.
    """
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(
            ClaimAnalysisJob(
                workspace_id=ws.id,
                section_index=0,
                section_sha="a" * 40,
                commit_sha="b" * 40,
                status="queued",
            )
        )
        s.commit()
        ws_id = ws.id
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    state = {"fired": False}

    def _before(conn, cursor, statement, parameters, context, executemany):
        if state["fired"] or "UPDATE claim_analysis_jobs" not in statement:
            return
        state["fired"] = True
        _restore_from_another_connection(pg_session_factory, ws_id)

    event.listen(migrated_engine, "before_cursor_execute", _before)
    try:
        with pg_session_factory() as s:
            jr.reconcile_cancelled_jobs(s)
            s.commit()
    finally:
        event.remove(migrated_engine, "before_cursor_execute", _before)

    assert state["fired"], "the trigger never matched — this test proved nothing"
    assert _claim_status(pg_session_factory, ws_id) == "queued", (
        "reconcile cancelled a claim-analysis job belonging to a workspace that "
        "had already been restored"
    )
