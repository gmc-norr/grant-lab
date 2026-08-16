"""Slice C1 spike — claim-analysis retirement gates.

These exist to prove the TESTS discriminate, not merely that the code works.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from call_manager.db.models import ClaimAnalysisJob, Workspace
from tests.proposal_assist.conftest import make_call, make_workspace

_SHA = "0" * 40


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _job(ws_id, *, section_sha: str, status: str = "queued", attempts: int = 0):
    """section_index and commit_sha are NOT NULL with no server default."""
    return ClaimAnalysisJob(
        workspace_id=ws_id,
        section_index=0,
        section_sha=section_sha,
        commit_sha=_SHA,
        status=status,
        attempts=attempts,
    )


def test_the_claim_cancel_skips_a_row_a_worker_holds_without_waiting(
    pg_session_factory, current_user
):
    """The SKIP LOCKED property, asserted directly.

    The cancel runs in a THREAD with its own lock_timeout. Calling it inline while
    this test holds the conflicting row would, once skip_locked is removed, block
    forever BEFORE the finally that releases the holder — hanging the suite instead
    of failing. A mutation must fail, never hang.
    """
    from call_manager.proposal_assist.jobs_retirement import (
        cancel_claim_analysis_jobs_for_workspace,
    )

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        ws.soft_deleted_at = _now_utc()
        s.add(_job(ws.id, section_sha="a", status="running"))
        s.commit()
        ws_id = ws.id

    result: dict[str, object] = {}
    finished = threading.Event()

    def _cancel():
        try:
            began = time.monotonic()
            with pg_session_factory() as s:
                s.execute(text("SET LOCAL lock_timeout = '3s'"))
                result["n"] = cancel_claim_analysis_jobs_for_workspace(
                    s, ws_id, s.get(Workspace, ws_id).lifecycle_generation
                )
                s.commit()
            result["elapsed"] = time.monotonic() - began
        except BaseException as exc:  # noqa: BLE001 — asserted after release
            result["error"] = exc
        finally:
            finished.set()

    holder = pg_session_factory()
    t = threading.Thread(target=_cancel)
    try:
        holder.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id).with_for_update().one()
        t.start()
        completed_while_locked = finished.wait(timeout=10)
    finally:
        holder.rollback()  # ALWAYS released, whatever the cancel did
        holder.close()
        t.join(timeout=15)

    assert completed_while_locked, "the cancel never returned while the row was held"
    assert "error" not in result, f"the cancel raised instead of skipping: {result.get('error')!r}"
    assert result["n"] == 0, "the held row should have been SKIPPED, not cancelled"
    assert result["elapsed"] < 2.0, f"the cancel waited {result['elapsed']:.1f}s on a held row"


def test_the_claim_cancel_terminalizes_rows_nobody_holds(pg_session_factory, current_user):
    """The ordinary path — both non-terminal states go to 'failed'."""
    from call_manager.proposal_assist.jobs_retirement import (
        cancel_claim_analysis_jobs_for_workspace,
    )

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        ws.soft_deleted_at = _now_utc()
        queued = _job(ws.id, section_sha="a", status="queued")
        running = _job(ws.id, section_sha="b", status="running")
        s.add_all([queued, running])
        s.commit()
        queued_id, running_id, ws_id = queued.id, running.id, ws.id

    with pg_session_factory() as s:
        n = cancel_claim_analysis_jobs_for_workspace(
            s, ws_id, s.get(Workspace, ws_id).lifecycle_generation
        )
        s.commit()

    assert n == 2
    with pg_session_factory() as s:
        for jid in (queued_id, running_id):
            job = s.get(ClaimAnalysisJob, jid)
            assert job.status == "failed", f"job {jid} left in {job.status!r}"
            assert job.last_error == "cancelled: workspace deleted"


def test_mark_failed_does_not_requeue_a_job_whose_workspace_is_deleted(
    pg_session_factory, current_user
):
    """attempts=1 is deliberately well under MAX_ATTEMPTS so the requeue branch is
    the one that WOULD run. At the cap this would pass without the fix."""
    from call_manager.proposal_assist.analysis.queue import mark_failed

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        ws.soft_deleted_at = _now_utc()
        job = _job(ws.id, section_sha="a", status="running", attempts=1)
        s.add(job)
        s.commit()
        job_id = job.id

    with pg_session_factory() as s:
        mark_failed(s, s.get(ClaimAnalysisJob, job_id), error="workspace deleted mid-job")
        s.commit()

    with pg_session_factory() as s:
        assert s.get(ClaimAnalysisJob, job_id).status == "failed"


def test_a_delete_committing_mid_mark_failed_cannot_resurrect_the_job(
    pg_session_factory, current_user
):
    """The resurrection window, driven end-to-end.

    A TIMING assertion cannot see this, so the discriminating observation is the
    job's FINAL STATUS. What the lock changes is WHERE the requeue decision is
    taken relative to the delete's commit, not how long the call takes:

      * WITH the lock, the workspace read blocks until the delete commits and
        then, under READ COMMITTED, re-reads the newly committed row version —
        so the decision is taken with soft_deleted_at visible.
      * WITHOUT it, the read is an ordinary snapshot read: measured, it returns
        soft_deleted_at=None instantly, and the requeue decision is already made
        before the delete commits.

    The unfixed call is not fast overall — measured, it still spends ~1s parked,
    but at its job UPDATE (pg_locks shows a tuple lock on the workspaces row and
    a wait on the deleter's transaction id, i.e. the FK check's row lock on the
    parent), which is AFTER the decision. Wall-clock therefore looks similar in
    both directions while the status does not.

    Step by step:
      * the delete transaction stamps soft_deleted_at and holds the row,
      * mark_failed starts and (with the fix) blocks on it,
      * the delete COMMITS,
      * with the fix mark_failed's FOR UPDATE re-reads the new row version, sees
        the workspace deleted, and writes 'failed';
      * without it, mark_failed already read 'live' beforehand and writes 'queued'
        — the resurrection.

    Mutation: drop `.with_for_update()` and this must FAIL with status 'queued'.
    """
    from call_manager.proposal_assist.analysis.queue import mark_failed

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = _job(ws.id, section_sha="a", status="running", attempts=1)
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    finished = threading.Event()
    err: list[BaseException] = []

    def _mark():
        try:
            with pg_session_factory() as s:
                s.execute(text("SET LOCAL lock_timeout = '15s'"))
                mark_failed(s, s.get(ClaimAnalysisJob, job_id), error="boom")
                s.commit()
        except BaseException as exc:  # noqa: BLE001 — asserted after join
            err.append(exc)
        finally:
            finished.set()

    deleter = pg_session_factory()
    t = threading.Thread(target=_mark)
    try:
        # The delete transaction: stamp soft_deleted_at, hold, do NOT commit yet.
        deleter.get(Workspace, ws_id, with_for_update=True).soft_deleted_at = _now_utc()
        deleter.flush()
        t.start()
        time.sleep(1.0)          # mark_failed is now past its read (or blocked on it)
        deleter.commit()         # the delete lands
    finally:
        deleter.close()
        assert finished.wait(timeout=20), "mark_failed never finished"
        t.join(timeout=5)

    assert not err, f"mark_failed raised: {err!r}"
    with pg_session_factory() as s:
        status = s.get(ClaimAnalysisJob, job_id).status
    assert status == "failed", (
        f"job came back as {status!r} after its workspace was deleted mid-call — "
        "the workspace check is not a linearization point"
    )


def test_claim_skips_jobs_for_a_soft_deleted_workspace(pg_session_factory, current_user):
    """The durable half — survives restart, like the four pass claim queries."""
    from call_manager.proposal_assist.analysis.queue import claim_queued_jobs

    with pg_session_factory() as s:
        # Distinct external_ids: make_call defaults to EXT-1 and two workspaces
        # with_call=True in one session collide on uq_calls_source_external.
        live = make_workspace(
            s, owner=current_user, call_id=make_call(s, external_id="EXT-LIVE").id
        )
        dead = make_workspace(
            s, owner=current_user, call_id=make_call(s, external_id="EXT-DEAD").id
        )
        s.flush()
        dead.soft_deleted_at = _now_utc()
        s.add(_job(live.id, section_sha="a"))
        s.add(_job(dead.id, section_sha="b"))
        s.commit()
        live_id, dead_id = live.id, dead.id

    with pg_session_factory() as s:
        claimed = claim_queued_jobs(s, limit=10)
        s.commit()
        ids = {j.workspace_id for j in claimed}

    assert live_id in ids
    assert dead_id not in ids


def test_reconcile_terminalizes_a_claim_row_on_a_workspace_with_no_setup_jobs(
    pg_session_factory, current_user
):
    """The early-return bug: this workspace has NO setup jobs, so a reconcile that
    returns early when the setup query finds nothing never reaches claim-analysis.

    Full path: a running job holds its row -> the hook's SKIP LOCKED cancel skips it
    -> the worker's session ends WITHOUT terminalizing it (a crash, so the
    cooperative checkpoint cannot be what saves us) -> a later reconcile marks it
    failed.
    """
    from call_manager.proposal_assist.jobs_retirement import (
        build_jobs_retirement_hook,
        reconcile_cancelled_jobs,
    )

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        ws.soft_deleted_at = _now_utc()
        ws.lifecycle_generation = 1
        job = _job(ws.id, section_sha="a", status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    holder = pg_session_factory()
    try:
        holder.query(ClaimAnalysisJob).filter_by(id=job_id).with_for_update().one()
        asyncio.run(build_jobs_retirement_hook(pg_session_factory)(ws_id, 1))
        with pg_session_factory() as s:
            assert s.get(ClaimAnalysisJob, job_id).status == "running", (
                "the hook should have SKIPPED the locked row, not waited on it"
            )
    finally:
        holder.rollback()  # the 'crash' — no terminal write from the worker
        holder.close()

    with pg_session_factory() as s:
        reconcile_cancelled_jobs(s)
        s.commit()

    with pg_session_factory() as s:
        assert s.get(ClaimAnalysisJob, job_id).status == "failed"


def test_the_abort_callback_is_checked_PER_ROW_not_once_per_job(pg_session_factory):
    """One check per job is NOT 'bounded by one LLM call': the audit judges each
    evidence row, so a 40-row job would spend 40 calls after the delete.

    The check sits at the TOP of the per-row loop, before the row-shape filters, so
    it fires for every row regardless of judgeability. should_abort here returns
    False twice then True: with the check inside the loop it raises on the third
    row; with it hoisted out of the loop it would be consulted ONCE, return False,
    and never raise.

    Mutation: move the check above `for row in evidence:` and this fails.
    """
    from call_manager.proposal_assist.citations.audit import (
        ClaimAnalysisAborted,
        validate_citations_semantic,
    )

    calls = {"n": 0}

    def _should_abort() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    ledger = {"evidence": [{"id": f"ev{i}"} for i in range(5)]}

    with pytest.raises(ClaimAnalysisAborted):
        validate_citations_semantic(
            proposal_text="",
            ledger=ledger,
            session=None,
            session_factory=pg_session_factory,
            workspace_id=uuid.uuid4(),
            citation_service=None,
            deterministic_issues=[],
            should_abort=_should_abort,
        )

    assert calls["n"] == 3, (
        f"should_abort was consulted {calls['n']} time(s) — it is not inside the "
        "per-row loop"
    )


def test_an_aborted_job_is_terminalized_and_never_marked_done(
    pg_session_factory, current_user, monkeypatch
):
    """The bug a `break` would have shipped: partial results, job marked done.

    Drives the REAL drain loop with analyze_job raising the abort, so the handler
    under test is production's.

    Mutation: delete the `except ClaimAnalysisAborted` branch — the generic handler
    then routes through mark_failed and (attempts < MAX) requeues it to 'queued'.
    """
    from call_manager.proposal_assist.analysis import pipeline as pl
    from call_manager.proposal_assist.citations.audit import ClaimAnalysisAborted

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = _job(ws.id, section_sha="a", status="queued", attempts=0)
        s.add(job)
        s.commit()
        job_id = job.id

    def _boom(*args, **kwargs):
        raise ClaimAnalysisAborted("workspace deleted mid-audit")

    monkeypatch.setattr(pl, "analyze_job", _boom)

    counts = pl.drain_analysis_queue(
        session_factory=pg_session_factory,
        citation_service=None,
        embedder=None,
        workspaces_dir="/tmp",
    )

    assert counts["failed"] == 1
    with pg_session_factory() as s:
        job = s.get(ClaimAnalysisJob, job_id)
        assert job.status == "failed", f"aborted job ended as {job.status!r}"
        assert job.last_error == "cancelled: workspace deleted"


def test_the_delete_route_returns_within_the_barrier_with_a_worker_mid_job(
    client_authed_owner, pg_session_factory, current_user
):
    """A REGRESSION GUARD on end-to-end route latency, measured as wall-clock on the
    route rather than 'the delete transaction committed'.

    NOTE: this does NOT prove SKIP LOCKED. The hook's own 1.0s wait_for and the
    coordinator's per-hook exception isolation keep the route fast even with a
    blocking cancel — that property is proven directly in
    test_the_claim_cancel_skips_a_row_a_worker_holds_without_waiting.
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        s.add(_job(ws.id, section_sha="a", status="running"))
        s.commit()
        ws_id = ws.id

    holder = pg_session_factory()
    try:
        holder.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id).with_for_update().one()
        began = time.monotonic()
        resp = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
        elapsed = time.monotonic() - began
        assert resp.status_code == 204
        assert elapsed < 6.0, (
            f"DELETE took {elapsed:.1f}s with a claim-analysis worker holding its row"
        )
    finally:
        holder.rollback()
        holder.close()


