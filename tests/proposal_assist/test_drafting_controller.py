from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from call_manager.db.models import TurnJob
from call_manager.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    TurnDoneEvent,
)
from call_manager.proposal_assist.drafting.controller import (
    WorkspaceController,
)
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_manager.proposal_assist.drafting.locking import (
    acquire_workspace_lock,
    release_workspace_lock,
)
from call_manager.proposal_assist.drafting.registry import SubprocessRegistry
from tests.proposal_assist.conftest import make_workspace


@pytest.mark.asyncio
async def test_controller_launch_returns_handle(pg_session_factory):
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    handle = await controller.launch(wsid)
    assert handle.workspace_id == wsid
    assert adapter.launches[0].workspace_id == wsid

    # Teardown: terminate releases the per-workspace advisory lock. Without it
    # the registry's lock_session keeps a pooled connection checked out holding
    # the lock, which can poison a later test that contends for the pool.
    await controller.terminate(wsid)


@pytest.mark.asyncio
async def test_controller_launch_runs_orphan_sweep(pg_session_factory):
    """A stale running turn_jobs row is transitioned to failed before
    the new handle is handed back."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(
            TurnJob(
                workspace_id=ws.id,
                status="running",
                started_at=datetime.now(UTC),
            )
        )
        s.commit()
        wsid = ws.id

    await controller.launch(wsid)

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
        assert job.status == "failed"
        assert job.error == "orphaned"

    # Teardown: release the advisory lock (see note above).
    await controller.terminate(wsid)


@pytest.mark.asyncio
async def test_controller_launch_holds_advisory_lock_for_handle_lifetime(
    pg_session_factory,
):
    """The advisory lock is acquired at launch and held until terminate.
    While a handle is live, a second session cannot acquire the lock.
    After terminate, the lock is released."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    await controller.launch(wsid)

    # Lock should be held — a second session cannot acquire it.
    with pg_session_factory() as s2:
        assert acquire_workspace_lock(s2, wsid) is False

    # After terminate, the lock is released.
    await controller.terminate(wsid)
    with pg_session_factory() as s3:
        assert acquire_workspace_lock(s3, wsid) is True
        # Release what we just acquired: pg_advisory_lock is session(connection)-
        # scoped and the test engine pools connections, so a stranded lock would
        # ride a pooled connection into a later test (surfacing as a spurious
        # "workspace is busy" there). Unlock before s3 returns to the pool.
        release_workspace_lock(s3, wsid)


@pytest.mark.asyncio
async def test_controller_launch_returns_cached_handle_on_second_call(
    pg_session_factory,
):
    """Second launch of a live workspace returns the cached handle."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    h1 = await controller.launch(wsid)
    h2 = await controller.launch(wsid)
    assert h1 is h2

    # Teardown: release the advisory lock (see note above).
    await controller.terminate(wsid)


@pytest.mark.asyncio
async def test_controller_send_turn_yields_events(pg_session_factory):
    """Controller.send_turn iterates the adapter's stream and touches
    the registry's idle timer along the way."""
    from call_manager.proposal_assist.drafting.contract import (
        AssistantTextEvent,
        TurnDoneEvent,
    )

    adapter = FakeAgentAdapter()
    adapter.script_next_turn(
        [
            AssistantTextEvent(text="reply"),
            TurnDoneEvent(tokens_in=5, tokens_out=10, model="fake"),
        ]
    )
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    events = [event async for event in controller.send_turn(wsid, "hi")]
    assert len(events) == 2
    assert isinstance(events[0], AssistantTextEvent)
    assert isinstance(events[1], TurnDoneEvent)


@pytest.mark.asyncio
async def test_controller_launch_releases_lock_if_adapter_launch_raises(
    pg_session_factory,
):
    """If adapter.launch raises after we acquired the advisory lock,
    the controller must release the lock and close lock_session — no
    leak."""
    adapter = FakeAgentAdapter()

    async def boom(context):
        raise RuntimeError("adapter exploded")

    adapter.launch = boom  # type: ignore[method-assign]
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    with pytest.raises(RuntimeError, match="adapter exploded"):
        await controller.launch(wsid)

    # Lock must be released — a fresh session can acquire it.
    with pg_session_factory() as s2:
        assert acquire_workspace_lock(s2, wsid) is True
        # cleanup so we don't leak this test's lock
        from call_manager.proposal_assist.drafting.locking import (
            release_workspace_lock,
        )

        release_workspace_lock(s2, wsid)


