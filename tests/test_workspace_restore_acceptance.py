"""Workspace Restore (Retention Slice C1) — acceptance, end to end.

This file is the slice's PROOF. It adds no production code; it drives the whole
restore stack — `restore_workspace_db` (Task 4), the route (Task 5), the setup
worker's `_fail` fresh-read (Task 2), the `signal_workspace_restored` bridge
(Task 3) and the two `reconcile_cancelled_jobs` statements (Task 1) — and
asserts the behaviour every one of those tasks exists to produce.

Two layers:

  Sequential (1–6) — the setup states, the late apply, the three cancellation
  paths, cancelled turns, and the one test that checks the thing the feature is
  actually for: the OWNER can open the workspace again.

  Interleaving (7–11) — the substance. 1–6 all pass against a restore with the
  concurrency hole; only these expose it. Each drives a concurrent writer
  against an *uncommitted* restore using the `before_cursor_execute` seam from
  Task 1 (tests/proposal_assist/test_jobs_retirement_generation.py), and asserts
  `state["fired"]` so a seam that never matched cannot pass silently. The
  listener is removed in a `finally` OUTSIDE the callback — mutating the listener
  collection inside the callback raises "deque mutated during iteration".

The seam for 7 and 8 fires on the statement that clears `soft_deleted_at`
(`UPDATE workspaces`). What these tests actually prove is that restore's
`SELECT ... FOR UPDATE` on the setup-job row prevents a concurrent retirement hook
or reconciliation from leaving the job `cancelled`: dropping `.with_for_update()`
reddens BOTH tests. Cancel-survival holds under BOTH statement orderings — under
the shipped M → J → W the racing cancel hits the job lock and SKIP LOCKED skips
it; under M → W → J the racing cancel commits first and restore's FOR UPDATE
re-read reads it and repairs it via the `cancelled_from` mapping (cancelled →
queued). Inverting to M → W → J therefore stays GREEN — it is an equivalent
mutation for this cancel-survival property. The M → J → W order IS still
normative, but for a reason these single-racer seams cannot observe: deadlock
avoidance (Postgres 40P01) against the setup worker and the activate route,
enforced by the lock-order documentation. Seed an UNCANCELLED `queued` job (not a
cancelled one): a cancelled job makes restore emit its own `UPDATE
workspace_setup_jobs`, which the trigger would match at the wrong instant.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime

from sqlalchemy import event, select

import call_manager.proposal_assist.jobs_retirement as jr
from call_manager.db.models import (
    ChatTurn,
    TurnJob,
    Workspace,
    WorkspaceEvent,
    WorkspaceSetupJob,
)
from call_manager.proposal_assist.lifecycle import (
    WorkspaceLifecycle,
    signal_workspace_restored,
)
from call_manager.proposal_assist.phases.setup import (
    Phase0Worker,
    WorkspaceGoneDuringSetup,
    claim_next_setup_job,
)
from call_manager.workspaces import restore_workspace_db
from tests.admin_retention_helpers import client_for, setup_app, soft_delete
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.workspace_delete_helper import soft_delete_workspace_for_test

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _seed_deleted_with_setup_job(
    pg_session_factory, *, status: str, cancelled_from=None, owner_email=None
):
    """Create a workspace with one setup job at *status*, then soft-delete it via
    the DB half of the delete (bumps the lifecycle generation, cancels turns).
    Returns (ws_id, owner_id, job_id)."""
    with pg_session_factory() as s:
        owner = make_user(
            s, email=owner_email or f"owner-{uuid.uuid4().hex[:8]}@ki.se"
        )
        ws = make_workspace(s, owner=owner, current_phase="setup")
        s.flush()
        ws_id = ws.id
        owner_id = owner.id
        job = WorkspaceSetupJob(
            workspace_id=ws_id, status=status, cancelled_from=cancelled_from
        )
        s.add(job)
        s.commit()
        job_id = job.id

    with pg_session_factory() as s:
        soft_delete_workspace_for_test(s, workspace_id=ws_id)

    return ws_id, owner_id, job_id


def _job(pg_session_factory, job_id) -> WorkspaceSetupJob:
    with pg_session_factory() as s:
        return s.get(WorkspaceSetupJob, job_id)


def _make_worker(pg_session_factory, tmp_path) -> Phase0Worker:
    from unittest.mock import Mock

    return Phase0Worker(
        session_factory=pg_session_factory,
        client=Mock(model="m"),
        extractor_version="sections-v1",
        rubric_extractor_version="rubric-v1",
        workspaces_dir=str(tmp_path / "repos"),
    )


# ===========================================================================
# Sequential 1: deleted while running → claimed NOW (not stuck for
# STUCK_SETUP_AFTER). Drive the REAL claim function, not just status=="queued".
# ===========================================================================


def test_deleted_while_running_is_claimable_immediately_after_restore(
    pg_session_factory,
):
    """Spec 5.9.4. A workspace deleted while its setup job was 'running' comes
    back 'queued' and a worker's REAL claim path picks it up on the next tick —
    it does not wait out STUCK_SETUP_AFTER. Asserting status=='queued' alone
    would not prove claimability; claim_next_setup_job is the proof."""
    ws_id, admin_id, job_id = _seed_deleted_with_setup_job(
        pg_session_factory, status="running"
    )

    with pg_session_factory() as s:
        outcome = restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)
    assert outcome.transitioned is True
    assert outcome.setup_job_restored_to == "queued"

    # The REAL claim path must pick THIS job up now.
    with pg_session_factory() as s:
        claimed = claim_next_setup_job(session=s)
        assert claimed is not None, "no job was claimable — the restore left it stuck"
        assert claimed.id == job_id
        assert claimed.status == "running"  # the claim set it running
        s.commit()


# ===========================================================================
# Sequential 2: deleted while waiting_for_rubric_review → extraction does NOT
# re-run. Spy the extraction entry point (_m1) and drive a real worker tick.
# ===========================================================================


def test_waiting_for_rubric_review_does_not_re_run_extraction_after_restore(
    pg_session_factory, tmp_path, monkeypatch
):
    """Spec: a job restored to 'waiting_for_rubric_review' is DONE with
    extraction; a human is the next actor. A scheduler tick must not re-claim
    it (claim takes only 'queued') and extraction (_m1) must never fire."""
    ws_id, admin_id, job_id = _seed_deleted_with_setup_job(
        pg_session_factory, status="waiting_for_rubric_review"
    )

    with pg_session_factory() as s:
        outcome = restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)
    assert outcome.transitioned is True
    assert outcome.setup_job_restored_to == "waiting_for_rubric_review"

    worker = _make_worker(pg_session_factory, tmp_path)

    called = {"m1": False}
    real_m1 = worker._m1

    def _spy_m1(job_id_, workspace_id_):
        called["m1"] = True
        return real_m1(job_id_, workspace_id_)

    monkeypatch.setattr(worker, "_m1", _spy_m1)

    # A real scheduler tick: claim finds no 'queued' job for this workspace, so
    # run_one returns False and never enters extraction.
    processed = worker.run_one()

    assert processed is False, "the tick claimed a job it should have left parked"
    assert called["m1"] is False, "extraction re-ran over a completed job"

    # The row is untouched: still waiting_for_rubric_review.
    assert _job(pg_session_factory, job_id).status == "waiting_for_rubric_review"


# ===========================================================================
# Sequential 3: the late apply (spec 5.9.9). Drive the REAL bridge, force the
# first apply past the barrier, retry succeeds, original lands late, live stays.
# ===========================================================================


class _FakeState:
    pass


class _FakeApp:
    def __init__(self, loop, lifecycle):
        self.state = _FakeState()
        self.state.app_loop = loop
        self.state.workspace_lifecycle = lifecycle


class _FakeRequest:
    def __init__(self, app):
        self.app = app


def test_the_late_apply_landing_after_the_retry_leaves_the_workspace_live(
    monkeypatch,
):
    """Spec 5.9.9. The first apply exceeds the barrier and reports False; the
    retry lands the state; the original apply lands LATE and is ignored — the
    workspace stays live. Drive the real bridge, not two apply() calls.

    Load-bearing detail: the timed-out apply is NOT cancelled (spec 5.4). The
    bridge leaves its future running, so the original coroutine RESUMES after the
    barrier and runs its body to completion. This test observes that resumption
    directly (`original_body_ran`), so a bridge that calls `fut.cancel()` on
    timeout — cancelling the coroutine at its parked await — reddens it: the body
    never runs. Asserting only the final generation would NOT catch that, because
    the retry already set it."""
    lifecycle = WorkspaceLifecycle()
    ws = uuid.uuid4()
    release = threading.Event()
    original_body_ran = threading.Event()
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    real_apply = lifecycle.apply

    async def _slow_first_apply(workspace_id, *, generation, retired):
        # The FIRST apply parks past the barrier. If the bridge cancels the
        # future on timeout, this await raises CancelledError and the two lines
        # below never run — which is exactly what this test must catch.
        await asyncio.to_thread(release.wait, 30)
        original_body_ran.set()
        return await real_apply(workspace_id, generation=generation, retired=retired)

    try:
        req = _FakeRequest(_FakeApp(loop, lifecycle))

        # First apply: parked, barrier expires → bridge reports False.
        monkeypatch.setattr(lifecycle, "apply", _slow_first_apply)
        monkeypatch.setattr(
            "call_manager.proposal_assist.lifecycle._RESTORE_BARRIER_SECONDS", 0.2
        )
        assert (
            signal_workspace_restored(req, workspace_id=ws, generation=5) is False
        )
        assert lifecycle.applied_generation(ws) == 0, "the slow apply must not have landed"

        # The retry uses the real (fast) apply and lands generation 5.
        monkeypatch.setattr(lifecycle, "apply", real_apply)
        assert (
            signal_workspace_restored(req, workspace_id=ws, generation=5) is True
        )
        assert lifecycle.applied_generation(ws) == 5
        assert lifecycle.is_retired(ws) is False

        # Now release the ORIGINAL slow apply. Because the bridge did NOT cancel
        # it, the coroutine resumes and runs its body to completion.
        release.set()
        assert original_body_ran.wait(timeout=5), (
            "the timed-out apply was cancelled: its coroutine never resumed after "
            "the barrier. Spec 5.4 requires the future to keep running so a late "
            "arrival and the retry converge."
        )

        # It carried the SAME generation (5), so monotonicity ignores it — the
        # workspace stays live at generation 5, not retired.
        assert lifecycle.applied_generation(ws) == 5, "the late apply moved the state"
        assert lifecycle.is_retired(ws) is False, "the late apply flipped it to retired"
    finally:
        release.set()
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)


# ===========================================================================
# Sequential 4: all three cancellation paths → restore works. One per path.
# ===========================================================================


def test_restore_after_the_retirement_hook_cancelled_the_job(pg_session_factory):
    """Cancellation path 1 (the hook). cancel_setup_jobs_for_workspace records
    cancelled_from='running'; restore maps it back to a progressing 'queued'."""
    ws_id, admin_id, job_id = _seed_deleted_with_setup_job(
        pg_session_factory, status="running"
    )
    # The hook cancels at the deletion generation (bumped by the delete).
    with pg_session_factory() as s:
        gen = s.execute(
            select(Workspace.lifecycle_generation).where(Workspace.id == ws_id)
        ).scalar_one()
        jr.cancel_setup_jobs_for_workspace(s, ws_id, gen)
        s.commit()
    assert _job(pg_session_factory, job_id).status == "cancelled"
    assert _job(pg_session_factory, job_id).cancelled_from == "running"

    with pg_session_factory() as s:
        outcome = restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)
    assert outcome.setup_job_restored_to == "queued"
    assert _job(pg_session_factory, job_id).status == "queued"


def test_restore_after_reconciliation_cancelled_the_job(pg_session_factory):
    """Cancellation path 2 (reconciliation). reconcile_cancelled_jobs terminalizes
    a soft-deleted workspace's setup job; restore maps it back."""
    ws_id, admin_id, job_id = _seed_deleted_with_setup_job(
        pg_session_factory, status="queued"
    )
    with pg_session_factory() as s:
        jr.reconcile_cancelled_jobs(s)
        s.commit()
    assert _job(pg_session_factory, job_id).status == "cancelled"
    assert _job(pg_session_factory, job_id).cancelled_from == "queued"

    with pg_session_factory() as s:
        outcome = restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)
    assert outcome.setup_job_restored_to == "queued"
    assert _job(pg_session_factory, job_id).status == "queued"


