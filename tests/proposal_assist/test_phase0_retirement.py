"""Phase-0 retirement: cancelled status, the jobs hook, and its reconciliation."""

import asyncio
import time
import uuid
from datetime import UTC, datetime

import pytest
from alembic.config import Config
from sqlalchemy import select, text

from alembic import command
from grant_lab.db.models import WorkspaceEvent, WorkspaceSetupJob
from grant_lab.proposal_assist.jobs_retirement import (
    _JOBS_CANCEL_SECONDS,
    build_jobs_retirement_hook,
)
from grant_lab.proposal_assist.phases.setup import (
    MAX_SETUP_ATTEMPTS,
    Phase0Worker,
    WorkspaceGoneDuringSetup,
    claim_next_setup_job,
)
from tests.proposal_assist.conftest import make_workspace


def test_cancelled_is_an_accepted_status(pg_session_factory, current_user):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        job = WorkspaceSetupJob(workspace_id=ws.id, status="cancelled")
        s.add(job)
        s.commit()
        assert s.get(WorkspaceSetupJob, job.id).status == "cancelled"


def test_only_one_rubric_initialized_event_per_workspace(pg_session_factory, current_user):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        s.add(WorkspaceEvent(workspace_id=ws.id, kind="rubric_initialized", payload={}))
        s.commit()
        s.add(WorkspaceEvent(workspace_id=ws.id, kind="rubric_initialized", payload={}))
        with pytest.raises(Exception):  # noqa: B017
            s.commit()
        s.rollback()
    # A different kind is unaffected by the partial index.
    with pg_session_factory() as s:
        ws2 = make_workspace(s, owner=current_user)
        s.flush()
        s.add(WorkspaceEvent(workspace_id=ws2.id, kind="member_added", payload={}))
        s.add(WorkspaceEvent(workspace_id=ws2.id, kind="member_added", payload={}))
        s.commit()


