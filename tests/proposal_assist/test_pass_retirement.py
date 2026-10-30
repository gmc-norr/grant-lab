"""Slice C1 spike — pass prefix guards and retirement, against the REAL runners."""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from grant_lab.config import Settings
from grant_lab.db.models import CoherenceJob, Workspace
from grant_lab.proposal_assist.coherence.pass_runner import run_coherence_pass
from grant_lab.proposal_assist.paths import workspace_repo_dir
from grant_lab.proposal_assist.review.pass_runner import run_review_pass
from grant_lab.workspaces import soft_delete_workspace_db
from tests.proposal_assist.conftest import make_workspace, review_stamp


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
            claimed_generation=0,
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
    from grant_lab.proposal_assist.coherence import pass_runner as cp

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
                    claimed_generation=0,
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
    from grant_lab.db.models import ReviewRun, ReviewRunItem

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
    from grant_lab.db.models import ReviewRun, ReviewRunItem

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
    from grant_lab.db.models import ReviewRunItem
    from grant_lab.proposal_assist.review.pass_runner import _run_one_call

    run_id, ws_id = _seed_review(pg_session_factory, current_user)

    from grant_lab.db.models import ReviewRun

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
                    lifecycle_generation=0,
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
    from grant_lab.db.models import ReviewRun, ReviewRunItem
    from grant_lab.proposal_assist.review.pass_runner import _run_one_call

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
                    lifecycle_generation=0,
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
    from grant_lab.proposal_assist.passes import PASS_REGISTRY
    from grant_lab.proposal_assist.review.pass_runner import _run_one_call

    run_id, ws_id = _seed_review(pg_session_factory, current_user)
    peak: list[int] = []

    def _sampling_builder(**kwargs):
        peak.append(PASS_REGISTRY.live_count(ws_id))
        return _stub_adapter_builder(**kwargs)

    # One child, running under a stand-in for the parent entry: what is asserted is
    # that the child has an entry of its OWN, so the peak must be 2 and not 1.
    # (A second concurrent child would need a second seeded ReviewRunItem — the
    # per-child property is already decided by the first one.)
    async with PASS_REGISTRY.track(ws_id):          # stands in for the parent
        await _run_one_call(
            kind="fit_check",
            instruction="x",
            run_id=run_id,
            workspace_id=ws_id,
            session_factory=pg_session_factory,
            settings=Settings(),
            adapter_builder=_sampling_builder,
            lifecycle_generation=0,
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
    from grant_lab.db.models import EvaluatorJob
    from grant_lab.proposal_assist.evaluator.pass_runner import run_evaluator_pass

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
            claimed_generation=0,
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
    from grant_lab.db.models import UncitedClaimsJob
    from grant_lab.proposal_assist.uncited_claims.pass_runner import (
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
            claimed_generation=0,
            session_factory=pg_session_factory,
            settings=Settings(),
            adapter_builder=_never_called_adapter_builder,
        )
    )

    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).evaluated_draft_sha is None


def test_the_review_parent_prefix_writes_nothing_for_a_deleted_workspace(
    pg_session_factory, current_user
):
    """Review's parent prefix commits the ReviewRunItem rows and run.draft_sha, so
    it needs the same workspace guard as the other three runners."""
    from grant_lab.db.models import ReviewRun, ReviewRunItem
    from grant_lab.proposal_assist.review.pass_runner import run_review_pass

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        run = ReviewRun(workspace_id=ws.id, status="running", draft_sha="0" * 40)
        s.add(run)
        s.commit()
        run_id, ws_id = run.id, ws.id

    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = _now_utc()
        s.commit()

    asyncio.run(
        run_review_pass(
            run_id=run_id,
            stamp=review_stamp(pg_session_factory, run_id, 0),
            session_factory=pg_session_factory,
            settings=Settings(),
            adapter_builder=_never_called_adapter_builder,
        )
    )

    with pg_session_factory() as s:
        items = s.scalars(
            select(ReviewRunItem).where(ReviewRunItem.review_run_id == run_id)
        ).all()
        assert items == [], "the prefix created item rows for a deleted workspace"


