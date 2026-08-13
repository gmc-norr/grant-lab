"""Slice C1 spike — claim-analysis retirement gates.

These exist to prove the TESTS discriminate, not merely that the code works.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
import time
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
                result["n"] = cancel_claim_analysis_jobs_for_workspace(s, ws_id)
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
        n = cancel_claim_analysis_jobs_for_workspace(s, ws_id)
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

    A TIMING assertion cannot see this. Measured directly, mark_failed takes ~2s
    either way while the workspace row is held: WITH the lock it blocks at its
    SELECT FOR UPDATE, WITHOUT it blocks at COMMIT instead (the job UPDATE needs a
    key-share lock on its FK parent). Same wall-clock, opposite correctness.

    The discriminating observation is the job's FINAL STATUS:
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
        job = _job(ws.id, section_sha="a", status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    holder = pg_session_factory()
    try:
        holder.query(ClaimAnalysisJob).filter_by(id=job_id).with_for_update().one()
        asyncio.run(build_jobs_retirement_hook(pg_session_factory)(ws_id))
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
