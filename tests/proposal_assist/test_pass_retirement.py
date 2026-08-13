"""Slice C1 spike — pass prefix guards and retirement, against the REAL runners."""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from call_manager.config import Settings
from call_manager.db.models import CoherenceJob, Workspace
from call_manager.proposal_assist.coherence.pass_runner import run_coherence_pass
from tests.proposal_assist.conftest import make_workspace


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _never_called_adapter_builder(**kwargs):
    """This IS the assertion for prefix-guard tests: reached only if the guard
    failed to abort."""
    raise AssertionError("the prefix guard should have aborted before the adapter build")


def _stub_adapter_builder(**kwargs):
    """Proceeds past the prefix without doing any LLM work."""

    class _Stub:
        async def launch(self, context):
            handle = type("H", (), {})()
            handle.state = {"launch_id": uuid.uuid4()}
            handle.workspace_id = kwargs.get("workspace_id")
            return handle

        async def send_turn(self, handle, instruction):
            return
            yield  # pragma: no cover — makes this an async generator

        async def terminate(self, handle) -> None:
            return None

        def unconfirmed_launch_ids(self):
            return []

        async def retire_unconfirmed(self) -> int:
            return 0

    return _Stub()


def _seed(pg_session_factory, current_user):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = CoherenceJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        return job.id, ws.id


def test_the_coherence_prefix_aborts_for_a_workspace_deleted_under_it(
    pg_session_factory, current_user
):
    """Gate 9: zero prefix writes, and the adapter is never built."""
    job_id, ws_id = _seed(pg_session_factory, current_user)

    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = _now_utc()
        s.commit()

    asyncio.run(
        run_coherence_pass(
            job_id=job_id,
            session_factory=pg_session_factory,
            settings=Settings(),
            adapter_builder=_never_called_adapter_builder,
        )
    )

    with pg_session_factory() as s:
        assert s.get(CoherenceJob, job_id).status == "running"


def test_the_coherence_prefix_holds_the_workspace_row_then_releases_it(
    pg_session_factory, current_user, monkeypatch
):
    """Gate 9, second half — TWO properties, both asserted while the interference
    is in force:

      1. the prefix genuinely HOLDS the workspace row, and
      2. it RELEASES it before the pass does any cloud work.

    A sleep-then-measure version proves neither: it passes with the guard removed,
    and it also passes if the whole (fast, stubbed) pass runs under the lock. So the
    prefix is paused deterministically inside context assembly and the row is probed
    with FOR UPDATE NOWAIT at two chosen moments.
    """
    from call_manager.proposal_assist.coherence import pass_runner as cp

    job_id, ws_id = _seed(pg_session_factory, current_user)

    in_prefix = threading.Event()
    may_finish_prefix = threading.Event()
    real_assemble = cp.assemble_coherence_context

    def _paused_assemble(**kwargs):
        in_prefix.set()
        may_finish_prefix.wait(timeout=30)
        return real_assemble(**kwargs)

    monkeypatch.setattr(cp, "assemble_coherence_context", _paused_assemble)

    def _row_is_locked() -> bool:
        """Probe from a separate connection: True == someone holds it."""
        try:
            with pg_session_factory() as probe:
                probe.execute(
                    select(Workspace).where(Workspace.id == ws_id).with_for_update(nowait=True)
                )
                return False
        except OperationalError:
            return True

    done = threading.Event()

    def _run_pass():
        try:
            asyncio.run(
                run_coherence_pass(
                    job_id=job_id,
                    session_factory=pg_session_factory,
                    settings=Settings(),
                    adapter_builder=_stub_adapter_builder,
                )
            )
        finally:
            done.set()

    t = threading.Thread(target=_run_pass)
    t.start()
    try:
        assert in_prefix.wait(timeout=15), "the prefix never ran"
        # (1) asserted WHILE the prefix is paused — the lock must be held now.
        assert _row_is_locked(), (
            "the prefix does not hold the workspace row — the guard cannot "
            "linearize against a delete without it"
        )
    finally:
        may_finish_prefix.set()

    assert done.wait(timeout=30), "the pass never finished"
    t.join(timeout=5)
    # (2) the lock is gone once the prefix's session has committed.
    assert not _row_is_locked(), "the prefix never released the workspace row"


def test_deleting_a_workspace_leaves_no_review_item_in_running(
    client_authed_owner, pg_session_factory, current_user
):
    """The case the item-terminalization exists for: a child ALREADY in 'running'
    when the DELETE lands.

    Retirement cancels the child's task, so its finalization session never runs —
    CancelledError propagates past the `finally`, and the block after the `try` is
    simply not executed. Only the delete transaction can terminalize the item.

    Mutation: revert cancel_review_runs_and_items_for_workspace to updating runs
    only, and this must FAIL with the item still 'running'.
    """
    from call_manager.db.models import ReviewRun, ReviewRunItem

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        run = ReviewRun(workspace_id=ws.id, status="running", draft_sha="0" * 40)
        s.add(run)
        s.flush()
        s.add(ReviewRunItem(review_run_id=run.id, kind="fit_check", status="running"))
        s.commit()
        run_id, ws_id = run.id, ws.id

    resp = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
    assert resp.status_code == 204

    with pg_session_factory() as s:
        item = s.scalar(select(ReviewRunItem).where(ReviewRunItem.review_run_id == run_id))
        assert item.status == "cancelled", (
            f"item left in {item.status!r} on a deleted workspace — a cancelled "
            "child never reaches its own finalization"
        )
        assert s.get(ReviewRun, run_id).status == "cancelled"