# --- the four prefixes, driven uniformly -------------------------------------
#
# The three tests below cover all four pass prefixes with one body each, because
# the property under test is a property of the SHAPE all four share: take the
# workspace row FOR UPDATE, hold it across context assembly (filesystem reads)
# and a write, commit. Per-pass seeding and the runner's id kwarg are the only
# differences.

_PREFIX_KINDS = ["coherence", "evaluator", "uncited", "review"]


def _seed_for(kind, pg_session_factory, current_user):
    """One 'running' job/run row on a fresh live workspace. Returns (row_id, ws_id)."""
    from grant_lab.db.models import EvaluatorJob, ReviewRun, UncitedClaimsJob

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        if kind == "coherence":
            row = CoherenceJob(workspace_id=ws.id, status="running")
        elif kind == "evaluator":
            row = EvaluatorJob(workspace_id=ws.id, status="running")
        elif kind == "uncited":
            row = UncitedClaimsJob(workspace_id=ws.id, status="running")
        else:
            row = ReviewRun(workspace_id=ws.id, status="running", draft_sha="0" * 40)
        s.add(row)
        s.commit()
        return row.id, ws.id


def _runner_for(kind):
    """Returns (runner, id_kwarg, module, assemble_attr) for one pass."""
    if kind == "coherence":
        from grant_lab.proposal_assist.coherence import pass_runner as m

        return m.run_coherence_pass, "job_id", m, "assemble_coherence_context"
    if kind == "evaluator":
        from grant_lab.proposal_assist.evaluator import pass_runner as m

        return m.run_evaluator_pass, "job_id", m, "assemble_evaluator_context"
    if kind == "uncited":
        from grant_lab.proposal_assist.uncited_claims import pass_runner as m

        return m.run_uncited_claims_pass, "job_id", m, "assemble_uncited_claims_context"
    from grant_lab.proposal_assist.review import pass_runner as m

    return m.run_review_pass, "run_id", m, "assemble_review_context"



def _fence_kwargs(kind, pg_session_factory, row_id):
    """The fence argument each runner takes.

    Review is the odd one out: it is the only pass that commits to git, so it is
    the only one converted to a threaded FenceStamp. Its three siblings still take
    a bare `claimed_generation` int. That asymmetry is real and is why this helper
    exists — a uniform driver cannot pass one kwarg to all four any more.
    """
    if kind == "review":
        return {"stamp": review_stamp(pg_session_factory, row_id, 0)}
    return {"claimed_generation": 0}


def _hold_workspace_row(pg_session_factory, ws_id, holding, release):
    """Take the workspace row FOR UPDATE from a separate connection and sit on it."""

    def _run():
        with pg_session_factory() as s:
            s.scalars(select(Workspace).where(Workspace.id == ws_id).with_for_update()).one()
            holding.set()
            # Long enough that an UNBOUNDED wait shows up as a failing assertion
            # here rather than as a hung test run.
            release.wait(timeout=20)
            s.rollback()

    return threading.Thread(target=_run)