def test_the_abort_fires_in_the_UNCITED_loop_for_a_section_with_no_ev_markers(
    pg_session_factory, current_user, tmp_path
):
    """The gap the audit-loop callback does NOT cover.

    `should_abort` reaches `validate_citations_semantic` only under
    `if cited_evs:` — so a section with zero `ev:` markers consulted it ZERO
    times, and the uncited classify/retrieve/judge loop (one classify plus a
    retrieve-and-judge fan-out per sentence) ran to completion after the delete
    committed. This drives the REAL `analyze_job` over a real repo whose section
    has no markers at all.

    Shaped like test_the_abort_callback_is_checked_PER_ROW_not_once_per_job: the
    callback returns False twice then True, so a check at the TOP of
    `for occ in sentences:` raises on the third sentence (n == 3) and the third
    sentence never reaches the LLM (2 classify calls, not 3). Hoisted above the
    loop it would be consulted ONCE, return False, and never raise.

    Mutation: move the check above `for occ in sentences:` (pipeline.py:418) —
    n == 1, no exception, job runs to completion.
    """
    from call_manager.proposal_assist.analysis.pipeline import analyze_job
    from call_manager.proposal_assist.analysis.queue import claim_queued_jobs
    from call_manager.proposal_assist.citations.audit import ClaimAnalysisAborted
    from call_manager.proposal_assist.references.embed import FakeEmbedder
    from tests.test_analysis_pipeline import (
        FakeCitationService,
        _enqueue_section,
        _init_repo,
    )

    doc = (
        "# T\nintro\n## Body\n"
        "First factual claim here. Second factual claim here. Third factual claim here.\n"
    )
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        repo = _init_repo(tmp_path, ws.id, doc)
        _enqueue_section(s, ws.id, repo, 1)
        s.commit()

    calls = {"n": 0}

    def _should_abort() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    svc = FakeCitationService()
    with pg_session_factory() as s:
        (job,) = claim_queued_jobs(s, limit=1)
        s.commit()
        with pytest.raises(ClaimAnalysisAborted):
            analyze_job(
                job,
                session=s,
                session_factory=pg_session_factory,
                citation_service=svc,
                embedder=FakeEmbedder(),
                workspaces_dir=tmp_path,
                should_abort=_should_abort,
            )
        s.rollback()

    assert calls["n"] == 3, (
        f"should_abort was consulted {calls['n']} time(s) for a 3-sentence section "
        "with no ev: markers — it is not inside the uncited per-sentence loop"
    )
    assert svc.calls.count("user_claim_classify") == 2, (
        f"{svc.calls.count('user_claim_classify')} classify calls — the aborting "
        "sentence still spent tokens, so the check is not at the TOP of the loop"
    )


