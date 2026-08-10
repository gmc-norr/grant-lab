"""Turns tombstone (B2): a retired workspace cannot install a subprocess entry,
and an already-live entry is torn down best-effort rather than left to the idle
sweep.

_retired is the in-process linearization point for INSTALL. It does NOT prove a
running subprocess stopped — deletion can commit between any precheck and an
in-flight streaming client.query, so Slice A's reconciliation sweep remains the
retry.
"""

import asyncio
import uuid

import pytest

from call_manager.proposal_assist.drafting.contract import WorkspaceRetired
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_manager.proposal_assist.drafting.registry import SubprocessRegistry


@pytest.mark.asyncio
async def test_retire_workspace_makes_claim_reject_before_the_cached_entry():
    """Two effects of retire_workspace on a LIVE entry: (a) claim_launch_or_wait
    rejects the workspace as its FIRST check, so a cached _entries handle is never
    handed back; (b) that entry is torn down rather than left running."""
    adapter = FakeAgentAdapter()
    reg = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    ws = uuid.uuid4()

    # Install a cached entry the normal way (launcher path). The registry stores
    # the handle opaquely in _Entry and never dereferences it here, so a sentinel
    # object() is a valid stand-in (mirrors test_drafting_registry.py, which passes
    # a throwaway handle to fulfill_pending_launch).
    am_launcher, pending = await reg.claim_launch_or_wait(ws)
    assert am_launcher is True and pending is None
    handle = object()
    installed = await reg.fulfill_pending_launch(ws, handle, lock_session=None)
    assert installed is True

    await reg.retire_workspace(ws)

    # (b) the live entry is POPPED AND TORN DOWN, not merely shadowed by the
    # tombstone. FakeAgentAdapter records terminations in `adapter.terminations`
    # (NOT `terminated` — verified against fake_adapter.py:35).
    assert adapter.terminations == [handle]
    with pytest.raises(WorkspaceRetired):
        await reg.claim_launch_or_wait(ws)     # tombstone still refuses


@pytest.mark.asyncio
async def test_retire_workspace_tears_down_a_live_entry_mid_turn():
    """Teardown must not spare a BUSY entry: a running turn on a deleted workspace
    is exactly the target (mirrors reconcile_deleted_entries, unlike sweep_idle
    which deliberately skips in_flight > 0)."""
    adapter = FakeAgentAdapter()
    reg = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    ws = uuid.uuid4()

    await reg.claim_launch_or_wait(ws)
    handle = object()
    assert await reg.fulfill_pending_launch(ws, handle, lock_session=None) is True
    await reg.increment_in_flight(ws)          # the suite's public busy marker

    await reg.retire_workspace(ws)

    assert adapter.terminations == [handle]                # torn down anyway
    assert ws not in reg._entries


@pytest.mark.asyncio
async def test_fulfill_pending_launch_refuses_to_install_and_resolves_a_real_waiter():
    """A launch in flight when retirement lands must NOT install, and must resolve
    the COALESCED WAITER with WorkspaceRetired (never hang)."""
    reg = SubprocessRegistry(adapter=FakeAgentAdapter(), idle_seconds=600)
    ws = uuid.uuid4()

    am_launcher, _ = await reg.claim_launch_or_wait(ws)   # launcher installs a pending future
    assert am_launcher is True

    # A SECOND caller coalesces onto that pending future — this is the waiter the
    # refusal has to resolve. Without it the test proves nothing about waiters.
    am_launcher2, waiter = await reg.claim_launch_or_wait(ws)
    assert am_launcher2 is False and waiter is not None

    await reg.retire_workspace(ws)                        # retire lands mid-launch

    installed = await reg.fulfill_pending_launch(ws, object(), lock_session=None)
    assert installed is False

    # The waiter raises rather than hanging or receiving a handle we discarded.
    with pytest.raises(WorkspaceRetired):
        await waiter

    # No live entry was installed.
    with pytest.raises(WorkspaceRetired):
        await reg.claim_launch_or_wait(ws)


@pytest.mark.asyncio
async def test_retire_workspace_is_idempotent():
    reg = SubprocessRegistry(adapter=FakeAgentAdapter(), idle_seconds=600)
    ws = uuid.uuid4()
    await reg.retire_workspace(ws)
    await reg.retire_workspace(ws)                        # no raise
    with pytest.raises(WorkspaceRetired):
        await reg.claim_launch_or_wait(ws)


class _BlockingLaunchAdapter(FakeAgentAdapter):
    """Pauses INSIDE launch() so retirement can land while the launcher still holds
    the pending future. That is the only window in which fulfill_pending_launch's
    refusal is reachable: retiring BEFORE controller.launch would make
    claim_launch_or_wait raise first (it is now the very first check), the adapter
    would never be called, and the launch/terminate assertions could not hold."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def launch(self, context):
        self.entered.set()
        await self.release.wait()
        return await super().launch(context)


@pytest.mark.asyncio
async def test_controller_launch_tears_down_the_fresh_handle_when_install_refused(
    pg_session_factory, tmp_path
):
    """The hand-off: fulfill returns False, so controller.launch raises
    WorkspaceRetired BEFORE handed_off=True and its existing except terminates the
    just-launched handle (leak-free)."""
    from call_manager.proposal_assist.drafting.controller import WorkspaceController
    from tests.proposal_assist.conftest import make_workspace

    adapter = _BlockingLaunchAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory, workspaces_dir=tmp_path
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)                            # NOT soft-deleted (so the
        s.commit()                                        # :111 precheck passes)
        ws_id = ws.id

    task = asyncio.create_task(controller.launch(ws_id))
    # The claim succeeded (not yet retired) and we are now parked inside
    # adapter.launch, holding the pending future.
    await asyncio.wait_for(adapter.entered.wait(), timeout=5)

    await registry.retire_workspace(ws_id)                # retire lands MID-LAUNCH
    adapter.release.set()                                 # resume into fulfill_...

    with pytest.raises(WorkspaceRetired):
        await asyncio.wait_for(task, timeout=5)

    # The fresh handle was launched then torn down; none is live, none leaked.
    # NOTE: `launches` records LaunchContext objects and `terminations` records
    # AgentHandles, so they are NOT comparable to each other — assert counts and
    # the handle's workspace_id instead.
    assert len(adapter.launches) == 1                     # launch was attempted
    assert len(adapter.terminations) == 1                 # ...and terminated
    assert adapter.terminations[0].workspace_id == ws_id
    with pytest.raises(WorkspaceRetired):
        await registry.claim_launch_or_wait(ws_id)        # nothing installed