def test_downgrade_converts_cancelled_rows_before_restoring_the_constraint(
    pg_session_factory, current_user, pg_url
):
    """The ordering IS the finding: restoring the old CheckConstraint while a
    'cancelled' row exists fails. Runs the migration's own statements (via
    `command.downgrade`, which calls 0061's `downgrade()`, which iterates
    `downgrade_statements()`), so the test cannot drift from the migration.

    `pg_session_factory` is session-scoped-DB-backed: the downgrade mutates
    the shared schema (drops the CHECK constraint's 'cancelled' branch and the
    partial unique index), so every later test in this pytest session would
    see the corrupted schema unless it's restored. The try/finally guarantees
    restoration to head even if the assertion below fails (mirrors the
    precedent in test_migration_0050_magic_link.py).
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        job = WorkspaceSetupJob(workspace_id=ws.id, status="cancelled")
        s.add(job)
        s.commit()
        job_id = job.id

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    try:
        command.downgrade(cfg, "0060")
        with pg_session_factory() as s:
            # Raw SQL, not the ORM: inside a downgraded window the head model
            # carries columns the schema does not have yet, and s.get() selects
            # all of them. Name only what exists at this revision.
            status = s.execute(
                text("SELECT status FROM workspace_setup_jobs WHERE id = :id"),
                {"id": job_id},
            ).scalar_one()
            assert status == "failed"
    finally:
        command.upgrade(cfg, "head")


def test_a_deleted_workspaces_job_is_never_claimed(pg_session_factory, current_user):
    with pg_session_factory() as s:
        live = make_workspace(s, owner=current_user)
        dead = make_workspace(s, owner=current_user)
        dead.soft_deleted_at = datetime.now(UTC)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=dead.id, status="queued"))
        s.commit()
        live_id = live.id

    # Only the deleted workspace has a queued job -> nothing claimable.
    with pg_session_factory() as s:
        assert claim_next_setup_job(session=s) is None
        s.commit()

    with pg_session_factory() as s:
        s.add(WorkspaceSetupJob(workspace_id=live_id, status="queued"))
        s.commit()

    with pg_session_factory() as s:
        job = claim_next_setup_job(session=s)
        assert job is not None
        assert job.workspace_id == live_id
        s.commit()


def test_the_hook_cancels_queued_and_running_setup_jobs(pg_session_factory, current_user):
    """Both of the states the worker drives, as the name says — the 'queued' half
    used to be fictional (the only row seeded was 'running'), so deleting
    'queued' from _CANCELLABLE left the whole suite green.

    'queued' is the COMMON case, not an edge one: create_workspace inserts the
    job as 'queued' (workspaces.py) and PHASE0_TICK_SECONDS defaults to 30, so
    a workspace deleted inside its first tick has exactly this row. Nothing else
    would ever move it — claim_next_setup_job filters deleted workspaces out in
    SQL and requeue_stuck_setup_jobs matches only 'running'.

    Two workspaces because uq_workspace_setup_jobs_ws allows one job per
    workspace; the queued one is soft-deleted, which is the real shape (the
    hook runs after the delete commits).
    """
    with pg_session_factory() as s:
        running_ws = make_workspace(s, owner=current_user)
        queued_ws = make_workspace(s, owner=current_user)
        queued_ws.soft_deleted_at = datetime.now(UTC)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=running_ws.id, status="running"))
        s.add(WorkspaceSetupJob(workspace_id=queued_ws.id, status="queued"))
        running_ws.lifecycle_generation = 1
        queued_ws.lifecycle_generation = 1
        s.commit()
        running_ws_id, queued_ws_id = running_ws.id, queued_ws.id

    hook = build_jobs_retirement_hook(pg_session_factory)
    assert asyncio.run(hook(running_ws_id, 1)) == 1
    assert asyncio.run(hook(queued_ws_id, 1)) == 1, "the hook did not cancel the queued job"

    with pg_session_factory() as s:
        for label, ws_id in (("running", running_ws_id), ("queued", queued_ws_id)):
            job = s.scalars(
                select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id)
            ).one()
            assert job.status == "cancelled", f"the {label} job was left {job.status!r}"
            assert job.completed_at is not None, f"the {label} job has no completed_at"


def test_the_hook_cancels_a_job_waiting_for_rubric_review(pg_session_factory, current_user):
    """'waiting_for_rubric_review' is NON-terminal: no completed_at, and the job
    is parked awaiting an owner action on a workspace that no longer exists.
    Excluding it from _CANCELLABLE left such a row live forever — neither the
    hook nor the reconcile tick would ever touch it again."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.soft_deleted_at = datetime.now(UTC)
        ws.lifecycle_generation = 1
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="waiting_for_rubric_review"))
        s.commit()
        ws_id = ws.id

    hook = build_jobs_retirement_hook(pg_session_factory)
    assert asyncio.run(hook(ws_id, 1)) == 1

    with pg_session_factory() as s:
        job = s.scalars(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id)
        ).one()
        assert job.status == "cancelled"
        assert job.completed_at is not None


def test_reconcile_cancels_a_waiting_for_rubric_review_row_the_hook_skipped(
    pg_session_factory, current_user
):
    """The convergence half for the same state: the hook SKIP LOCKED past the
    row, so only reconcile_cancelled_jobs can terminalize it."""
    import threading

    from grant_lab.proposal_assist.jobs_retirement import reconcile_cancelled_jobs

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.soft_deleted_at = datetime.now(UTC)
        ws.lifecycle_generation = 1
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="waiting_for_rubric_review"))
        s.commit()
        ws_id = ws.id

    holding = threading.Event()
    release = threading.Event()

    def _hold():
        with pg_session_factory() as s:
            s.scalars(
                select(WorkspaceSetupJob)
                .where(WorkspaceSetupJob.workspace_id == ws_id)
                .with_for_update()
            ).one()
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    t = threading.Thread(target=_hold)
    t.start()
    try:
        assert holding.wait(timeout=5)
        assert asyncio.run(build_jobs_retirement_hook(pg_session_factory)(ws_id, 1)) == 0
    finally:
        release.set()
        t.join(timeout=30)

    with pg_session_factory() as s:
        assert reconcile_cancelled_jobs(s) == 1
        s.commit()

    with pg_session_factory() as s:
        job = s.scalars(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id)
        ).one()
        assert job.status == "cancelled"
        assert job.completed_at is not None