def test_mark_done_cannot_resurrect_a_job_the_retirement_hook_terminalized(
    pg_session_factory, current_user
):
    """The lost update on the SUCCESS path.

    The hook's cancel SKIP LOCKEDs past rows a worker holds — but a worker whose
    per-job session holds no lock on its job row (every sentence a cache hit, so
    `touch_running_job` never ran) does not shield it. The hook then wins the row
    and writes status='failed' / last_error='cancelled: workspace deleted', and an
    unconditional `mark_done` flushes 'done' back over it while leaving the
    cancellation message behind: a resurrected job in an incoherent state.

    Uses the REAL hook, not a hand-written UPDATE, so the terminal write under
    test is production's.

    Mutation: drop the FOR UPDATE re-read + status guard from
    `analysis/queue.py:mark_done` — the row ends 'done' with last_error=None.
    """
    from call_manager.proposal_assist.analysis.queue import mark_done
    from call_manager.proposal_assist.jobs_retirement import build_jobs_retirement_hook

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        ws.lifecycle_generation = 1
        s.flush()
        job = _job(ws.id, section_sha="a", status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    # The worker's per-job session: the job is loaded, but NO row lock is held.
    with pg_session_factory() as worker:
        job = worker.get(ClaimAnalysisJob, job_id)
        assert job.status == "running"

        with pg_session_factory() as s:
            s.get(Workspace, ws_id).soft_deleted_at = _now_utc()
            s.commit()
        assert asyncio.run(build_jobs_retirement_hook(pg_session_factory)(ws_id, 1)) == 1

        mark_done(worker, job)
        worker.commit()

    with pg_session_factory() as s:
        row = s.get(ClaimAnalysisJob, job_id)
        assert row.status == "failed", f"mark_done resurrected a cancelled job as {row.status!r}"
        assert row.last_error == "cancelled: workspace deleted", (
            f"cancellation message lost: {row.last_error!r}"
        )


def test_mark_done_gives_up_on_the_job_row_instead_of_waiting_unboundedly(
    pg_session_factory, current_user, monkeypatch
):
    """The re-read above is a BLOCKING lock wait in an APScheduler worker thread,
    and PostgreSQL's lock_timeout is disabled by default — so without a bound one
    stuck holder of the job row parks that thread for as long as it holds it.

    Runs mark_done in a THREAD, for the reason
    test_the_claim_cancel_skips_a_row_a_worker_holds_without_waiting gives: an
    unbounded wait would otherwise hang the suite from inside the `with` that
    holds the row, and a mutation must FAIL, never hang.

    Mutation: drop the `bounded_lock_waits(...)` wrapper from
    `analysis/queue.py:mark_done` — the thread is still blocked when the join
    times out.
    """
    from call_manager.proposal_assist.analysis import queue as q
    from call_manager.proposal_assist.row_locks import RowLockTimeout

    monkeypatch.setattr(q, "MARK_DONE_LOCK_TIMEOUT_SECONDS", 0.25)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = _job(ws.id, section_sha="a", status="running")
        s.add(job)
        s.commit()
        job_id = job.id

    result: dict[str, object] = {}

    def _finalize():
        try:
            with pg_session_factory() as worker:
                q.mark_done(worker, worker.get(ClaimAnalysisJob, job_id))
                worker.commit()
            result["outcome"] = "committed"
        except BaseException as exc:  # noqa: BLE001 — asserted after release
            result["outcome"] = exc

    holder = pg_session_factory()
    t = threading.Thread(target=_finalize)
    try:
        holder.query(ClaimAnalysisJob).filter_by(id=job_id).with_for_update().one()
        began = time.monotonic()
        t.start()
        t.join(timeout=10)
        still_waiting = t.is_alive()
        elapsed = time.monotonic() - began
    finally:
        holder.rollback()  # ALWAYS released, whatever mark_done did
        holder.close()
        t.join(timeout=15)

    assert not still_waiting, (
        "mark_done was still waiting on the held job row after 10s — the FOR "
        "UPDATE re-read is unbounded"
    )
    assert isinstance(result["outcome"], RowLockTimeout), (
        f"expected a typed RowLockTimeout, got {result['outcome']!r}"
    )
    assert elapsed < 5.0, f"mark_done waited {elapsed:.1f}s on a held row"

    with pg_session_factory() as s:
        assert s.get(ClaimAnalysisJob, job_id).status == "running", (
            "the job must be left non-terminal for the caller to requeue"
        )


def test_a_job_the_hook_terminalized_is_COUNTED_by_the_drain_not_silently_dropped(
    pg_session_factory, current_user, monkeypatch
):
    """mark_done's guard makes the job row terminal-but-not-'done'; the tick's
    accounting must say so.

    Left stale, the caller's in-memory `job.status` still reads 'running' after
    mark_done declines, so the job lands in NO bucket — done, superseded and
    failed all stay 0 — while the session's annotation writes commit anyway. A
    tick that cancelled work reports a completely empty result, and
    `if any(out.values())` in the scheduler means it does not even log.

    Mutations (either must fail this test):
      * drop `session.expire(job, ["status"])` from
        `analysis/queue.py:mark_done` — counts == {done: 0, failed: 0,
        superseded: 0};
      * drop the `elif status == "failed"` arm from drain_analysis_queue — same.
    """
    from call_manager.proposal_assist.analysis import pipeline as pl
    from call_manager.proposal_assist.analysis.queue import mark_done
    from call_manager.proposal_assist.jobs_retirement import build_jobs_retirement_hook

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        ws.lifecycle_generation = 1
        s.flush()
        s.add(_job(ws.id, section_sha="a", status="queued"))
        s.commit()
        ws_id = ws.id

    def _analyze(job, *, session, **kwargs):
        """Stands in for a real pass whose workspace is deleted while it runs.

        The delete and the cancel are production's — the REAL hook, as in
        test_mark_done_cannot_resurrect_a_job_the_retirement_hook_terminalized —
        and the tail is the real mark_done. The worker session holds no lock on
        the job row here (no heartbeat ran), which is exactly the case the hook's
        SKIP LOCKED does not shield.
        """
        with pg_session_factory() as s2:
            s2.get(Workspace, ws_id).soft_deleted_at = _now_utc()
            s2.commit()
        assert asyncio.run(build_jobs_retirement_hook(pg_session_factory)(ws_id, 1)) == 1
        mark_done(session, job)

    monkeypatch.setattr(pl, "analyze_job", _analyze)

    counts = pl.drain_analysis_queue(
        session_factory=pg_session_factory,
        citation_service=None,
        embedder=None,
        workspaces_dir="/tmp",
    )

    assert counts == {"done": 0, "failed": 1, "superseded": 0}, (
        f"a cancelled job was dropped from the tick's accounting: {counts}"
    )
    with pg_session_factory() as s:
        row = s.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id).one()
        assert row.status == "failed"
        assert row.last_error == "cancelled: workspace deleted"


