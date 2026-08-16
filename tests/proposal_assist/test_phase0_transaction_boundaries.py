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
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from call_manager.db.models import (
    Call,
    CallRubric,
    CallSectionExtract,
    Workspace,
    WorkspaceEvent,
    WorkspaceSetupJob,
)
from call_manager.proposal_assist.drafting.locking import (
    CommitMutexTimeout,
    acquire_commit_mutex,
)
from call_manager.proposal_assist.extractors.refresh import (
    persist_rubric,
    persist_section_extracts,
)
from call_manager.proposal_assist.extractors.sections import SECTION_NAMES
from call_manager.proposal_assist.hashing import source_text_hash
from call_manager.proposal_assist.phases import setup as setup_mod
from call_manager.proposal_assist.phases.setup import (
    CallSourceChanged,
    Phase0Worker,
    WorkspaceGoneDuringSetup,
)
from tests.proposal_assist.conftest import make_call, make_workspace


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


def _assert_already_locked(session_factory, stmt, *, what: str) -> None:
    """From a THIRD connection, `SELECT ... FOR UPDATE NOWAIT` must fail with
    55P03 (lock_not_available) — proving somebody else already holds the row.

    NOWAIT is what makes this a probe rather than a participant: a plain
    `FOR UPDATE` would join the wait queue and tell us nothing.
    """
    with session_factory() as probe:
        with pytest.raises(OperationalError) as excinfo:
            probe.scalars(stmt.with_for_update(nowait=True)).one()
        orig = excinfo.value.orig
        sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
        assert sqlstate == "55P03", (
            f"expected 55P03 (lock_not_available) probing {what}, got {sqlstate}: {orig}"
        )
        probe.rollback()


def test_m2_holds_the_workspace_row_before_it_blocks_on_the_call_row(
    pg_session_factory, current_user, tmp_path, phase0_client, monkeypatch
):
    """The acquisition ORDER, not merely the fact that a lock is taken.

    The branch's thesis is one global order — commit mutex < workspace_setup_jobs
    < workspaces < calls. Gate 6
    (test_two_workspaces_sharing_a_call_serialize_m2_on_the_call_row) proves two
    workers serialize on the Call row, but it passes just as well if _m2 took the
    Call row BEFORE the workspace row, so it does not pin the order at all.

    The technique that does: connection B takes and holds the row that comes
    SECOND (the Call row). _m2, started in a thread, must therefore already HOLD
    the row that comes FIRST (the workspace row) while it sits blocked on B. A
    third connection C proves that by failing to take the workspace row with
    NOWAIT. Under the inverted order _m2 would be blocked on the Call row having
    acquired nothing, and C's NOWAIT would succeed.

    Why the order matters: soft_delete_workspace_db updates the workspace row
    while holding the commit mutex. _m2 taking the Call row first and the
    workspace row second would mean an in-flight ingestion batch (which holds
    Call rows for its whole upsert loop) could park _m2 between the two
    acquisitions, and DELETE-vs-_m2 would then be an ordinary lock cycle rather
    than a queue.
    """
    monkeypatch.setattr(setup_mod, "_ROW_LOCK_TIMEOUT_SECONDS", 60)  # must not fire

    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)
    pinned = w._m1(job_id, ws_id)                 # BEFORE the Session.get patch
    with pg_session_factory() as s:
        call_id = s.get(Workspace, ws_id).call_id

    ws_row_locked = threading.Event()
    real_get = Session.get

    def _tracking_get(self, entity, ident, **kw):
        obj = real_get(self, entity, ident, **kw)
        if entity is Workspace and kw.get("with_for_update"):
            ws_row_locked.set()
        return obj

    monkeypatch.setattr(Session, "get", _tracking_get)

    b_holding = threading.Event()
    release_b = threading.Event()

    def _b_holds_the_call_row():
        with pg_session_factory() as b:
            b.scalars(select(Call).where(Call.id == call_id).with_for_update()).one()
            b_holding.set()
            release_b.wait(timeout=30)
            b.rollback()

    outcome: list[BaseException | None] = []

    def _run_m2():
        try:
            w._m2(job_id, ws_id, pinned)
        except BaseException as exc:  # noqa: BLE001 — asserted after join
            outcome.append(exc)
        else:
            outcome.append(None)

    b = threading.Thread(target=_b_holds_the_call_row)
    t = threading.Thread(target=_run_m2)
    b.start()
    try:
        assert b_holding.wait(timeout=10), "B never took the Call row"
        t.start()
        assert ws_row_locked.wait(timeout=15), (
            "_m2 never acquired the workspace row while blocked on the Call row "
            "— it took the Call row FIRST, inverting the lock order"
        )
        _assert_already_locked(
            pg_session_factory,
            select(Workspace).where(Workspace.id == ws_id),
            what="the workspace row _m2 must already hold",
        )
    finally:
        release_b.set()
        b.join(timeout=30)
        t.join(timeout=30)

    assert outcome == [None], f"_m2 did not finish cleanly after B released: {outcome}"