@pytest.mark.parametrize("kind", _PREFIX_KINDS)
def test_a_prefix_gives_up_instead_of_pinning_a_workspace_row_it_cannot_get(
    kind, pg_session_factory, current_user, monkeypatch
):
    """Every pass prefix takes the workspace row FOR UPDATE and holds it across
    context assembly's filesystem reads — with PostgreSQL's lock_timeout at its
    default of 0 (disabled), that wait is unbounded.

    Unbounded is not survivable here: the owner's DELETE takes the SAME row to
    stamp soft_deleted_at, and by then it holds the per-workspace advisory mutex,
    so a stalled prefix blocks the DELETE, 429s every mutex-participating writer
    behind it, and eats into the route's < 6.0s budget. Since Slice C3 the
    DELETE's own wait on that row is capped at 2s (drafting/locking.py), so it
    gives up rather than hanging — and answers 429 having deleted nothing. The
    prefix must give up instead — a reaper requeue is cheap.

    The timeout is shortened via row_locks.PASS_PREFIX_LOCK_TIMEOUT_SECONDS
    (bounded_prefix_lock_waits reads it as a module global at call time) purely
    to keep the test fast; the holder below sits on the row for far longer, so
    an unbounded prefix cannot pass by luck.

    Mutation: delete `bounded_prefix_lock_waits(s)` from the prefix's
    `with session_factory() as s, ...` line — this must FAIL.
    """
    from grant_lab.proposal_assist import row_locks

    monkeypatch.setattr(row_locks, "PASS_PREFIX_LOCK_TIMEOUT_SECONDS", 1)
    row_id, ws_id = _seed_for(kind, pg_session_factory, current_user)
    runner, id_kwarg, _mod, _assemble = _runner_for(kind)

    holding = threading.Event()
    release = threading.Event()
    t = _hold_workspace_row(pg_session_factory, ws_id, holding, release)
    t.start()
    try:
        assert holding.wait(timeout=10), "the holder never took the workspace row"
        started = time.monotonic()
        with pytest.raises(row_locks.PassPrefixLockTimeout):
            asyncio.run(
                runner(
                    session_factory=pg_session_factory,
                    settings=Settings(),
                    adapter_builder=_stub_adapter_builder,
                    **_fence_kwargs(kind, pg_session_factory, row_id),
                    **{id_kwarg: row_id},
                )
            )
        # It gave up at its own bound rather than waiting out the holder.
        assert time.monotonic() - started < 10
    finally:
        release.set()
        t.join(timeout=30)


@pytest.mark.parametrize("kind", _PREFIX_KINDS)
def test_a_prefix_waiting_for_the_workspace_row_does_not_stall_the_event_loop(
    kind, pg_session_factory, current_user, monkeypatch
):
    """The prefix's wait for the workspace row must not freeze the app.

    The scheduler runs these passes on the application's own event loop. The
    prefix used to run its synchronous session (the row-lock wait, the cap
    checks, context assembly, the commit) directly on that loop, so while it
    waited, every other request and the live editor's sockets waited too. The
    outside review 2026-10-29 (F4) measured a 10 ms timer taking 268 ms behind a
    250 ms wait.

    A heartbeat ticks every 10 ms while the prefix waits out a held row. Its
    worst gap must stay small. The population is asserted non-empty first, so a
    heartbeat that never ran cannot pass.
    """
    from grant_lab.proposal_assist import row_locks

    monkeypatch.setattr(row_locks, "PASS_PREFIX_LOCK_TIMEOUT_SECONDS", 1)
    row_id, ws_id = _seed_for(kind, pg_session_factory, current_user)
    runner, id_kwarg, _mod, _assemble = _runner_for(kind)

    async def _drive() -> list[float]:
        gaps: list[float] = []

        async def _heartbeat() -> None:
            while True:
                t0 = time.monotonic()
                await asyncio.sleep(0.01)
                gaps.append(time.monotonic() - t0)

        beat = asyncio.create_task(_heartbeat())
        try:
            with pytest.raises(row_locks.PassPrefixLockTimeout):
                await runner(
                    session_factory=pg_session_factory,
                    settings=Settings(),
                    adapter_builder=_stub_adapter_builder,
                    **_fence_kwargs(kind, pg_session_factory, row_id),
                    **{id_kwarg: row_id},
                )
        finally:
            beat.cancel()
        return gaps

    holding = threading.Event()
    release = threading.Event()
    t = _hold_workspace_row(pg_session_factory, ws_id, holding, release)
    t.start()
    try:
        assert holding.wait(timeout=10), "the holder never took the workspace row"
        gaps = asyncio.run(_drive())
    finally:
        release.set()
        t.join(timeout=30)
    # ~1 s of waiting at 10 ms per beat: dozens of beats if the loop stayed free.
    assert len(gaps) >= 20, f"the heartbeat barely ran ({len(gaps)} beats)"
    assert max(gaps) < 0.25, f"the event loop stalled for {max(gaps):.3f} s"