def test_the_abort_callback_is_actually_WIRED_INTO_the_cited_audit_call_site(
    pg_session_factory, current_user, tmp_path
):
    """The threading itself, not just the loop it lands in.

    `validate_citations_semantic`'s per-row check is pinned above — but by calling
    that function DIRECTLY. Deleting `should_abort=should_abort` from analyze_job's
    call site (pipeline.py:385) leaves every other test in this file green
    (measured: 11 passed under exactly that mutation), because nothing else drives
    the cited branch with a callback. This drives the real `analyze_job` over a
    section that HAS an `ev:` marker.

    Counting is not enough to discriminate: the uncited loop now checks per
    sentence too, so an unwired audit still raises — one loop later. So this
    asserts the raise came out of `citations/audit.py`, which only the wired call
    site can produce.

    Mutation: delete `should_abort=should_abort` at pipeline.py:385 — the abort
    then surfaces from pipeline.py's uncited loop and no audit.py frame appears.
    """
    from call_manager.proposal_assist.analysis.pipeline import analyze_job
    from call_manager.proposal_assist.analysis.queue import claim_queued_jobs
    from call_manager.proposal_assist.citations.audit import ClaimAnalysisAborted
    from call_manager.proposal_assist.references.embed import FakeEmbedder
    from tests.test_analysis_pipeline import (
        FakeCitationService,
        _enqueue_section,
        _init_repo,
    )

    ev = "ev-1111aaaa2222"
    doc = f"# T\nintro\n## S\nCited sentence here[@k]<!-- ev:{ev} -->.\n"
    citations = (
        f'{{"version": 2, "evidence": [{{"id": "{ev}", "citekey": "k", "stance": "supports"}}]}}'
    )

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        repo = _init_repo(tmp_path, ws.id, doc, citations_json=citations)
        _enqueue_section(s, ws.id, repo, 1)
        s.commit()

    calls = {"n": 0}

    def _should_abort() -> bool:
        calls["n"] += 1
        return True

    with pg_session_factory() as s:
        (job,) = claim_queued_jobs(s, limit=1)
        s.commit()
        with pytest.raises(ClaimAnalysisAborted) as excinfo:
            analyze_job(
                job,
                session=s,
                session_factory=pg_session_factory,
                citation_service=FakeCitationService(),
                embedder=FakeEmbedder(),
                workspaces_dir=tmp_path,
                should_abort=_should_abort,
            )
        s.rollback()

    frames = [str(entry.path) for entry in excinfo.traceback]
    assert any(f.endswith("citations/audit.py") for f in frames), (
        "the abort did not come from the semantic audit — should_abort is not "
        f"threaded into the validate_citations_semantic call. Frames: {frames}"
    )
    assert calls["n"] == 1
