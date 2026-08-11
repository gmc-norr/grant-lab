"""Phase-0 milestone boundaries: locks, Call pinning, bounded retry."""

import json
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import (
    Call,
    CallSectionExtract,
    Workspace,
    WorkspaceEvent,
    WorkspaceSetupJob,
)
from call_manager.proposal_assist.drafting.locking import (
    CommitMutexTimeout,
    acquire_commit_mutex,
)
from call_manager.proposal_assist.extractors.refresh import persist_section_extracts
from call_manager.proposal_assist.extractors.sections import SECTION_NAMES
from call_manager.proposal_assist.phases import setup as setup_mod
from call_manager.proposal_assist.phases.setup import (
    CallSourceChanged,
    Phase0Worker,
    WorkspaceGoneDuringSetup,
)
from tests.proposal_assist.conftest import make_workspace


@dataclass
class _StubClient:
    """Returns the OpenAI-shaped envelope (`choices[0].message.content` as a
    JSON string) that the real extractors (extract_sections / extract_rubric)
    parse. A flat dict here works only for tests that never call .chat() —
    the moment a test drives _m2/_m3, a flat-dict stub breaks with a
    ValueError("unexpected envelope shape...")."""

    model: str = "stub-model"

    def chat(self, *, messages, response_format=None):
        payload = {name: f"{name} text" for name in SECTION_NAMES}
        payload["criteria"] = [
            {
                "name": "Excellence",
                "description": "Excellence of the proposal",
                "weight": 1.0,
                "scale_max": 5,
            }
        ]
        payload["funder_notes"] = "notes"
        return {
            "choices": [
                {"message": {"role": "assistant", "content": json.dumps(payload)}}
            ],
            "model": self.model,
        }


@pytest.fixture
def phase0_client():
    return _StubClient()