@pytest.mark.parametrize("kind", ["evaluator", "uncited", "review"])
def test_a_prefix_holds_the_workspace_row_then_releases_it(
    kind, pg_session_factory, current_user, monkeypatch
):
    """The FOR UPDATE NOWAIT probe that
    test_the_coherence_prefix_holds_the_workspace_row_then_releases_it applies to
    coherence, applied to the other three prefixes.

    Their delete-guard tests commit soft_deleted_at BEFORE calling the runner, so
    the read sees it set lock or no lock — dropping `with_for_update=True` leaves
    every one of them green. Without the lock the guard is a TOCTOU check, not a
    linearization point: a DELETE committing between the read and the prefix's
    own commit is exactly the race the guard exists to close.

    Same two properties as the coherence version, both asserted while the
    interference is in force: (1) the row IS held while the prefix runs, and
    (2) it is released before any cloud work.

    Mutation: drop `with_for_update=True` from the prefix's
    `s.get(Workspace, ...)` — this must FAIL on (1).
    """
    row_id, ws_id = _seed_for(kind, pg_session_factory, current_user)
    runner, id_kwarg, mod, assemble_attr = _runner_for(kind)

    in_prefix = threading.Event()
    may_finish_prefix = threading.Event()
    real_assemble = getattr(mod, assemble_attr)

    def _paused_assemble(**kwargs):
        in_prefix.set()
        may_finish_prefix.wait(timeout=30)
        return real_assemble(**kwargs)

    monkeypatch.setattr(mod, assemble_attr, _paused_assemble)

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
                runner(
                    session_factory=pg_session_factory,
                    settings=Settings(),
                    adapter_builder=_stub_adapter_builder,
                    **_fence_kwargs(kind, pg_session_factory, row_id),
                    **{id_kwarg: row_id},
                )
            )
        except Exception:  # noqa: BLE001 — see the comment on the (2) assertion
            pass
        finally:
            done.set()

    t = threading.Thread(target=_run_pass)
    t.start()
    try:
        assert in_prefix.wait(timeout=15), "the prefix never ran"
        # (1) asserted WHILE the prefix is paused — the lock must be held now.
        assert _row_is_locked(), (
            f"the {kind} prefix does not hold the workspace row — the guard "
            "cannot linearize against a delete without it"
        )
    finally:
        may_finish_prefix.set()

    assert done.wait(timeout=30), "the pass never finished"
    t.join(timeout=5)
    # (2) the lock is gone once the prefix's session has ended. The pass itself is
    # driven by a stub adapter against a bare workspace (no repo on disk), so what
    # it does after the prefix is not this test's business — the swallowed
    # exception above keeps a post-prefix error from masquerading as a lock defect.
    assert not _row_is_locked(), f"the {kind} prefix never released the workspace row"