def test_restore_after_the_worker_self_cancelled_the_job(
    pg_session_factory, tmp_path
):
    """Cancellation path 3 (the worker). Phase0Worker._fail on a deleted
    workspace records cancelled_from and cancels; restore maps it back."""
    ws_id, admin_id, job_id = _seed_deleted_with_setup_job(
        pg_session_factory, status="running"
    )
    worker = _make_worker(pg_session_factory, tmp_path)
    worker._fail(job_id, WorkspaceGoneDuringSetup("workspace deleted"))
    assert _job(pg_session_factory, job_id).status == "cancelled"
    assert _job(pg_session_factory, job_id).cancelled_from == "running"

    with pg_session_factory() as s:
        outcome = restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)
    assert outcome.setup_job_restored_to == "queued"
    assert _job(pg_session_factory, job_id).status == "queued"


# ===========================================================================
# Sequential 5: cancelled turns stay cancelled after a restore.
# ===========================================================================


def test_cancelled_turns_stay_cancelled_after_restore(pg_session_factory):
    """Restore touches only setup jobs and workspace metadata. A TurnJob the
    delete cancelled must remain cancelled — restore does not resurrect turns."""
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        s.flush()
        ws_id = ws.id
        admin_id = owner.id
        ut = ChatTurn(
            workspace_id=ws_id, role="user", content={"type": "text", "text": "hi"}
        )
        s.add(ut)
        s.flush()
        tj = TurnJob(
            workspace_id=ws_id,
            user_turn_id=ut.id,
            submitted_by_user_id=owner.id,
            status="queued",
            queued_at=datetime.now(UTC),
        )
        s.add(tj)
        s.commit()
        tj_id = tj.id

    # The DB half of delete cancels queued turn jobs.
    with pg_session_factory() as s:
        soft_delete_workspace_for_test(s, workspace_id=ws_id)
    with pg_session_factory() as s:
        assert s.get(TurnJob, tj_id).status == "cancelled"

    with pg_session_factory() as s:
        restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)

    with pg_session_factory() as s:
        assert s.get(TurnJob, tj_id).status == "cancelled", (
            "restore resurrected a cancelled turn"
        )


