"""Slice C1 spike — pass prefix guards and retirement, against the REAL runners."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import UTC, datetime

import pytest
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


def _seed_review(pg_session_factory, current_user, *, item_status="queued"):
    from call_manager.db.models import ReviewRun, ReviewRunItem

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        run = ReviewRun(workspace_id=ws.id, status="running", draft_sha="0" * 40)
        s.add(run)
        s.flush()
        s.add(ReviewRunItem(review_run_id=run.id, kind="fit_check", status=item_status))
        s.commit()
        return run.id, ws.id


def test_the_child_prefix_blocks_on_the_run_row_instead_of_committing_running(
    pg_session_factory, current_user
):
    """Gate 6b — assert WHILE the interference is in force.

    Asserting AFTER releasing the run lock proves nothing: finalization then sees
    the cancelled run and moves the item off 'running' anyway, so it passes with or
    without the prefix lock. The discriminating observation is made while the
    holder still owns the run row: WITH the fix the child is blocked in its prefix
    and the item is still 'queued'; WITHOUT it the child commits 'running'.
    """
    from call_manager.db.models import ReviewRunItem
    from call_manager.proposal_assist.review.pass_runner import _run_one_call

    run_id, ws_id = _seed_review(pg_session_factory, current_user)

    from call_manager.db.models import ReviewRun

    child_done = threading.Event()

    def _child():
        try:
            asyncio.run(
                _run_one_call(
                    kind="fit_check",
                    instruction="x",
                    run_id=run_id,
                    workspace_id=ws_id,
                    session_factory=pg_session_factory,
                    settings=Settings(),
                    adapter_builder=_stub_adapter_builder,
                )
            )
        finally:
            child_done.set()

    holder = pg_session_factory()
    observed: list[str] = []
    t = threading.Thread(target=_child)
    try:
        holder.get(ReviewRun, run_id, with_for_update=True)
        t.start()
        time.sleep(1.5)  # ample time for an UNGUARDED child to commit 'running'
        with pg_session_factory() as probe:
            observed.append(
                probe.scalar(
                    select(ReviewRunItem.status).where(
                        ReviewRunItem.review_run_id == run_id
                    )
                )
            )
    finally:
        holder.rollback()
        holder.close()
        assert child_done.wait(timeout=30), "the child never finished"
        t.join(timeout=5)

    assert observed == ["queued"], (
        f"while the run row was held the item was {observed!r} — the child "
        "committed without waiting for the run lock"
    )


def _pausing_adapter_builder(gate: threading.Event, entered: threading.Event):
    """Adapter whose turn parks until `gate` is set, so the child can be held
    BETWEEN its prefix and its finalization."""

    def _build(**kwargs):
        class _Stub:
            async def launch(self, context):
                handle = type("H", (), {})()
                handle.state = {"launch_id": uuid.uuid4()}
                handle.workspace_id = kwargs.get("workspace_id")
                return handle

            async def send_turn(self, handle, instruction):
                entered.set()
                await asyncio.get_running_loop().run_in_executor(None, gate.wait, 30)
                return
                yield  # pragma: no cover

            async def terminate(self, handle) -> None:
                return None

            def unconfirmed_launch_ids(self):
                return []

            async def retire_unconfirmed(self) -> int:
                return 0

        return _Stub()

    return _build


def test_finalization_takes_the_run_row_before_the_item_row(
    pg_session_factory, current_user
):
    """The normative order, proven by ACQUISITION rather than by inducing a
    deadlock.

    The child is parked inside its turn (after the prefix, before finalization) so
    the ITEM row can be taken first. Finalization must then already HOLD the run row
    while it blocks on the item — probed with FOR UPDATE NOWAIT from a third
    connection. Under the old item-then-run order it would be blocked on the item
    holding nothing, and the probe would succeed.

    Mutation: restore finalization to item-then-run and this must FAIL.
    """
    from call_manager.db.models import ReviewRun, ReviewRunItem
    from call_manager.proposal_assist.review.pass_runner import _run_one_call

    run_id, ws_id = _seed_review(pg_session_factory, current_user, item_status="queued")
    with pg_session_factory() as s:
        item_id = s.scalar(
            select(ReviewRunItem.id).where(ReviewRunItem.review_run_id == run_id)
        )

    def _run_row_is_locked() -> bool:
        try:
            with pg_session_factory() as probe:
                probe.execute(
                    select(ReviewRun).where(ReviewRun.id == run_id).with_for_update(nowait=True)
                )
                return False
        except OperationalError:
            return True

    gate = threading.Event()
    in_turn = threading.Event()
    child_done = threading.Event()

    def _child():
        try:
            asyncio.run(
                _run_one_call(
                    kind="fit_check",
                    instruction="x",
                    run_id=run_id,
                    workspace_id=ws_id,
                    session_factory=pg_session_factory,
                    settings=Settings(),
                    adapter_builder=_pausing_adapter_builder(gate, in_turn),
                )
            )
        finally:
            child_done.set()

    holder = pg_session_factory()
    locked_while_blocked: list[bool] = []
    t = threading.Thread(target=_child)
    try:
        t.start()
        assert in_turn.wait(timeout=15), "the child never reached its turn"
        # The prefix has committed and released its run lock; take the ITEM so only
        # FINALIZATION contends.
        holder.get(ReviewRunItem, item_id, with_for_update=True)
        gate.set()                      # child proceeds into finalization
        time.sleep(1.5)                 # it is now blocked on the item row
        locked_while_blocked.append(_run_row_is_locked())
    finally:
        gate.set()
        holder.rollback()
        holder.close()
        assert child_done.wait(timeout=30), "the child never finished"
        t.join(timeout=5)

    assert locked_while_blocked == [True], (
        "finalization was blocked on the item row without holding the run row — "
        "that is the item-then-run order, which inverts against the prefix and "
        "the delete path"
    )


@pytest.mark.asyncio
async def test_every_review_child_is_tracked_as_its_own_entry(
    pg_session_factory, current_user
):
    """Gate 6 — against the REAL _run_one_call.

    An earlier version of this test used hand-rolled coroutines, so deleting the
    tracking from production review could not have failed it. The peak live count
    is sampled from inside the real adapter build, which only happens per child.

    Mutation: delete the child's `async with PASS_REGISTRY.track(...)` and this
    must FAIL.
    """
    from call_manager.proposal_assist.passes import PASS_REGISTRY
    from call_manager.proposal_assist.review.pass_runner import _run_one_call

    run_id, ws_id = _seed_review(pg_session_factory, current_user)
    peak: list[int] = []

    def _sampling_builder(**kwargs):
        peak.append(PASS_REGISTRY.live_count(ws_id))
        return _stub_adapter_builder(**kwargs)

    # Two children on the same workspace, concurrently.
    async with PASS_REGISTRY.track(ws_id):          # stands in for the parent
        await asyncio.gather(
            _run_one_call(
                kind="fit_check",
                instruction="x",
                run_id=run_id,
                workspace_id=ws_id,
                session_factory=pg_session_factory,
                settings=Settings(),
                adapter_builder=_sampling_builder,
            ),
        )

    assert peak, "the adapter was never built — the child did not run"
    assert max(peak) >= 2, (
        f"peak live entries was {max(peak)} — the child is not registering its own "
        "entry (it would be 1: only the parent)"
    )
    assert PASS_REGISTRY.live_count(ws_id) == 0


def test_the_evaluator_prefix_writes_nothing_for_a_deleted_workspace(
    pg_session_factory, current_user
):
    """Unlike coherence, this prefix COMMITS progress — so a missing guard is a
    durable write against a deleted workspace, not just wasted work."""
    from call_manager.db.models import EvaluatorJob
    from call_manager.proposal_assist.evaluator.pass_runner import run_evaluator_pass

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = EvaluatorJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = _now_utc()
        s.commit()

    asyncio.run(
        run_evaluator_pass(
            job_id=job_id,
            session_factory=pg_session_factory,
            settings=Settings(),
            adapter_builder=_never_called_adapter_builder,
        )
    )

    with pg_session_factory() as s:
        job = s.get(EvaluatorJob, job_id)
        assert job.evaluated_draft_sha is None
        assert job.evaluated_spec_sha is None
        assert job.evaluated_rubric_updated_at is None


def test_the_uncited_prefix_writes_nothing_for_a_deleted_workspace(
    pg_session_factory, current_user
):
    """This prefix commits evaluated_draft_sha."""
    from call_manager.db.models import UncitedClaimsJob
    from call_manager.proposal_assist.uncited_claims.pass_runner import (
        run_uncited_claims_pass,
    )

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = UncitedClaimsJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = _now_utc()
        s.commit()

    asyncio.run(
        run_uncited_claims_pass(
            job_id=job_id,
            session_factory=pg_session_factory,
            settings=Settings(),
            adapter_builder=_never_called_adapter_builder,
        )
    )

    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).evaluated_draft_sha is None