def test_reconcile_cancels_a_queued_row_the_hook_skipped(pg_session_factory, current_user):
    """The convergence half for 'queued' — the common case's second exit.

    The hook SKIP LOCKED past the row (here: a concurrent holder), so
    reconcile_cancelled_jobs is the only thing left that can terminalize it.
    Without 'queued' in _CANCELLABLE the row survives BOTH passes and stays
    live forever on a workspace nobody can reach.
    """
    import threading

    from grant_lab.proposal_assist.jobs_retirement import reconcile_cancelled_jobs

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.soft_deleted_at = datetime.now(UTC)
        ws.lifecycle_generation = 1
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = ws.id

    holding = threading.Event()
    release = threading.Event()

    def _hold():
        with pg_session_factory() as s:
            s.scalars(
                select(WorkspaceSetupJob)
                .where(WorkspaceSetupJob.workspace_id == ws_id)
                .with_for_update()
            ).one()
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    t = threading.Thread(target=_hold)
    t.start()
    try:
        assert holding.wait(timeout=5)
        assert asyncio.run(build_jobs_retirement_hook(pg_session_factory)(ws_id, 1)) == 0
    finally:
        release.set()
        t.join(timeout=30)

    with pg_session_factory() as s:
        assert reconcile_cancelled_jobs(s) == 1, "reconcile left the queued row live"
        s.commit()

    with pg_session_factory() as s:
        job = s.scalars(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id)
        ).one()
        assert job.status == "cancelled"
        assert job.completed_at is not None


def test_a_locked_row_is_skipped_not_waited_on(pg_session_factory, current_user):
    """SKIP LOCKED is what bounds the hook: without it the hook inherits the
    worker's transaction lifetime and the DELETE route blocks behind it."""
    import threading

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.lifecycle_generation = 1
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="running"))
        s.commit()
        ws_id = ws.id

    holding = threading.Event()
    release = threading.Event()

    def _hold():
        with pg_session_factory() as s:
            s.scalars(
                select(WorkspaceSetupJob)
                .where(WorkspaceSetupJob.workspace_id == ws_id)
                .with_for_update()
            ).one()
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    t = threading.Thread(target=_hold)
    t.start()
    try:
        assert holding.wait(timeout=5)
        started = time.monotonic()
        assert asyncio.run(build_jobs_retirement_hook(pg_session_factory)(ws_id, 1)) == 0
        assert time.monotonic() - started < _JOBS_CANCEL_SECONDS + 1
    finally:
        release.set()
        t.join(timeout=30)


def test_the_hook_is_bounded_by_wait_for(pg_session_factory, current_user, monkeypatch):
    """Declaring a constant bounds nothing; the wait_for is the bound."""
    import grant_lab.proposal_assist.jobs_retirement as jr

    def _never_returns(session, workspace_id, generation):
        # NOTE: kept short (not 30s) deliberately. asyncio.run()'s cleanup
        # (Runner.close() -> loop.shutdown_default_executor()) blocks until any
        # orphaned to_thread thread actually finishes, even after wait_for has
        # already timed out internally — that's a property of asyncio.run()
        # wrapping a *fresh* loop per call, not of build_jobs_retirement_hook.
        # Verified directly against loop.run_until_complete() (no such cleanup
        # wait): the hook genuinely raises TimeoutError at ~_JOBS_CANCEL_SECONDS.
        # A long sleep here would make THIS test wait out the full sleep before
        # the exception surfaces, defeating both the < 5s assertion and, in the
        # 30s case, leaving a lingering thread that stalls interpreter exit.
        time.sleep(2)
        return 0

    monkeypatch.setattr(jr, "cancel_setup_jobs_for_workspace", _never_returns)
    monkeypatch.setattr(jr, "_JOBS_CANCEL_SECONDS", 0.2)

    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(jr.build_jobs_retirement_hook(pg_session_factory)(uuid.uuid4(), 1))
    assert time.monotonic() - started < 5