# ===========================================================================
# Sequential 6: the OWNER can open it again (spec 5.9.1). Owner, not admin,
# GETs the workspace via the cockpit endpoint its UI uses → 200, not 404.
# ===========================================================================


def test_the_owner_can_open_the_workspace_again_after_restore(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Spec 5.9.1 — the thing the feature is for. A deleted workspace 404s the
    owner (require_member filters soft_deleted_at). After an admin restores it,
    the OWNER GETs the cockpit read-model the UI opens and receives 200."""
    app = setup_app(monkeypatch, pg_url, tmp_path)

    # Owner + their workspace.
    with pg_session_factory() as s:
        owner = make_user(s, email="cockpit-owner@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="setup")
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="running"))
        s.commit()
        ws_id = ws.id
        owner_id = owner.id

    # The owner's authenticated client (a distinct helper account is the admin).
    owner_client = client_for(app, pg_session_factory, email="junk@ki.se", is_admin=False)[0]
    owner_client.cookies.set(
        "session", app.state.session_signer.make(user_id=owner_id)
    )
    admin_client, _ = client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)

    cockpit = f"/api/workspaces/{ws_id}/cockpit-meta"

    # Deleted → the owner is 404 on the endpoint their UI opens.
    with pg_session_factory() as s:
        soft_delete(s, ws_id)
    r_deleted = owner_client.get(cockpit)
    assert r_deleted.status_code == 404, r_deleted.text

    # Admin restores.
    with admin_client:
        r_restore = admin_client.post(
            f"/api/admin/workspaces/deleted/{ws_id}/restore"
        )
    assert r_restore.status_code == 204, r_restore.text

    # Owner can open it again → 200.
    r_open = owner_client.get(cockpit)
    assert r_open.status_code == 200, r_open.text
    body = r_open.json()
    assert body["role"] == "owner"
    # running → queued: the cockpit shows a progressing setup, not a stuck one.
    assert body["setup_status"] == "queued"


# ===========================================================================
# Interleaving seams (7–10) — the substance of this task.
# ===========================================================================


def _cancel_from_another_connection(pg_session_factory, ws_id, *, generation):
    """Run the retirement hook's DB half on a SEPARATE connection, exactly as it
    runs in production off its own thread. It sees the COMMITTED (deleted) state
    and cancels at *generation*."""
    with pg_session_factory() as other:
        jr.cancel_setup_jobs_for_workspace(other, ws_id, generation)
        other.commit()


def _reconcile_from_another_connection(pg_session_factory):
    """Run reconciliation on a SEPARATE connection. It sees the COMMITTED
    (deleted) state for every soft-deleted workspace."""
    with pg_session_factory() as other:
        jr.reconcile_cancelled_jobs(other)
        other.commit()


# ---------------------------------------------------------------------------
# 7. The retirement hook races the restore.
# ---------------------------------------------------------------------------


def test_the_retirement_hook_racing_the_restore_cannot_leave_it_cancelled(
    pg_session_factory, migrated_engine
):
    """Interleaving. From another connection run cancel_setup_jobs_for_workspace
    (which still sees the committed, deleted state) AFTER restore has locked the
    job and BEFORE it commits. After both commit the workspace is live and the
    setup job is in a progressing state — NOT cancelled.

    The seam fires on `UPDATE workspaces` (the soft_deleted_at clear). Under the
    normative M → J → W order that statement runs after the job SELECT ... FOR
    UPDATE, so at the instant the hook fires restore holds the job lock and the
    hook's SKIP LOCKED passes over it. Seed an UNCANCELLED `queued` job so
    restore's repair is a no-op (queued → queued writes nothing), which is what
    makes a dropped `.with_for_update()` leave a surviving cancel for this test
    to catch. The M → W → J inversion does NOT — it is equivalent for this
    property; see the module docstring.
    """
    ws_id, admin_id, job_id = _seed_deleted_with_setup_job(
        pg_session_factory, status="queued"
    )
    with pg_session_factory() as s:
        del_gen = s.execute(
            select(Workspace.lifecycle_generation).where(Workspace.id == ws_id)
        ).scalar_one()

    state = {"fired": False}

    def _before(conn, cursor, statement, parameters, context, executemany):
        # Fire on the workspace clear, NOT the job SELECT. `UPDATE workspaces`
        # is the soft_deleted_at write; matching it means restore already holds
        # the job lock under M → J → W.
        if state["fired"] or "UPDATE workspaces" not in statement:
            return
        state["fired"] = True
        _cancel_from_another_connection(pg_session_factory, ws_id, generation=del_gen)

    event.listen(migrated_engine, "before_cursor_execute", _before)
    try:
        with pg_session_factory() as s:
            restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)
    finally:
        event.remove(migrated_engine, "before_cursor_execute", _before)

    assert state["fired"], "the trigger never matched — this test proved nothing"

    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        assert ws.soft_deleted_at is None, "workspace is not live after restore"
        job = s.get(WorkspaceSetupJob, job_id)
        assert job.status != "cancelled", (
            "a retirement hook racing the restore left the setup job cancelled — "
            "the workspace is live but permanently stuck"
        )
        assert job.status == "queued"


# ---------------------------------------------------------------------------
# 8. Reconciliation races the restore. Same shape and seam.
# ---------------------------------------------------------------------------


def test_reconciliation_racing_the_restore_cannot_leave_it_cancelled(
    pg_session_factory, migrated_engine
):
    """Interleaving. Identical to 7 but the racing writer is
    reconcile_cancelled_jobs. Same seam: fire on the `UPDATE workspaces` clear,
    so under M → J → W restore holds the job lock and reconciliation's SKIP
    LOCKED passes over it."""
    ws_id, admin_id, job_id = _seed_deleted_with_setup_job(
        pg_session_factory, status="queued"
    )

    state = {"fired": False}

    def _before(conn, cursor, statement, parameters, context, executemany):
        if state["fired"] or "UPDATE workspaces" not in statement:
            return
        state["fired"] = True
        _reconcile_from_another_connection(pg_session_factory)

    event.listen(migrated_engine, "before_cursor_execute", _before)
    try:
        with pg_session_factory() as s:
            restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)
    finally:
        event.remove(migrated_engine, "before_cursor_execute", _before)

    assert state["fired"], "the trigger never matched — this test proved nothing"

    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        assert ws.soft_deleted_at is None, "workspace is not live after restore"
        job = s.get(WorkspaceSetupJob, job_id)
        assert job.status != "cancelled", (
            "reconciliation racing the restore left the setup job cancelled"
        )
        assert job.status == "queued"


# ---------------------------------------------------------------------------
# 9. An uncancelled running job (SKIP LOCKED survivor) is normalized to queued.
# ---------------------------------------------------------------------------


def test_uncancelled_running_job_is_normalized_and_claimable(pg_session_factory):
    """Spec 5.9. A setup job still 'running' with cancelled_from IS NULL — the
    hook's SKIP LOCKED passed over a row a live worker held, and that worker died
    with the deletion. Restore treats the uncancelled active status as the
    interrupted SOURCE and normalizes it to 'queued', claimable now. It is NOT
    left 'running' for STUCK_SETUP_AFTER.

    Distinct from a *cancelled* job with cancelled_from IS NULL, which restore
    leaves alone."""
    ws_id, admin_id, job_id = _seed_deleted_with_setup_job(
        pg_session_factory, status="running", cancelled_from=None
    )
    # Sanity: it really is an uncancelled running row.
    assert _job(pg_session_factory, job_id).status == "running"
    assert _job(pg_session_factory, job_id).cancelled_from is None

    with pg_session_factory() as s:
        outcome = restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)
    assert outcome.setup_job_restored_to == "queued"

    with pg_session_factory() as s:
        job = s.get(WorkspaceSetupJob, job_id)
        assert job.status == "queued"
        assert job.started_at is None

    # And it is claimable NOW.
    with pg_session_factory() as s:
        claimed = claim_next_setup_job(session=s)
        assert claimed is not None and claimed.id == job_id
        s.commit()


# ---------------------------------------------------------------------------
# 10. A late _fail after restore does not re-cancel.
# ---------------------------------------------------------------------------


def test_a_late_fail_after_restore_does_not_re_cancel(pg_session_factory, tmp_path):
    """Spec. A worker raised WorkspaceGoneDuringSetup while the workspace was
    deleted; a restore then superseded that. The stale exception must not
    re-cancel the job: _fail's FRESH read sees the workspace live and returns
    without a write, so the job stays 'queued' (charged no attempt), not
    'cancelled'."""
    ws_id, admin_id, job_id = _seed_deleted_with_setup_job(
        pg_session_factory, status="running"
    )

    # Restore first: the workspace is live and the job is 'queued' again.
    with pg_session_factory() as s:
        restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)
    assert _job(pg_session_factory, job_id).status == "queued"
    attempts_before = _job(pg_session_factory, job_id).attempts

    # The late, stale exception arrives.
    worker = _make_worker(pg_session_factory, tmp_path)
    worker._fail(job_id, WorkspaceGoneDuringSetup("stale: workspace was deleted"))

    job = _job(pg_session_factory, job_id)
    assert job.status == "queued", (
        f"a stale WorkspaceGoneDuringSetup re-cancelled a restored job "
        f"(status={job.status!r})"
    )
    assert job.cancelled_from is None
    assert job.attempts == attempts_before, "the stale exception charged an attempt"


# ---------------------------------------------------------------------------
# 11. A restore that loses the mutex race must return the WINNER's generation.
# ---------------------------------------------------------------------------


def _restore_from_another_connection(pg_session_factory, ws_id, admin_id):
    """Run a COMPLETE restore on a SEPARATE connection and commit it. Returns
    the generation it allocated."""
    with pg_session_factory() as other:
        return restore_workspace_db(other, workspace_id=ws_id, admin_id=admin_id).generation


def test_a_restore_that_loses_the_mutex_race_returns_the_winners_generation(
    pg_session_factory, migrated_engine
):
    """Interleaving. Two admins restore the same deleted workspace. B reads the
    row while it is still DELETED, then blocks on the commit mutex; A wins,
    commits generation G+1 and goes live; B finally takes the mutex, sees LIVE
    and takes the idempotent path.

    The generation B returns must be A's G+1, not the G cached on the instance
    it loaded before the mutex. It is the caller's fault line, not a cosmetic
    one: the route hands this straight to signal_workspace_restored, the
    lifecycle authority rejects a superseded generation as already-retired, and
    the route answers 204 either way — so a stale G leaves the database live
    while in-process access stays retired, with nothing on screen saying so.

    The seam fires on `pg_advisory_xact_lock`, i.e. after B's pre-mutex load and
    before B holds the mutex — the only window in which A can win. B holds no
    locks at that instant, so A acquires the same mutex, commits and releases it
    inside the callback without deadlocking.
    """
    ws_id, admin_id, job_id = _seed_deleted_with_setup_job(
        pg_session_factory, status="queued"
    )
    with pg_session_factory() as s:
        del_gen = s.execute(
            select(Workspace.lifecycle_generation).where(Workspace.id == ws_id)
        ).scalar_one()

    state: dict = {"fired": False, "winner_gen": None}

    def _before(conn, cursor, statement, parameters, context, executemany):
        # A's own restore runs the same statement; the `fired` latch keeps this
        # from recursing.
        if state["fired"] or "pg_advisory_xact_lock" not in statement:
            return
        state["fired"] = True
        state["winner_gen"] = _restore_from_another_connection(
            pg_session_factory, ws_id, admin_id
        )

    event.listen(migrated_engine, "before_cursor_execute", _before)
    try:
        with pg_session_factory() as s:
            outcome = restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)
    finally:
        event.remove(migrated_engine, "before_cursor_execute", _before)

    assert state["fired"], "the trigger never matched — this test proved nothing"
    winner_gen = state["winner_gen"]
    # Without this the assertion below could pass on two equal numbers.
    assert winner_gen > del_gen, (
        f"the winning restore did not advance the generation "
        f"({winner_gen} vs deleted-state {del_gen}) — the seam fired too late"
    )

    assert outcome.transitioned is False, (
        "B wrote a transition even though A had already made the workspace live"
    )
    assert outcome.generation == winner_gen, (
        f"the losing restore returned generation {outcome.generation} "
        f"(the value cached before the mutex) instead of the winner's "
        f"{winner_gen}; applying it would be rejected as superseded while the "
        f"route still answered 204"
    )

    # A did the real work: live, audited once, job progressing.
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is None
        assert s.get(WorkspaceSetupJob, job_id).status == "queued"
        events = s.execute(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "admin_deleted_workspace_restored",
            )
        ).scalars().all()
    assert len(events) == 1, f"expected exactly one restore audit row, got {len(events)}"