def test_m3_holds_the_job_row_before_it_blocks_on_the_workspace_row(
    pg_session_factory, current_user, tmp_path, phase0_client, monkeypatch
):
    """The other half of the order, and the one with no coverage at all: _m3
    takes the SETUP-JOB row before the workspace row.

    That correction is the actual deadlock fix — the web `activate` route
    (rubric.py) locks workspace_setup_jobs before it flushes its workspace-row
    update, so a workspace-then-job _m3 inverts against it and gives Postgres a
    real 40P01 shape. Same discriminating technique as the _m2 order test above:
    connection B holds the SECOND row (workspace), and a third connection C must
    find the FIRST row (the job) already held by _m3.
    """
    monkeypatch.setattr(setup_mod, "_ROW_LOCK_TIMEOUT_SECONDS", 60)  # must not fire

    job_id, ws_id = _queued_job(pg_session_factory, current_user)
    w = _worker(pg_session_factory, tmp_path, phase0_client)
    pinned = w._m1(job_id, ws_id)                 # BEFORE the Session.get patch
    w._m2(job_id, ws_id, pinned)

    job_row_locked = threading.Event()
    real_get = Session.get

    def _tracking_get(self, entity, ident, **kw):
        obj = real_get(self, entity, ident, **kw)
        if entity is WorkspaceSetupJob and kw.get("with_for_update"):
            job_row_locked.set()
        return obj

    monkeypatch.setattr(Session, "get", _tracking_get)

    b_holding = threading.Event()
    release_b = threading.Event()

    def _b_holds_the_workspace_row():
        with pg_session_factory() as b:
            b.scalars(select(Workspace).where(Workspace.id == ws_id).with_for_update()).one()
            b_holding.set()
            release_b.wait(timeout=30)
            b.rollback()

    outcome: list[BaseException | None] = []

    def _run_m3():
        try:
            w._m3(job_id, ws_id, pinned)
        except BaseException as exc:  # noqa: BLE001 — asserted after join
            outcome.append(exc)
        else:
            outcome.append(None)

    b = threading.Thread(target=_b_holds_the_workspace_row)
    t = threading.Thread(target=_run_m3)
    b.start()
    try:
        assert b_holding.wait(timeout=10), "B never took the workspace row"
        t.start()
        assert job_row_locked.wait(timeout=15), (
            "_m3 never acquired the setup-job row while blocked on the workspace "
            "row — it took the workspace row FIRST, inverting the lock order"
        )
        _assert_already_locked(
            pg_session_factory,
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.id == job_id),
            what="the setup-job row _m3 must already hold",
        )
    finally:
        release_b.set()
        b.join(timeout=30)
        t.join(timeout=30)

    assert outcome == [None], f"_m3 did not finish cleanly after B released: {outcome}"
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "waiting_for_rubric_review"


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
    lock — which Slice C3 now caps at 2s rather than leaving uncapped, so the
    DELETE gives up instead of hanging. Bounded or not, it comes back 429 having
    deleted nothing: the exact bug this acquisition order exists to prevent.
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
    """If the shared Call's extract cache goes fresh while we compute (another
    worker or workspace's _m2 wins the race first), snapshot the canonical
    rows rather than overwrite them with our own now-redundant payload.

    This is a SINGLE workspace with the other worker simulated inline (a
    direct persist_section_extracts call from a second session, not a second
    real _m2 run) — it exercises the under-lock freshness re-check in
    isolation. For two workspaces actually racing two concurrent `_m2` calls
    against the same Call row, see
    test_two_workspaces_sharing_a_call_serialize_m2_on_the_call_row below.

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


def test_two_workspaces_sharing_a_call_serialize_m2_on_the_call_row(
    pg_session_factory, current_user, tmp_path, phase0_client, monkeypatch
):
    """Gate 6. Two DIFFERENT workspaces, ONE shared Call, two REAL concurrent
    `_m2` runs (actual threads, not simulated interference like the test
    above) — they must serialize on the Call row's `with_for_update` lock, and
    the shared `CallSectionExtract` rows must end consistent (one section set,
    one model, both workspaces snapshot it).

    Mechanism: patch `persist_section_extracts` so whichever thread gets there
    FIRST (there's a real race — either workspace's job can win) pauses right
    after it has locked the Call row but before it commits. The other thread's
    own `with_for_update` SELECT on that same Call row blocks in Postgres for
    the whole pause — proven by asserting it has NOT finished `_m2` while the
    winner is paused, then that it DOES finish shortly after we release the
    winner. A version of _m2 that dropped `with_for_update` on the Call row
    would let the loser run straight through instead of blocking, so this
    assertion is the one that actually discriminates the lock (verified by
    hand: dropping `with_for_update=True` from the Call row acquisition in
    _m2 turns this red — either on the "loser already finished" assertion or,
    if timing races the other way, on a primary-key IntegrityError from both
    threads inserting the same (call_id, section) rows unguarded).
    """
    with pg_session_factory() as s:
        call = make_call(s, external_id="EXT-shared-m2")
        ws_a = make_workspace(s, owner=current_user, call_id=call.id)
        ws_b = make_workspace(s, owner=current_user, call_id=call.id)
        s.add(WorkspaceSetupJob(workspace_id=ws_a.id, status="running"))
        s.add(WorkspaceSetupJob(workspace_id=ws_b.id, status="running"))
        s.flush()
        job_a_id = s.scalars(
            select(WorkspaceSetupJob.id).where(WorkspaceSetupJob.workspace_id == ws_a.id)
        ).one()
        job_b_id = s.scalars(
            select(WorkspaceSetupJob.id).where(WorkspaceSetupJob.workspace_id == ws_b.id)
        ).one()
        call_id, ws_a_id, ws_b_id = call.id, ws_a.id, ws_b.id
        pinned = source_text_hash(call.full_text or "")
        s.commit()

    w = _worker(pg_session_factory, tmp_path, phase0_client)

    claim_lock = threading.Lock()
    winner_tid: dict[str, int | None] = {"tid": None}
    winner_paused = threading.Event()
    release_winner = threading.Event()
    done = {"a": threading.Event(), "b": threading.Event()}
    errors: dict[str, BaseException] = {}

    real_persist = setup_mod.persist_section_extracts

    def _pausing_persist(*, session, call, **kwargs):
        with claim_lock:
            is_winner = winner_tid["tid"] is None
            if is_winner:
                winner_tid["tid"] = threading.get_ident()
        if is_winner:
            winner_paused.set()
            release_winner.wait(timeout=10)
        return real_persist(session=session, call=call, **kwargs)

    monkeypatch.setattr(setup_mod, "persist_section_extracts", _pausing_persist)

    def _run(job_id, ws_id, key):
        try:
            w._m2(job_id, ws_id, pinned)
        except BaseException as exc:  # noqa: BLE001 — surfaced via `errors`, not silenced
            errors[key] = exc
        finally:
            done[key].set()

    t_a = threading.Thread(target=_run, args=(job_a_id, ws_a_id, "a"))
    t_b = threading.Thread(target=_run, args=(job_b_id, ws_b_id, "b"))
    t_a.start()
    t_b.start()
    try:
        assert winner_paused.wait(timeout=10), "neither thread reached the persist point"
        loser_key = "b" if winner_tid["tid"] == t_a.ident else "a"

        # The loser is blocked in Postgres acquiring the Call row the winner
        # is still holding (uncommitted) — it must NOT have finished yet.
        assert not done[loser_key].wait(timeout=0.5), (
            f"the loser ({loser_key}) finished _m2 before the winner released "
            "the Call row — they did not serialize on it"
        )

        release_winner.set()

        assert done[loser_key].wait(timeout=10), "loser never unblocked after release"
        assert t_a.join(timeout=10) is None and not t_a.is_alive()
        assert t_b.join(timeout=10) is None and not t_b.is_alive()
    finally:
        release_winner.set()
        t_a.join(timeout=10)
        t_b.join(timeout=10)

    assert errors == {}

    with pg_session_factory() as s:
        rows = s.scalars(
            select(CallSectionExtract).where(CallSectionExtract.call_id == call_id)
        ).all()
        # One consistent set of section rows for the shared Call — the loser's
        # under-lock freshness re-check saw the winner's committed rows and
        # discarded its own now-redundant payload instead of persisting a
        # second, conflicting copy.
        assert {r.section for r in rows} == set(SECTION_NAMES)
        assert len({r.extracted_by_model for r in rows}) == 1

        ws_a_after = s.get(Workspace, ws_a_id)
        ws_b_after = s.get(Workspace, ws_b_id)
        assert set(ws_a_after.section_extracts) == set(SECTION_NAMES)
        assert set(ws_b_after.section_extracts) == set(SECTION_NAMES)
        # Both workspaces snapshot the SAME canonical text (whichever model
        # won), not a mix of the winner's and loser's own computed payloads.
        assert ws_a_after.section_extracts == ws_b_after.section_extracts


def test_m2_times_out_on_a_held_cache_row_rather_than_pinning_the_workspace(
    pg_session_factory, current_user, tmp_path, phase0_client, monkeypatch
):
    """The lock_timeout must bound the whole persist transaction, not just the
    `with_for_update` acquisitions.

    The Call row is already covered (a wait there is a wait on an acquisition).
    The uncovered case is a wait raised by the persist DML itself, AFTER the
    acquisitions have returned: `persist_section_extracts` issues
    `DELETE FROM call_section_extracts WHERE call_id = :id`, which must take a
    row lock on every cache row for that Call. A bound that covered only the
    acquisition success path would leave that DELETE outside it — and _m2 is
    holding the workspace row while it waits, which is exactly what pins the row
    against a concurrent DELETE.

    `_ROW_LOCK_TIMEOUT_SECONDS` is monkeypatched on the module (the helper reads
    it as a module global at call time, so the patch takes effect) purely to keep
    the test fast.
    """
    monkeypatch.setattr(setup_mod, "_ROW_LOCK_TIMEOUT_SECONDS", 1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = WorkspaceSetupJob(workspace_id=ws.id, status="running")
        s.add(job)
        call = s.get(Call, ws.call_id)
        # Pre-existing cache rows for this Call, stamped with an OLDER extractor
        # version so `section_extracts_are_fresh` is False and _m2 really reaches
        # persist_section_extracts (a fresh cache would skip the DELETE entirely).
        persist_section_extracts(
            session=s,
            call=call,
            sections={n: f"stale {n}" for n in SECTION_NAMES},
            model="old-model",
            extractor_version="v0",
            source_hash=source_text_hash(call.full_text or ""),
        )
        s.commit()
        job_id, ws_id, call_id = job.id, ws.id, call.id

    w = _worker(pg_session_factory, tmp_path, phase0_client)
    pinned = w._m1(job_id, ws_id)

    holding = threading.Event()
    release = threading.Event()

    def _hold_one_cache_row():
        with pg_session_factory() as s:
            s.scalars(
                select(CallSectionExtract)
                .where(CallSectionExtract.call_id == call_id)
                .limit(1)
                .with_for_update()
            ).one()
            holding.set()
            # Long enough that an UNBOUNDED wait shows up as a failure here
            # rather than as a hung test run.
            release.wait(timeout=20)
            s.rollback()

    t = threading.Thread(target=_hold_one_cache_row)
    t.start()
    try:
        assert holding.wait(timeout=10), "the holder never took the cache row"
        started = time.monotonic()
        with pytest.raises(setup_mod.SetupRowLockTimeout):
            w._m2(job_id, ws_id, pinned)
        # It gave up at its own timeout instead of holding the workspace row for
        # the whole lifetime of the unrelated cache-row holder.
        assert time.monotonic() - started < 10
    finally:
        release.set()
        t.join(timeout=30)


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


def test_m3_times_out_on_a_held_rubric_row_rather_than_pinning_the_workspace(
    pg_session_factory, current_user, tmp_path, phase0_client, monkeypatch
):
    """The _m3 analogue of
    test_m2_times_out_on_a_held_cache_row_rather_than_pinning_the_workspace:
    _bounded_lock_waits must bound the whole persist transaction here too, not
    just the `with_for_update` acquisitions.

    _m3 holds strictly MORE than _m2 while it waits — the workspace_setup_jobs
    row AND the workspaces row, both taken FOR UPDATE before any DML runs. The
    uncovered case is a wait raised AFTER those acquisitions returned:
    `persist_rubric` mutates the existing call_rubrics row, so the following
    `s.flush()` issues an UPDATE that must take that row's lock. Without the
    bound that UPDATE waits forever while _m3 pins both rows against a DELETE
    that has already won the commit mutex — and although Slice C3 caps that
    DELETE's own wait at 2s, it spends the cap and answers 429 having deleted
    nothing.

    Holding the CallRubric row rather than the Call row is deliberate: a wait on
    the Call row is a wait on an acquisition, which sits inside the bound either
    way and would stay green with `_bounded_lock_waits(s)` deleted.

    `_ROW_LOCK_TIMEOUT_SECONDS` is monkeypatched on the module (the helper reads
    it as a module global at call time, so the patch takes effect) purely to keep
    the test fast.
    """
    monkeypatch.setattr(setup_mod, "_ROW_LOCK_TIMEOUT_SECONDS", 1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = WorkspaceSetupJob(workspace_id=ws.id, status="running")
        s.add(job)
        call = s.get(Call, ws.call_id)
        # A pre-existing rubric row for this Call stamped with an OLDER extractor
        # version, for two reasons: `rubric_is_fresh` is False so _m3 really
        # reaches persist_rubric (a fresh rubric skips the DML entirely and the
        # test would prove nothing), and persist_rubric therefore takes its
        # UPDATE-an-existing-row branch — the row the holder below owns.
        persist_rubric(
            session=s,
            call=call,
            payload={
                "criteria": [
                    {
                        "name": "Stale",
                        "description": "from an older extractor",
                        "weight": 1.0,
                        "scale_max": 5,
                    }
                ],
                "funder_notes": "stale notes",
            },
            model="old-model",
            extractor_version="v0",
            source_hash=source_text_hash(call.full_text or ""),
        )
        s.commit()
        job_id, ws_id, call_id = job.id, ws.id, call.id

    w = _worker(pg_session_factory, tmp_path, phase0_client)
    pinned = w._m1(job_id, ws_id)

    holding = threading.Event()
    release = threading.Event()

    def _hold_the_rubric_row():
        with pg_session_factory() as s:
            s.scalars(
                select(CallRubric).where(CallRubric.call_id == call_id).with_for_update()
            ).one()
            holding.set()
            # Long enough that an UNBOUNDED wait shows up as a failure here
            # rather than as a hung test run.
            release.wait(timeout=20)
            s.rollback()

    t = threading.Thread(target=_hold_the_rubric_row)
    t.start()
    try:
        assert holding.wait(timeout=10), "the holder never took the rubric row"
        started = time.monotonic()
        with pytest.raises(setup_mod.SetupRowLockTimeout):
            w._m3(job_id, ws_id, pinned)
        # It gave up at its own timeout instead of holding the job and workspace
        # rows for the whole lifetime of the unrelated rubric-row holder.
        assert time.monotonic() - started < 10
        # And it actually LET GO: a third connection can take the workspace row
        # while the rubric-row holder is still sitting on its lock.
        with pg_session_factory() as probe:
            probe.scalars(
                select(Workspace).where(Workspace.id == ws_id).with_for_update(nowait=True)
            ).one()
            probe.rollback()
    finally:
        release.set()
        t.join(timeout=30)


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
    and hold no lock while the LLM call runs.

    The worker's outcome is CAPTURED and asserted rather than left to escape the
    thread. A bare `threading.Thread(target=lambda: w._m2(...))` lets the
    expected WorkspaceGoneDuringSetup surface only as a
    PytestUnhandledThreadExceptionWarning — and an UNEXPECTED worker failure
    (or a silent success, meaning the delete never landed) would be swallowed
    exactly the same way while this test stayed green.
    """
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

    outcome: list[BaseException | None] = []

    def _run_m2():
        try:
            w._m2(job_id, ws_id, pinned)
        except BaseException as exc:  # noqa: BLE001 — asserted after join, not silenced
            outcome.append(exc)
        else:
            outcome.append(None)

    t = threading.Thread(target=_run_m2)
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
        # lifecycle authority uses unique UUIDs; no cleanup needed

    # The worker must have observed the deletion and refused — exactly that, not
    # some other failure and not a silent success.
    assert not t.is_alive(), "the worker thread never finished"
    assert outcome, "the worker thread recorded no outcome"
    assert type(outcome[0]) is WorkspaceGoneDuringSetup, (
        f"expected _m2 to raise WorkspaceGoneDuringSetup once the workspace was "
        f"deleted, got {outcome[0]!r}"
    )


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