def test_delete_reaches_the_jobs_hook(client_authed_owner, current_user, pg_session_factory):
    """Layer tests do not prove wiring: replacing main.py's "jobs" hook with a
    no-op must fail HERE. This is the mutation that went undetected for turns."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="running"))
        s.commit()
        ws_id = ws.id

    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204
    with pg_session_factory() as s:
        job = s.scalars(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id)
        ).one()
        assert job.status == "cancelled"


def test_a_row_the_hook_skipped_is_converged_by_the_tick(pg_session_factory, current_user):
    """Full path: the hook skips a locked row, the worker dies WITHOUT
    terminalizing it (so the cooperative check cannot be what saves us), and a
    later tick cancels it."""
    import threading

    from grant_lab.proposal_assist.jobs_retirement import reconcile_cancelled_jobs

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.soft_deleted_at = datetime.now(UTC)
        ws.lifecycle_generation = 1
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="running"))
        s.commit()
        ws_id = ws.id

    holding = threading.Event()
    release = threading.Event()

    def _hold():
        with pg_session_factory() as s:
            s.scalars(
                select(WorkspaceSetupJob)
                .where(WorkspaceSetupJob.workspace_id == ws_id)
                .with_for_update()
            ).one()
            holding.set()
            release.wait(timeout=30)
            s.rollback()  # worker dies without terminalizing

    t = threading.Thread(target=_hold)
    t.start()
    try:
        assert holding.wait(timeout=5)
        assert asyncio.run(build_jobs_retirement_hook(pg_session_factory)(ws_id, 1)) == 0
    finally:
        release.set()
        t.join(timeout=30)

    with pg_session_factory() as s:
        assert (
            s.scalars(
                select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id)
            )
            .one()
            .status
            == "running"
        )

    with pg_session_factory() as s:
        assert reconcile_cancelled_jobs(s) == 1
        s.commit()

    with pg_session_factory() as s:
        assert (
            s.scalars(
                select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id)
            )
            .one()
            .status
            == "cancelled"
        )


def test_reconcile_leaves_live_workspaces_alone(pg_session_factory, current_user):
    from grant_lab.proposal_assist.jobs_retirement import reconcile_cancelled_jobs

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="running"))
        s.commit()

    with pg_session_factory() as s:
        assert reconcile_cancelled_jobs(s) == 0
        s.commit()


def test_the_reconcile_tick_is_registered_without_an_llm_client(monkeypatch, pg_url):
    """A deployment with drafting off must still converge skipped cancellations.
    The Phase-0 tick returns early when DRAFTING_ENABLED is false or the key is
    unset, so reconciliation must not live behind it.

    Constructs build_scheduler exactly as
    test_phase0_scheduler_tick.py::test_build_scheduler_registers_phase0_tick
    does: build_scheduler takes several required kwargs (alerter, ollama_url,
    gemma_model, ranking_tick_seconds, ranking_queue_batch,
    embedding_pregate_threshold, embed_client, workspace_controller) that the
    brief's shorthand `build_scheduler(settings=..., session_factory=...,
    start=False)` omits.
    """
    from unittest.mock import MagicMock

    from grant_lab.config import Settings
    from grant_lab.db.session import make_engine, make_session_factory
    from grant_lab.scheduler import build_scheduler

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")
    monkeypatch.setenv("DRAFTING_ENABLED", "false")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings()
    assert settings.drafting_enabled is False
    assert settings.anthropic_api_key is None

    engine = make_engine(settings.database_url)
    try:
        session_factory = make_session_factory(engine)

        from grant_lab.proposal_assist.drafting.factory import (
            build_workspace_controller,
        )

        workspace_controller = build_workspace_controller(
            settings=settings,
            session_factory=session_factory,
        )

        class _Alerter:
            def record_failure(self, source_slug, *, error=None):
                pass

            def record_success(self, source_slug):
                pass

        alerter = _Alerter()
        embed_client = MagicMock(model="bge-m3")
        sched = build_scheduler(
            session_factory=session_factory,
            settings=settings,
            alerter=alerter,
            ollama_url="http://localhost",
            gemma_model="gemma:test",
            ranking_tick_seconds=60,
            ranking_queue_batch=10,
            embedding_pregate_threshold=0.3,
            embed_client=embed_client,
            workspace_controller=workspace_controller,
            start=False,
        )
        try:
            ids = {j.id for j in sched.get_jobs()}
            assert "jobs_retirement_reconcile_tick" in ids
        finally:
            sched.shutdown(wait=False)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Worker self-cancel: _fail records cancelled_from (Slice C1, spec 5.2.2)
# ---------------------------------------------------------------------------

def _make_worker(pg_session_factory, tmp_path):
    from unittest.mock import Mock

    return Phase0Worker(
        session_factory=pg_session_factory,
        client=Mock(model="m"),
        extractor_version="sections-v1",
        rubric_extractor_version="rubric-v1",
        workspaces_dir=str(tmp_path / "repos"),
    )


def test_worker_fail_on_deleted_workspace_records_cancelled_from(
    pg_session_factory, tmp_path
):
    """_fail with WorkspaceGoneDuringSetup on a 'running' job of a DELETED
    workspace → cancelled, cancelled_from == 'running'.

    This is the happy path for the worker self-cancel: the workspace is gone,
    the exception is correct, and the job should be cancelled with the source
    state recorded.
    """
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        ws_id = ws.id
        job = WorkspaceSetupJob(workspace_id=ws_id, status="running")
        s.add(job)
        s.commit()
        job_id = job.id
        # Soft-delete the workspace
        ws_row = s.get(type(ws), ws_id)
        ws_row.soft_deleted_at = datetime.now(UTC)
        s.commit()

    worker = _make_worker(pg_session_factory, tmp_path)
    worker._fail(job_id, WorkspaceGoneDuringSetup("workspace deleted"))

    with pg_session_factory() as s:
        j = s.get(WorkspaceSetupJob, job_id)
        assert j.status == "cancelled"
        assert j.cancelled_from == "running"


def test_worker_fail_on_live_workspace_leaves_job_untouched(
    pg_session_factory, tmp_path
):
    """_fail with WorkspaceGoneDuringSetup on a LIVE workspace leaves the job
    completely untouched — attempts UNCHANGED.

    This is the post-restore case: the exception was raised while the workspace
    was deleted, but a restore has since brought it back. The FRESH read sees
    the workspace as live and returns without writing anything.

    We seed attempts = MAX_SETUP_ATTEMPTS - 1 so that a stray `attempts += 1`
    would dead-letter the job to 'failed', making the failure loud rather than
    silent.
    """
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        ws_id = ws.id
        job = WorkspaceSetupJob(
            workspace_id=ws_id,
            status="queued",
            attempts=MAX_SETUP_ATTEMPTS - 1,
        )
        s.add(job)
        s.commit()
        job_id = job.id
        # workspace is LIVE (soft_deleted_at IS NULL)

    worker = _make_worker(pg_session_factory, tmp_path)
    worker._fail(job_id, WorkspaceGoneDuringSetup("stale exception from before restore"))

    with pg_session_factory() as s:
        j = s.get(WorkspaceSetupJob, job_id)
        assert j.status == "queued", (
            f"live-workspace stale exception should leave status unchanged, got {j.status!r}"
        )
        assert j.attempts == MAX_SETUP_ATTEMPTS - 1, (
            f"attempts must not be incremented for a stale exception, "
            f"got {j.attempts} (would be dead-lettered to 'failed' at {MAX_SETUP_ATTEMPTS})"
        )
        assert j.cancelled_from is None


def test_worker_fail_when_workspace_row_absent_still_cancels(
    pg_session_factory, tmp_path, monkeypatch
):
    """_fail when the workspace row is ABSENT → job is cancelled.

    In production today WorkspaceSetupJob.workspace_id has
    ForeignKey('workspaces.id', ondelete='CASCADE'), so deleting the workspace
    row takes its setup jobs with it — a real DB cannot hold a setup job whose
    workspace is absent. This branch is defence for a future schema that stops
    cascading. We use a mocked session so we can return a present job but an
    absent workspace row without violating the FK.

    The fix must NOT trade one hole for another: "use the fresh read" is only
    correct if it still cancels the gone-workspace case.
    """
    from unittest.mock import MagicMock

    job_id = uuid.uuid4()
    ws_id = uuid.uuid4()

    # Build a mock job whose status is 'running' and not in the terminal set.
    mock_job = MagicMock()
    mock_job.id = job_id
    mock_job.workspace_id = ws_id
    mock_job.status = "running"

    # The mock session: get() returns the job, execute() for the workspace
    # lookup returns None (workspace row absent).
    mock_session = MagicMock()
    mock_session.get.return_value = mock_job
    mock_session.execute.return_value.one_or_none.return_value = None
    # Ensure context manager protocol works.
    mock_session.__enter__ = lambda self: self
    mock_session.__exit__ = MagicMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_session)

    worker = _make_worker(pg_session_factory, tmp_path)
    # Replace the session factory with our mock.
    worker.session_factory = mock_factory

    worker._fail(job_id, WorkspaceGoneDuringSetup("workspace gone entirely"))

    # The mock_job attributes should have been set to 'cancelled'.
    assert mock_job.cancelled_from == "running"
    assert mock_job.status == "cancelled"
    assert mock_job.completed_at is not None
