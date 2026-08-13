"""Slice C1 spike — claim-analysis retirement gates.

These exist to prove the TESTS discriminate, not merely that the code works.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from sqlalchemy import text

from call_manager.db.models import ClaimAnalysisJob, Workspace
from tests.proposal_assist.conftest import make_workspace

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


def test_mark_faileds_workspace_check_is_a_real_linearization_point(
    pg_session_factory, current_user
):
    """The property the pre-set test above CANNOT prove: that the check holds the
    workspace row while it decides.

    Hold the workspace row from another connection, then run mark_failed in a
    thread. WITH the FOR UPDATE it blocks (a delete in flight cannot be missed);
    WITHOUT it, it reads straight through and commits 'queued' — exactly the
    resurrection window.

    Threaded + lock_timeout so the mutation FAILS rather than hanging.
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
                s.execute(text("SET LOCAL lock_timeout = '4s'"))
                mark_failed(s, s.get(ClaimAnalysisJob, job_id), error="boom")
                s.commit()
        except BaseException as exc:  # noqa: BLE001
            err.append(exc)
        finally:
            finished.set()

    holder = pg_session_factory()
    t = threading.Thread(target=_mark)
    try:
        holder.get(Workspace, ws_id, with_for_update=True)
        t.start()
        completed_while_workspace_held = finished.wait(timeout=2.0)
    finally:
        holder.rollback()
        holder.close()
        t.join(timeout=15)

    assert not completed_while_workspace_held, (
        "mark_failed decided the job's fate without holding the workspace row — "
        "a DELETE committing right after its read would be missed"
    )
    assert not err, f"mark_failed raised: {err!r}"
