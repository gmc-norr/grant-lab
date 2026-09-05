import asyncio
import uuid

import pytest

from grant_lab.proposal_assist.drafting.contract import AgentHandle, LaunchContext
from grant_lab.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from grant_lab.proposal_assist.drafting.registry import SubprocessRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _install_handle(
    registry: SubprocessRegistry,
    workspace_id: uuid.UUID,
) -> AgentHandle:
    """Claim the launcher slot, have the fake adapter create a handle, and
    fulfil the pending launch.  Returns the installed handle."""
    am_launcher, _ = await registry.claim_launch_or_wait(workspace_id)
    assert am_launcher, "expected to become the launcher"
    ctx = LaunchContext(workspace_id=workspace_id)
    handle = await registry._adapter.launch(ctx)
    await registry.fulfill_pending_launch(workspace_id, handle, lock_session=None)
    return handle


# ---------------------------------------------------------------------------
# claim_launch_or_wait / fulfill_pending_launch / fail_pending_launch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_launch_or_wait_first_caller_becomes_launcher():
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    wsid = uuid.uuid4()
    am_launcher, pending = await registry.claim_launch_or_wait(wsid)
    assert am_launcher is True
    assert pending is None


@pytest.mark.asyncio
async def test_claim_launch_or_wait_second_caller_gets_pending_future():
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    wsid = uuid.uuid4()
    am1, _ = await registry.claim_launch_or_wait(wsid)
    am2, pending = await registry.claim_launch_or_wait(wsid)
    assert am1 is True
    assert am2 is False
    assert pending is not None
    assert not pending.done()


@pytest.mark.asyncio
async def test_fulfill_pending_launch_resolves_future_and_installs_entry():
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    wsid = uuid.uuid4()
    am1, _ = await registry.claim_launch_or_wait(wsid)
    am2, pending = await registry.claim_launch_or_wait(wsid)
    handle = AgentHandle(workspace_id=wsid, state={})
    await registry.fulfill_pending_launch(wsid, handle, lock_session=None)
    assert pending.result() is handle
    # Entry should now exist; a third caller gets a resolved future (cached).
    am3, pending3 = await registry.claim_launch_or_wait(wsid)
    assert am3 is False
    assert pending3.result() is handle


@pytest.mark.asyncio
async def test_fail_pending_launch_propagates_exception_to_waiters():
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    wsid = uuid.uuid4()
    await registry.claim_launch_or_wait(wsid)
    _, pending = await registry.claim_launch_or_wait(wsid)
    await registry.fail_pending_launch(wsid, RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        pending.result()
    # Next caller can re-attempt as launcher (pending entry was cleared).
    am, _ = await registry.claim_launch_or_wait(wsid)
    assert am is True


# ---------------------------------------------------------------------------
# terminate / sweep_idle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_terminate_removes_handle():
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    wsid = uuid.uuid4()

    h = await _install_handle(registry, wsid)
    await registry.terminate(wsid)

    assert adapter.terminations == [h]
    # After terminate, _install_handle should succeed as the launcher again
    # (no cached entry, no pending launch).
    h2 = await _install_handle(registry, wsid)
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

    await _install_handle(registry, wsid)
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
    await _install_handle(registry, wsid)
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

    handle = await _install_handle(registry, wsid)
    # Inject a fake lock_session so we can observe close().
    registry._entries[wsid].lock_session = _FakeSession()  # type: ignore[assignment]

    # Patch release_workspace_lock to avoid importing real locking machinery.
    released: list[uuid.UUID] = []

    async def _do_terminate():
        import grant_lab.proposal_assist.drafting.locking as _lock_mod

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


# ---------------------------------------------------------------------------
# in_flight accounting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_idle_skips_handles_with_in_flight_turns():
    """A handle with in_flight > 0 is never swept, even past the idle threshold."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=0)
    wsid = uuid.uuid4()

    await _install_handle(registry, wsid)
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

    await _install_handle(registry, wsid)
    await registry.increment_in_flight(wsid)
    await registry.decrement_in_flight(wsid)

    # decrement_in_flight bumps last_touched_monotonic, so we sleep again.
    await asyncio.sleep(0.01)
    swept = await registry.sweep_idle()

    assert swept == 1
    assert len(adapter.terminations) == 1


@pytest.mark.asyncio
async def test_begin_in_flight_exclusive_refuses_second_concurrent_turn():
    """The exclusive guard serializes turns at the subprocess level: once a
    turn is in flight, a second begin_in_flight_exclusive returns False so the
    reaper-freed-row race can't run two turns against one SDK client."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    wsid = uuid.uuid4()
    await _install_handle(registry, wsid)

    # First turn claims exclusivity.
    assert await registry.begin_in_flight_exclusive(wsid) is True
    # Second concurrent attempt is refused while the first is in flight.
    assert await registry.begin_in_flight_exclusive(wsid) is False
    # After the first releases, a new turn may begin again.
    await registry.decrement_in_flight(wsid)
    assert await registry.begin_in_flight_exclusive(wsid) is True


@pytest.mark.asyncio
async def test_begin_in_flight_exclusive_false_for_unknown_workspace():
    """No entry (handle gone / never launched) → refuse, don't crash."""
    registry = SubprocessRegistry(adapter=FakeAgentAdapter(), idle_seconds=600)
    assert await registry.begin_in_flight_exclusive(uuid.uuid4()) is False