def test_git_runs_hermetically_and_with_a_timeout(tmp_path, monkeypatch):
    """Site git config, hooks and signing must not reach into M1's locked region,
    and every command must be able to time out rather than hang under the lock."""
    seen: dict = {}

    def _fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(setup_mod.subprocess, "run", _fake_run)
    setup_mod._run_git(["init", "-q", "-b", "main"], cwd=tmp_path)

    assert seen["kwargs"]["timeout"] == setup_mod._GIT_TIMEOUT_SECONDS
    assert "core.hooksPath=/dev/null" in seen["args"]
    assert "commit.gpgsign=false" in seen["args"]
    env = seen["kwargs"]["env"]
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_a_hung_git_raises_rather_than_blocking(tmp_path, monkeypatch):
    def _boom(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(setup_mod.subprocess, "run", _boom)
    with pytest.raises(subprocess.TimeoutExpired):
        setup_mod._run_git(["init"], cwd=tmp_path)


def _worker(pg_session_factory, tmp_path, client):
    return Phase0Worker(
        session_factory=pg_session_factory,
        client=client,
        extractor_version="v1",
        rubric_extractor_version="v1",
        workspaces_dir=str(tmp_path),
    )


def _queued_job(pg_session_factory, current_user):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = WorkspaceSetupJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        return job.id, ws.id


def _run_git_head(repo_dir: Path) -> str:
    return setup_mod._run_git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()


def test_m1_creates_the_repo_and_pins_the_call_hash(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = WorkspaceSetupJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    pinned = _worker(pg_session_factory, tmp_path, phase0_client)._m1(job_id, ws_id)

    assert isinstance(pinned, str) and pinned
    assert (tmp_path / str(ws_id) / "repo" / ".git").is_dir()
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).snapshot_refreshed_at is not None


def test_m1_refuses_a_deleted_workspace_and_creates_nothing(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    """The headline gate: no post-delete repo creation."""
    from call_manager.proposal_assist.drafting.locking import WorkspaceDeleted

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        ws.soft_deleted_at = datetime.now(UTC)
        s.flush()
        job = WorkspaceSetupJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    with pytest.raises(WorkspaceDeleted):
        _worker(pg_session_factory, tmp_path, phase0_client)._m1(job_id, ws_id)

    assert not (tmp_path / str(ws_id)).exists()


def test_a_wedged_m1_makes_delete_time_out_at_the_mutex_not_the_row(
    pg_session_factory, current_user, tmp_path, phase0_client, monkeypatch
):
    """Gate 3b. The wedge is a FILESYSTEM call, not a subprocess: if it were a
    subprocess, _GIT_TIMEOUT_SECONDS could release the row before the mutex
    timeout and the result would depend on which timeout fired first."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="running"))
        s.commit()
        ws_id = ws.id
        job_id = s.scalars(
            select(WorkspaceSetupJob.id).where(WorkspaceSetupJob.workspace_id == ws_id)
        ).one()

    inside = threading.Event()
    release = threading.Event()
    real_mkdir = Path.mkdir

    def _blocking_mkdir(self, *a, **kw):
        real_mkdir(self, *a, **kw)
        if self.name == "repo":
            inside.set()
            release.wait(timeout=30)

    monkeypatch.setattr(Path, "mkdir", _blocking_mkdir)

    worker = _worker(pg_session_factory, tmp_path, phase0_client)
    t = threading.Thread(target=lambda: worker._m1(job_id, ws_id))
    t.start()
    try:
        assert inside.wait(timeout=10)
        started = time.monotonic()
        with pg_session_factory() as s2:
            with pytest.raises(CommitMutexTimeout):
                acquire_commit_mutex(s2, ws_id, timeout_seconds=2)
            s2.rollback()
        # It returned at the mutex timeout, not by hanging on the row lock.
        assert time.monotonic() - started < 10
    finally:
        release.set()
        t.join(timeout=30)


def test_m1_acquires_the_mutex_before_the_workspace_row_lock(
    pg_session_factory, current_user, tmp_path, phase0_client, monkeypatch
):
    """Gate 3b, the ordering half. The wedge test above proves the mutex is
    HELD across M1's filesystem window; it does not prove the mutex is taken
    BEFORE the workspace row lock — a version of _m1 with the two lines
    swapped (row lock, then mutex) passes it just as well. This test records
    acquisition order directly and fails on that swap.

    Why order matters: soft_delete_workspace_db takes this SAME mutex before
    it mutates anything. Mutex-then-row means a concurrent DELETE blocks at
    the mutex and times out (429) without ever touching the row lock.
    Row-then-mutex means a DELETE can win the mutex and then block on M1's row
    lock, which by then has no timeout at all — the delete path resets
    lock_timeout to DEFAULT — i.e. an unbounded hang, the exact bug this slice
    exists to prevent.
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="running"))
        s.commit()
        ws_id = ws.id
        job_id = s.scalars(
            select(WorkspaceSetupJob.id).where(WorkspaceSetupJob.workspace_id == ws_id)
        ).one()

    order: list[str] = []
    real_acquire = setup_mod.acquire_commit_mutex_live
    real_get = Session.get

    def _tracking_acquire(session, workspace_id, *a, **kw):
        order.append("mutex")
        return real_acquire(session, workspace_id, *a, **kw)

    def _tracking_get(self, entity, ident, **kw):
        if entity is Workspace and kw.get("with_for_update"):
            order.append("row")
        return real_get(self, entity, ident, **kw)

    # Patch the name as USED inside setup.py, not where it's defined — `from
    # ... import acquire_commit_mutex_live` binds the name into setup_mod's
    # namespace at import time, so patching locking.acquire_commit_mutex_live
    # would not intercept _m1's call at all.
    monkeypatch.setattr(setup_mod, "acquire_commit_mutex_live", _tracking_acquire)
    monkeypatch.setattr(Session, "get", _tracking_get)

    worker = _worker(pg_session_factory, tmp_path, phase0_client)
    worker._m1(job_id, ws_id)

    assert order == ["mutex", "row"]


def test_m2_snapshots_sections_onto_the_workspace(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)
    pinned = w._m1(job_id, ws_id)
    w._m2(job_id, ws_id, pinned)

    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        assert set(ws.section_extracts) == set(SECTION_NAMES)
        assert s.scalars(
            select(CallSectionExtract).where(CallSectionExtract.call_id == ws.call_id)
        ).all()


