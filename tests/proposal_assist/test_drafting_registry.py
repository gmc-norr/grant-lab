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
