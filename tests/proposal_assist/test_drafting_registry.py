import asyncio
import uuid

import pytest

from call_monitor.proposal_assist.drafting.contract import AgentHandle, LaunchContext
from call_monitor.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_monitor.proposal_assist.drafting.registry import SubprocessRegistry


@pytest.mark.asyncio
async def test_registry_get_or_launch_returns_new_handle():
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    wsid = uuid.uuid4()
    ctx = LaunchContext(workspace_id=wsid)

    handle = await registry.get_or_launch(wsid, context=ctx)
    assert isinstance(handle, AgentHandle)
    assert handle.workspace_id == wsid
    assert adapter.launches[0].workspace_id == wsid


@pytest.mark.asyncio
async def test_registry_returns_same_handle_on_second_call():
    """Within the idle window, get_or_launch returns the existing handle
    without relaunching."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    wsid = uuid.uuid4()
    ctx = LaunchContext(workspace_id=wsid)

    h1 = await registry.get_or_launch(wsid, context=ctx)
    h2 = await registry.get_or_launch(wsid)

    assert h1 is h2
    assert len(adapter.launches) == 1  # only one launch


@pytest.mark.asyncio
async def test_registry_terminate_removes_handle():
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    wsid = uuid.uuid4()
    ctx = LaunchContext(workspace_id=wsid)

    h = await registry.get_or_launch(wsid, context=ctx)
    await registry.terminate(wsid)

    assert adapter.terminations == [h]
    # Next get_or_launch creates a fresh handle.
    ctx2 = LaunchContext(workspace_id=wsid)
    h2 = await registry.get_or_launch(wsid, context=ctx2)
    assert h2 is not h
    assert len(adapter.launches) == 2


@pytest.mark.asyncio
async def test_registry_sweep_idle_terminates_stale_handles():
    """Handles untouched for longer than idle_seconds are terminated by
    sweep_idle()."""
    adapter = FakeAgentAdapter()
    # idle_seconds=0 so any handle older than 'now' is stale.
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=0)
    wsid = uuid.uuid4()
    ctx = LaunchContext(workspace_id=wsid)

    await registry.get_or_launch(wsid, context=ctx)
    # Yield to give the timestamp a tiny gap.
    await asyncio.sleep(0.01)
    swept = await registry.sweep_idle()
    assert swept == 1
    assert len(adapter.terminations) == 1


@pytest.mark.asyncio
async def test_registry_sweep_idle_leaves_fresh_handles_alone():
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    wsid = uuid.uuid4()
    ctx = LaunchContext(workspace_id=wsid)
    await registry.get_or_launch(wsid, context=ctx)
    swept = await registry.sweep_idle()
    assert swept == 0
    assert adapter.terminations == []


@pytest.mark.asyncio
async def test_registry_terminate_unknown_workspace_is_noop():
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    await registry.terminate(uuid.uuid4())  # no raise
    assert adapter.terminations == []


@pytest.mark.asyncio
async def test_registry_terminate_calls_adapter_before_releasing_lock():
    """adapter.terminate must be called, and the lock_session must be closed
    even when adapter.terminate raises — verifying terminate-before-release
    ordering via a raising FakeAgentAdapter."""

    class _RaisingAdapter(FakeAgentAdapter):
        async def terminate(self, handle):
            self.terminations.append(handle)
            raise RuntimeError("adapter exploded during terminate")

    adapter = _RaisingAdapter()
    closed: list[bool] = []

    class _FakeSession:
        """Minimal stand-in for a SQLAlchemy Session."""

        def close(self):
            closed.append(True)

    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    wsid = uuid.uuid4()
    ctx = LaunchContext(workspace_id=wsid)

    handle = await registry.get_or_launch(wsid, context=ctx)
    # Inject a fake lock_session so we can observe close().
    registry._entries[wsid].lock_session = _FakeSession()  # type: ignore[assignment]

    # Patch release_workspace_lock to avoid importing real locking machinery.
    released: list[uuid.UUID] = []

    async def _do_terminate():
        import call_monitor.proposal_assist.drafting.locking as _lock_mod

        orig = _lock_mod.release_workspace_lock

        def _fake_release(session, wsid_arg):
            released.append(wsid_arg)

        _lock_mod.release_workspace_lock = _fake_release
        try:
            await registry.terminate(wsid)
        except Exception:
            pass  # the RuntimeError is expected; we only check side-effects
        finally:
            _lock_mod.release_workspace_lock = orig

    await _do_terminate()

    # adapter.terminate was called despite raising.
    assert len(adapter.terminations) == 1
    assert adapter.terminations[0] is handle
    # Lock was released and session was closed despite the exception.
    assert released == [wsid]
    assert closed == [True]


@pytest.mark.asyncio
async def test_sweep_idle_skips_handles_with_in_flight_turns():
    """A handle with in_flight > 0 is never swept, even past the idle threshold."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=0)
    wsid = uuid.uuid4()
    ctx = LaunchContext(workspace_id=wsid)

    await registry.get_or_launch(wsid, context=ctx)
    await registry.increment_in_flight(wsid)

    # Sleep past the idle threshold.
    await asyncio.sleep(0.01)
    swept = await registry.sweep_idle()

    assert swept == 0
    assert adapter.terminations == []


@pytest.mark.asyncio
async def test_decrement_in_flight_makes_handle_sweepable_again():
    """After decrement_in_flight brings in_flight back to 0 and enough
    idle time has passed, sweep_idle terminates the handle."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=0)
    wsid = uuid.uuid4()
    ctx = LaunchContext(workspace_id=wsid)

    await registry.get_or_launch(wsid, context=ctx)
    await registry.increment_in_flight(wsid)
    await registry.decrement_in_flight(wsid)

    # decrement_in_flight bumps last_touched_monotonic, so we sleep again.
    await asyncio.sleep(0.01)
    swept = await registry.sweep_idle()

    assert swept == 1
    assert len(adapter.terminations) == 1