def test_m2_raises_when_the_call_changed_under_it(
    pg_session_factory, current_user, tmp_path, phase0_client, monkeypatch
):
    """The re-ingest must land DURING the compute window — while _m2 holds no
    lock — which is the only window the real race occupies. Re-ingesting
    before _m2 is called at all would also be caught by a version that hashes
    the pre-lock `call_text` (the mismatch being reported here is deliberately
    pinned to the under-lock re-read at setup.py, not the pre-lock read)."""
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)
    pinned = w._m1(job_id, ws_id)

    real_compute = setup_mod.compute_section_extracts

    def _compute_then_reingest_the_call(*, call_text, client):
        result = real_compute(call_text=call_text, client=client)
        # Interference during the compute window: no session/lock is held by
        # _m2 here, which is exactly the race this milestone must survive.
        with pg_session_factory() as s:
            ws = s.get(Workspace, ws_id)
            s.get(Call, ws.call_id).full_text = "re-ingested body"
            s.commit()
        return result

    monkeypatch.setattr(
        setup_mod, "compute_section_extracts", _compute_then_reingest_the_call
    )

    with pytest.raises(CallSourceChanged):
        w._m2(job_id, ws_id, pinned)

    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).section_extracts in (None, {})


def test_m2_does_not_start_a_cloud_call_for_a_deleted_workspace(
    pg_session_factory, current_user, tmp_path
):
    """Pre-compute liveness: the delete lands after M1, before M2's LLM call."""
    calls = {"n": 0}

    @dataclass
    class _CountingClient:
        model: str = "stub-model"

        def chat(self, *, messages, response_format=None):
            calls["n"] += 1
            return _StubClient().chat(messages=messages)

    client = _CountingClient()
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, client)
    pinned = w._m1(job_id, ws_id)

    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    with pytest.raises(WorkspaceGoneDuringSetup):
        w._m2(job_id, ws_id, pinned)

    assert calls["n"] == 0


def test_m2_discards_a_payload_another_worker_made_redundant(
    pg_session_factory, current_user, tmp_path, phase0_client, monkeypatch
):
    """Two workspaces share a Call. If the cache went fresh while we computed,
    snapshot the canonical rows rather than overwriting them.

    The other worker's persist must land DURING the compute window — while
    _m2 holds no lock — so the pre-lock freshness check (which runs before
    the compute) still sees a stale cache, `sections` comes back non-None,
    and the under-lock re-check this test is actually about gets exercised."""
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)
    pinned = w._m1(job_id, ws_id)

    real_compute = setup_mod.compute_section_extracts

    def _compute_then_another_worker_wins(*, call_text, client):
        result = real_compute(call_text=call_text, client=client)
        # Interference during the compute window: another worker races in
        # and persists the canonical rows while _m2 holds no lock.
        with pg_session_factory() as s:
            ws = s.get(Workspace, ws_id)
            call = s.get(Call, ws.call_id)
            persist_section_extracts(
                session=s,
                call=call,
                sections={n: f"canonical {n}" for n in SECTION_NAMES},
                model="other-worker",
                extractor_version="v1",
                source_hash=pinned,
            )
            s.commit()
        return result

    monkeypatch.setattr(
        setup_mod, "compute_section_extracts", _compute_then_another_worker_wins
    )

    w._m2(job_id, ws_id, pinned)

    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        assert ws.section_extracts["eligibility"] == "canonical eligibility"
        rows = s.scalars(
            select(CallSectionExtract).where(CallSectionExtract.call_id == ws.call_id)
        ).all()
        assert {r.extracted_by_model for r in rows} == {"other-worker"}


def test_m3_writes_rubric_event_and_status_atomically(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)
    pinned = w._m1(job_id, ws_id)
    w._m2(job_id, ws_id, pinned)
    w._m3(job_id, ws_id, pinned)

    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        assert ws.rubric_criteria
        assert ws.rubric_updated_at is not None
        events = s.scalars(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "rubric_initialized",
            )
        ).all()
        assert len(events) == 1
        assert s.get(WorkspaceSetupJob, job_id).status == "waiting_for_rubric_review"


def test_a_cancel_beats_m3_and_is_not_overwritten(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    """A job cancelled before `_m3` is even called must stay cancelled, not
    be resurrected into waiting_for_rubric_review. Mutation testing showed
    this is caught by the pre-lock `_assert_live` liveness check — it never
    reaches the CAS inside the locked transaction, so it does NOT cover a
    cancel landing while M3 computed. For that race, see
    test_a_cancel_during_the_compute_window_beats_m3_under_lock below."""
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)
    pinned = w._m1(job_id, ws_id)
    w._m2(job_id, ws_id, pinned)

    with pg_session_factory() as s:
        s.get(WorkspaceSetupJob, job_id).status = "cancelled"
        s.commit()

    with pytest.raises(WorkspaceGoneDuringSetup):
        w._m3(job_id, ws_id, pinned)

    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "cancelled"
        assert (
            s.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == ws_id,
                    WorkspaceEvent.kind == "rubric_initialized",
                )
            ).all()
            == []
        )