@pytest.mark.asyncio
async def test_controller_launch_closes_session_if_acquire_raises(
    pg_session_factory,
    monkeypatch,
):
    """If acquire_workspace_lock itself raises (e.g., OperationalError on a
    stale pooled connection), the opened lock_session must still be closed
    so the connection doesn't leak. Without the cleanup, the connection
    would sit checked-out until GC."""
    from call_manager.proposal_assist.drafting import controller as ctrl_mod

    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    def boom(session, workspace_id):
        raise RuntimeError("DB went away mid-acquire")

    monkeypatch.setattr(ctrl_mod, "acquire_workspace_lock", boom)

    with pytest.raises(RuntimeError, match="DB went away"):
        await controller.launch(wsid)

    # Pending slot was cleared via fail_pending_launch — a subsequent
    # caller becomes the launcher (would hang otherwise).
    am_launcher, _ = await registry.claim_launch_or_wait(wsid)
    assert am_launcher is True
    # Clean the slot we just claimed so we don't leak test state.
    await registry.fail_pending_launch(wsid, RuntimeError("test cleanup"))


@pytest.mark.asyncio
async def test_controller_launch_cleans_up_handle_if_fulfill_path_cancelled(
    pg_session_factory,
):
    """If the launcher task is cancelled after adapter.launch succeeds but
    before fulfill_pending_launch runs, the orphan handle is terminated,
    the advisory lock is released, and lock_session is closed."""
    import asyncio as _asyncio

    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    # Patch fulfill_pending_launch to await indefinitely — gives us a
    # deterministic window to cancel the launcher task mid-fulfill.
    fulfill_started = _asyncio.Event()
    original_fulfill = registry.fulfill_pending_launch

    async def slow_fulfill(workspace_id, handle, *, lock_session):
        fulfill_started.set()
        await _asyncio.sleep(60)
        # Never reached.
        await original_fulfill(workspace_id, handle, lock_session=lock_session)

    registry.fulfill_pending_launch = slow_fulfill  # type: ignore[method-assign]

    task = _asyncio.create_task(controller.launch(wsid))
    await fulfill_started.wait()
    task.cancel()
    with pytest.raises(_asyncio.CancelledError):
        await task

    # Handle was terminated.
    assert len(adapter.terminations) == 1
    # Lock was released — fresh session can re-acquire.
    with pg_session_factory() as s2:
        assert acquire_workspace_lock(s2, wsid) is True
        from call_manager.proposal_assist.drafting.locking import (
            release_workspace_lock,
        )

        release_workspace_lock(s2, wsid)