def _head(repo_dir: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_workspace_repo(repo_dir: Path) -> str:
    """A real git repo with one committed proposal.md, so the review prefix's
    assemble_review_context gets a draft_sha and the finalize's
    write_and_commit_files has a base to commit onto. Mirrors
    _init_repo_with_n_commits in test_reconcile.py."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo_dir, check=True)
    (repo_dir / "proposal.md").write_text("# Draft\n\nseed proposal body\n")
    subprocess.run(["git", "add", "proposal.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed proposal"], cwd=repo_dir, check=True)
    return _head(repo_dir)


def _completing_then_parking_adapter_builder(gate: threading.Event, entered: threading.Event):
    """An adapter that COMPLETES every Wave-1 call (fit_check + the four personas)
    with real markdown, then PARKS on consolidation.

    The vanilla _pausing_adapter_builder is hollow for this test: it persists no
    markdown, so `files` in the finalize is always empty and the git write is never
    reached — the finalize guard could be deleted and nothing would move. Here the
    Wave-1 markdown is committed to raw_response_json (so _run_one_call marks each
    item 'done' and _markdown_of returns non-empty), which makes the finalize
    genuinely try to write fit-check.md. Consolidation parks so the test can delete
    the workspace in the window between the persisted Wave-1 markdown and the
    finalize — the exact window the delete/finalize coupling is supposed to cover.
    """

    def _build(**kwargs):
        session_factory = kwargs["session_factory"]
        item_id = kwargs["item_id"]
        kind = kwargs["kind"]

        class _Stub:
            async def launch(self, context):
                handle = type("H", (), {})()
                handle.state = {"launch_id": uuid.uuid4()}
                handle.workspace_id = kwargs.get("workspace_id")
                return handle

            async def send_turn(self, handle, instruction):
                if kind == "consolidation":
                    # The window: Wave-1 markdown is already persisted; the finalize
                    # has not run. Hold here until the test releases the gate.
                    entered.set()
                    await asyncio.get_running_loop().run_in_executor(None, gate.wait, 30)
                    return
                    yield  # pragma: no cover
                # Persist markdown so this Wave-1 item finalizes 'done' — the deciding
                # condition in _run_one_call is item.raw_response_json.get("markdown").
                from grant_lab.db.models import ReviewRunItem

                with session_factory() as s:
                    item = s.get(ReviewRunItem, item_id)
                    item.raw_response_json = {"markdown": f"# {kind} review\n"}
                    s.commit()
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


def test_no_git_commit_lands_for_a_workspace_deleted_mid_review_pass(
    pg_session_factory, current_user, tmp_path, _settings_env
):
    """The git-level consequence of the delete/finalize coupling.

    Nothing else asserts this. The mechanism is covered by the run-row tests in
    this file, but a refactor of either soft_delete_workspace_db's cancel or
    pass_runner's still-running guard could reopen a post-delete git write
    without any of them failing.

    The delete must land BETWEEN the prefix and the finalize. Deleting first
    makes the prefix return at pass_runner.py:242-243, which passes whether or
    not the finalize guard exists — a hollow gate. And the pass must reach a real
    git-write attempt: every Wave-1 call completes with markdown so the finalize's
    `files` is non-empty; only then does deleting mid-pass (which cancels the run)
    versus the finalize's still-running guard actually decide whether HEAD moves.
    """
    from grant_lab.db.models import ReviewRun

    run_id, ws_id = _seed_review(pg_session_factory, current_user)
    repo_dir = workspace_repo_dir(Settings().workspaces_dir, ws_id)
    head_before = _init_workspace_repo(repo_dir)
    assert _head(repo_dir) == head_before

    gate = threading.Event()
    entered = threading.Event()
    done = threading.Event()

    def _pass():
        try:
            asyncio.run(
                run_review_pass(
                    run_id=run_id,
                    stamp=review_stamp(pg_session_factory, run_id, 0),
                    session_factory=pg_session_factory,
                    settings=Settings(),
                    adapter_builder=_completing_then_parking_adapter_builder(gate, entered),
                )
            )
        finally:
            done.set()

    t = threading.Thread(target=_pass)
    t.start()
    try:
        assert entered.wait(timeout=30), "the pass never reached consolidation"
        # Wave-1 markdown is persisted; the prefix has committed; finalization has
        # not run. Delete FOR REAL — through the service, so the run-cancel in the
        # delete transaction is part of what is under test. soft_delete_workspace_db
        # commits itself; there is no actor_id parameter.
        with pg_session_factory() as s:
            soft_delete_workspace_db(s, workspace_id=ws_id)
    finally:
        gate.set()
        assert done.wait(timeout=60), "the pass never finished"
        t.join(timeout=5)

    assert _head(repo_dir) == head_before, (
        "a review pass committed to the repo of a workspace deleted mid-pass"
    )
    with pg_session_factory() as s:
        assert s.get(ReviewRun, run_id).status == "cancelled"