def test_m3_raises_when_the_call_changed_under_it(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)
    pinned = w._m1(job_id, ws_id)
    w._m2(job_id, ws_id, pinned)

    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        s.get(Call, ws.call_id).full_text = "re-ingested body"
        s.commit()

    with pytest.raises(CallSourceChanged):
        w._m3(job_id, ws_id, pinned)

    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "running"


def test_a_cancel_during_the_compute_window_beats_m3_under_lock(
    pg_session_factory, current_user, tmp_path, phase0_client, monkeypatch
):
    """The compare-and-set exercised for real. Cancelling before calling
    `_m3` at all (test_a_cancel_beats_m3_and_is_not_overwritten above) is
    caught by the pre-lock `_assert_live` liveness read and never reaches
    the CAS inside the locked transaction — the Step-5 atomicity mutation
    proves that test stays green even when the status write is pulled out
    of the transaction. The real race the CAS exists for is a cancel that
    lands DURING the compute window, after `_assert_live` has already seen
    'running' and before the locked session re-reads the row — so the
    interference here happens from inside a `compute_rubric` wrapper,
    mirroring test_m2_raises_when_the_call_changed_under_it's pattern."""
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)
    pinned = w._m1(job_id, ws_id)
    w._m2(job_id, ws_id, pinned)

    real_compute = setup_mod.compute_rubric

    def _compute_then_cancel(*, call_text, client):
        result = real_compute(call_text=call_text, client=client)
        # Interference during the compute window: no session/lock is held by
        # _m3 here, which is exactly the race the CAS must survive.
        with pg_session_factory() as s:
            s.get(WorkspaceSetupJob, job_id).status = "cancelled"
            s.commit()
        return result

    monkeypatch.setattr(setup_mod, "compute_rubric", _compute_then_cancel)

    with pytest.raises(WorkspaceGoneDuringSetup):
        w._m3(job_id, ws_id, pinned)

    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "cancelled"
        assert (
            s.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == ws_id,
                    WorkspaceEvent.kind == "rubric_initialized",
                )
            ).all()
            == []
        )


def test_m3_raises_when_the_call_changes_during_the_compute_window(
    pg_session_factory, current_user, tmp_path, phase0_client, monkeypatch
):
    """The under-lock hash re-check exercised for real. Re-ingesting the Call
    before calling `_m3` at all (test_m3_raises_when_the_call_changed_under_it
    above) would be caught just as well by a buggy implementation that
    compared `pinned` against the pre-lock-captured `call_text` instead of
    re-reading the Call under the lock — the interference already landed
    before either read happened. This test lands the re-ingest DURING the
    compute window (inside the `compute_rubric` wrapper, no lock held),
    which only an under-lock re-read can catch."""
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)
    pinned = w._m1(job_id, ws_id)
    w._m2(job_id, ws_id, pinned)

    real_compute = setup_mod.compute_rubric

    def _compute_then_reingest_the_call(*, call_text, client):
        result = real_compute(call_text=call_text, client=client)
        with pg_session_factory() as s:
            ws = s.get(Workspace, ws_id)
            s.get(Call, ws.call_id).full_text = "re-ingested body"
            s.commit()
        return result

    monkeypatch.setattr(setup_mod, "compute_rubric", _compute_then_reingest_the_call)

    with pytest.raises(CallSourceChanged):
        w._m3(job_id, ws_id, pinned)

    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "running"


def test_a_changed_call_consumes_attempts_and_dead_letters(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    """Gate 5b: bounded retry, not a spin. Attempts 1-4 requeue, 5 dead-letters."""
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)

    for expected_attempt in range(1, 5):
        w._fail(job_id, CallSourceChanged("call re-ingested"))
        with pg_session_factory() as s:
            job = s.get(WorkspaceSetupJob, job_id)
            assert job.attempts == expected_attempt
            assert job.status == "queued"
            assert job.started_at is None
            assert "re-ingested" in job.last_error
        with pg_session_factory() as s:
            s.get(WorkspaceSetupJob, job_id).status = "running"
            s.commit()

    w._fail(job_id, CallSourceChanged("call re-ingested"))
    with pg_session_factory() as s:
        job = s.get(WorkspaceSetupJob, job_id)
        assert job.attempts == 5
        assert job.status == "failed"
        assert job.completed_at is not None