@pytest.mark.asyncio
async def test_controller_sweep_idle_releases_advisory_lock(pg_session_factory):
    """sweep_idle must release the per-workspace advisory lock so
    another process can take over after the idle timeout fires."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=0)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    await controller.launch(wsid)

    # Give the touch timestamp a tiny gap so the entry is "stale".
    import asyncio as _asyncio

    await _asyncio.sleep(0.01)

    swept = await registry.sweep_idle()
    assert swept == 1

    # Lock must be released.
    with pg_session_factory() as s2:
        assert acquire_workspace_lock(s2, wsid) is True
        from call_manager.proposal_assist.drafting.locking import (
            release_workspace_lock,
        )

        release_workspace_lock(s2, wsid)


@pytest.mark.asyncio
async def test_send_turn_increments_and_decrements_in_flight(pg_session_factory):
    """in_flight is 1 while the generator is being iterated and 0 afterwards."""

    class _ProbingAdapter(FakeAgentAdapter):
        """Yields a probe event, then records in_flight, then yields TurnDone."""

        async def send_turn(self, handle, user_message):
            yield AssistantTextEvent(text="probing")

    adapter = _ProbingAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    gen = controller.send_turn(wsid, "hi")
    # Consume the first (and only) event — in_flight should be 1 at this point.
    first = await gen.__anext__()
    # Peek at in_flight while still inside the generator.
    async with registry._lock:
        entry = registry._entries.get(wsid)
        in_flight_mid = entry.in_flight if entry is not None else -1

    # Exhaust the generator so the finally block fires.
    import contextlib

    with contextlib.suppress(StopAsyncIteration):
        await gen.__anext__()

    async with registry._lock:
        entry = registry._entries.get(wsid)
        in_flight_after = entry.in_flight if entry is not None else -1

    assert isinstance(first, AssistantTextEvent)
    assert in_flight_mid == 1
    assert in_flight_after == 0


@pytest.mark.asyncio
async def test_concurrent_launch_same_workspace_returns_same_handle(
    pg_session_factory,
):
    """Two concurrent launch() calls for the same workspace return the
    same handle. Without the pending-launch fix, the second would have
    raised LockNotAcquired."""
    import asyncio

    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    h1, h2 = await asyncio.gather(
        controller.launch(wsid),
        controller.launch(wsid),
    )
    assert h1 is h2
    assert len(adapter.launches) == 1

    # Teardown: release the advisory lock (see note above).
    await controller.terminate(wsid)


@pytest.mark.asyncio
async def test_send_turn_decrements_in_flight_on_early_exit(pg_session_factory):
    """If the consumer breaks out of the loop early (aclose), in_flight is
    still decremented via the finally block."""
    adapter = FakeAgentAdapter()
    adapter.script_next_turn(
        [
            AssistantTextEvent(text="first"),
            AssistantTextEvent(text="second"),
            TurnDoneEvent(tokens_in=1, tokens_out=1, model="fake"),
        ]
    )
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    # Consume only the first event and then close the generator.
    gen = controller.send_turn(wsid, "hi")
    first = await gen.__anext__()
    await gen.aclose()

    async with registry._lock:
        entry = registry._entries.get(wsid)
        in_flight_after = entry.in_flight if entry is not None else -1

    assert isinstance(first, AssistantTextEvent)
    assert in_flight_after == 0


@pytest.mark.asyncio
async def test_launch_refuses_soft_deleted_workspace(pg_session_factory, tmp_path):
    from call_manager.db.models import Workspace
    from call_manager.proposal_assist.drafting.contract import WorkspaceRetired

    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory, workspaces_dir=tmp_path
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id
    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    with pytest.raises(WorkspaceRetired):
        await controller.launch(ws_id)
    assert adapter.launches == []


@pytest.mark.asyncio
async def test_launch_refuses_deleted_workspace_even_when_handle_is_cached(
    pg_session_factory, tmp_path
):
    """The cached/pending path returns early at controller.py:109, so the check
    MUST precede claim_launch_or_wait."""
    from call_manager.db.models import Workspace
    from call_manager.proposal_assist.drafting.contract import WorkspaceRetired

    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory, workspaces_dir=tmp_path
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    try:
        await controller.launch(ws_id)  # warm: handle cached
        assert len(adapter.launches) == 1

        with pg_session_factory() as s:
            s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
            s.commit()

        with pytest.raises(WorkspaceRetired):
            await controller.launch(ws_id)  # cached path must STILL refuse
        assert len(adapter.launches) == 1
    finally:
        # Release the advisory-lock session held by the cached entry.
        await controller.terminate(ws_id)


@pytest.mark.asyncio
async def test_precheck_race_is_documented_not_closed(pg_session_factory, tmp_path):
    """CONCEDED RACE (spec v8): a deletion committing AFTER the precheck can still
    see a cached handle returned. This test pins the documented behavior so a
    future change cannot silently claim more than the design promises."""
    from call_manager.db.models import Workspace

    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory, workspaces_dir=tmp_path
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    try:
        await controller.launch(ws_id)  # warm the cache

        original = controller._workspace_is_deleted

        def _deleting_precheck(w):
            result = original(w)  # observes NOT deleted
            with pg_session_factory() as s:  # deletion commits AFTER the check
                s.get(Workspace, w).soft_deleted_at = datetime.now(UTC)
                s.commit()
            return result

        controller._workspace_is_deleted = _deleting_precheck

        handle = await controller.launch(ws_id)  # documented: still succeeds
        assert handle is not None
        assert len(adapter.launches) == 1  # cached — no NEW subprocess
    finally:
        await controller.terminate(ws_id)


@pytest.mark.asyncio
async def test_reconcile_pops_deleted_entries_even_when_in_flight(pg_session_factory, tmp_path):
    """A deleted workspace's entry must be torn down (releasing its advisory-lock
    session) even mid-turn — the idle sweep deliberately skips in_flight > 0."""
    from call_manager.db.models import Workspace
    from tests.proposal_assist.conftest import make_user

    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory, workspaces_dir=tmp_path
    )
    with pg_session_factory() as s:
        live = make_workspace(s, owner=make_user(s, email="live-owner@ki.se"))
        dead = make_workspace(s, owner=make_user(s, email="dead-owner@ki.se"))
        s.commit()
        live_id, dead_id = live.id, dead.id

    await controller.launch(live_id)
    await controller.launch(dead_id)
    await registry.increment_in_flight(dead_id)  # mid-turn
    with pg_session_factory() as s:
        s.get(Workspace, dead_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    n = await controller.reconcile_live_clients()

    assert n >= 1
    assert dead_id not in registry._entries  # entry popped, lock released
    assert live_id in registry._entries  # live workspace untouched
    assert any(h.workspace_id == dead_id for h in adapter.terminations)
    await controller.terminate(live_id)


@pytest.mark.asyncio
async def test_reconcile_passes_soft_delete_predicate_to_the_adapter(pg_session_factory, tmp_path):
    from call_manager.db.models import Workspace
    from tests.proposal_assist.conftest import make_user

    seen: list = []

    class _RecordingAdapter(FakeAgentAdapter):
        async def reconcile_live_clients(self, is_deleted):
            seen.append(is_deleted)
            return 0

    adapter = _RecordingAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory, workspaces_dir=tmp_path
    )
    with pg_session_factory() as s:
        live = make_workspace(s, owner=make_user(s, email="p-live@ki.se"))
        dead = make_workspace(s, owner=make_user(s, email="p-dead@ki.se"))
        s.commit()
        live_id, dead_id = live.id, dead.id
    with pg_session_factory() as s:
        s.get(Workspace, dead_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    await controller.reconcile_live_clients()

    assert len(seen) == 1
    assert seen[0](dead_id) is True
    assert seen[0](live_id) is False