def test_a_deleted_workspace_takes_the_terminal_path_not_the_retry_path(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    _worker(pg_session_factory, tmp_path, phase0_client)._fail(
        job_id, WorkspaceGoneDuringSetup("deleted")
    )

    with pg_session_factory() as s:
        job = s.get(WorkspaceSetupJob, job_id)
        assert job.status == "cancelled"
        assert job.completed_at is not None


def test_fail_never_resurrects_an_already_terminal_job(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    with pg_session_factory() as s:
        s.get(WorkspaceSetupJob, job_id).status = "cancelled"
        s.commit()

    _worker(pg_session_factory, tmp_path, phase0_client)._fail(job_id, RuntimeError("boom"))

    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "cancelled"


def test_delete_route_returns_promptly_while_phase0_runs_its_extractors(
    client_authed_owner, current_user, pg_session_factory, tmp_path, monkeypatch
):
    """Spec gate 2, measured as ROUTE wall-clock. A slow extractor must not be
    on the DELETE route's critical path: M2/M3 compute with no transaction open
    and hold no lock while the LLM call runs."""
    from call_manager.proposal_assist.collab.room import MANAGER

    started = threading.Event()
    release = threading.Event()

    @dataclass
    class _SlowClient:
        model: str = "slow"

        def chat(self, *, messages, response_format=None):
            started.set()
            release.wait(timeout=30)
            return _StubClient().chat(messages=messages)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = WorkspaceSetupJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    w = _worker(pg_session_factory, tmp_path, _SlowClient())
    pinned = w._m1(job_id, ws_id)
    t = threading.Thread(target=lambda: w._m2(job_id, ws_id, pinned))
    t.start()
    try:
        assert started.wait(timeout=10)          # the LLM call is in flight
        began = time.monotonic()
        resp = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
        elapsed = time.monotonic() - began
        assert resp.status_code == 204
        assert elapsed < 6.0, f"DELETE took {elapsed:.1f}s during a Phase-0 extractor call"
    finally:
        release.set()
        t.join(timeout=30)
        MANAGER._retired_ws.discard(ws_id)


def test_recovery_after_a_crash_between_m1_and_m2(
    pg_session_factory, current_user, tmp_path, phase0_client
):
    """Spec gate 15. M1 committed, then the process died. Re-running from M1 must
    converge: git init is skipped, the initial commit is detected, and no work is
    duplicated."""
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)

    pinned_first = w._m1(job_id, ws_id)
    head_before = _run_git_head(tmp_path / str(ws_id) / "repo")

    pinned_again = w._m1(job_id, ws_id)          # the re-run
    assert pinned_again == pinned_first
    assert _run_git_head(tmp_path / str(ws_id) / "repo") == head_before

    w._m2(job_id, ws_id, pinned_again)
    w._m3(job_id, ws_id, pinned_again)
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "waiting_for_rubric_review"


def test_recovery_after_a_crash_between_m2_and_m3_makes_no_second_llm_call(
    pg_session_factory, current_user, tmp_path
):
    """M2 committed, then the process died. The re-run must skip the LLM entirely
    (the (hash, version) guard) and still produce exactly one audit event."""
    calls = {"n": 0}

    @dataclass
    class _CountingClient:
        model: str = "stub-model"

        def chat(self, *, messages, response_format=None):
            calls["n"] += 1
            return _StubClient().chat(messages=messages)

    client = _CountingClient()
    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, client)
    pinned = w._m1(job_id, ws_id)
    w._m2(job_id, ws_id, pinned)
    after_m2 = calls["n"]
    assert after_m2 > 0

    w._m2(job_id, ws_id, pinned)                 # the re-run
    assert calls["n"] == after_m2                # no second cloud call

    w._m3(job_id, ws_id, pinned)
    with pg_session_factory() as s:
        events = s.scalars(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "rubric_initialized",
            )
        ).all()
        assert len(events) == 1
