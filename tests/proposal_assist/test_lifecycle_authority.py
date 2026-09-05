"""collab and turns consult WorkspaceLifecycle, not their own sets.

Retention Slice C0 (spec 4.5). Two sets meant a reactivation had to clear both,
and a fan-out can half-succeed. One authority means a transition is one write.

These assertions deliberately exercise the SUBSYSTEMS rather than reading
lifecycle state: reading the authority would pass even if a guard still
consulted a stale local set.
"""
from __future__ import annotations

import uuid

import pytest

from grant_lab.proposal_assist.collab.room import MANAGER, ClientState, RoomManager
from grant_lab.proposal_assist.drafting.contract import WorkspaceRetired
from grant_lab.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from grant_lab.proposal_assist.drafting.registry import SubprocessRegistry
from grant_lab.proposal_assist.lifecycle import LIFECYCLE, WorkspaceLifecycle


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _empty_load():
    return []


def _make_client():
    class _FakeClient:
        async def send_bytes(self, data: bytes) -> None:
            pass

        async def close(self, code: int = 1000) -> None:
            pass

    return _FakeClient()


# --------------------------------------------------------------------------
# 1. Without calling retire_workspace: apply(retired=True) alone makes the
#    subsystems refuse.
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_collab_acquire_refuses_after_authority_retire_without_hook():
    """apply(retired=True) is enough to make acquire raise WorkspaceRetired.
    retire_workspace is NOT called — proving the guard reads the authority."""
    lifecycle = WorkspaceLifecycle()
    mgr = RoomManager(lifecycle=lifecycle)
    ws = uuid.uuid4()
    await lifecycle.apply(ws, generation=1, retired=True)
    with pytest.raises(WorkspaceRetired):
        await mgr.acquire(
            (ws, "draft"),
            load=_empty_load,
            client=_make_client(),
            state=ClientState(user_id=1, can_write=True),
        )


@pytest.mark.anyio
async def test_turns_claim_refuses_after_authority_retire_without_hook():
    """apply(retired=True) is enough to make claim_launch_or_wait raise WorkspaceRetired.
    retire_workspace is NOT called — proving the guard reads the authority."""
    lifecycle = WorkspaceLifecycle()
    reg = SubprocessRegistry(adapter=FakeAgentAdapter(), idle_seconds=600, lifecycle=lifecycle)
    ws = uuid.uuid4()
    await lifecycle.apply(ws, generation=1, retired=True)
    with pytest.raises(WorkspaceRetired):
        await reg.claim_launch_or_wait(ws)


# --------------------------------------------------------------------------
# 2. apply(retired=False) after retired=True un-poisons both subsystems.
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_collab_acquire_accepts_after_authority_restore():
    """apply(generation=2, retired=False) after retire makes acquire succeed again."""
    lifecycle = WorkspaceLifecycle()
    mgr = RoomManager(lifecycle=lifecycle)
    ws = uuid.uuid4()
    await lifecycle.apply(ws, generation=1, retired=True)
    await lifecycle.apply(ws, generation=2, retired=False)
    # Should succeed (no raise)
    await mgr.acquire(
        (ws, "draft"),
        load=_empty_load,
        client=_make_client(),
        state=ClientState(user_id=1, can_write=True),
    )


@pytest.mark.anyio
async def test_turns_claim_accepts_after_authority_restore():
    """apply(generation=2, retired=False) after retire makes claim succeed again."""
    lifecycle = WorkspaceLifecycle()
    reg = SubprocessRegistry(adapter=FakeAgentAdapter(), idle_seconds=600, lifecycle=lifecycle)
    ws = uuid.uuid4()
    await lifecycle.apply(ws, generation=1, retired=True)
    await lifecycle.apply(ws, generation=2, retired=False)
    am_launcher, _ = await reg.claim_launch_or_wait(ws)
    assert am_launcher is True


# --------------------------------------------------------------------------
# 3. A stale retire (lower generation) arriving after a restore leaves both
#    subsystems accepting.
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stale_retire_does_not_poison_collab_after_restore():
    """apply(generation=1, retired=True) arriving AFTER generation=2 is a no-op."""
    lifecycle = WorkspaceLifecycle()
    mgr = RoomManager(lifecycle=lifecycle)
    ws = uuid.uuid4()
    await lifecycle.apply(ws, generation=2, retired=False)  # restore arrived first
    await lifecycle.apply(ws, generation=1, retired=True)  # stale retire arrives late
    # Subsystem must still accept
    await mgr.acquire(
        (ws, "draft"),
        load=_empty_load,
        client=_make_client(),
        state=ClientState(user_id=1, can_write=True),
    )


@pytest.mark.anyio
async def test_stale_retire_does_not_poison_turns_after_restore():
    lifecycle = WorkspaceLifecycle()
    reg = SubprocessRegistry(adapter=FakeAgentAdapter(), idle_seconds=600, lifecycle=lifecycle)
    ws = uuid.uuid4()
    await lifecycle.apply(ws, generation=2, retired=False)
    await lifecycle.apply(ws, generation=1, retired=True)
    am_launcher, _ = await reg.claim_launch_or_wait(ws)
    assert am_launcher is True


# --------------------------------------------------------------------------
# 4. Two managers on different lifecycles are independent.
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_two_managers_on_different_lifecycles_are_independent():
    """A lifecycle passed to RoomManager(lifecycle=...) is the one its guards
    consult — retiring on lifecycle_a does not affect mgr_b."""
    lc_a = WorkspaceLifecycle()
    lc_b = WorkspaceLifecycle()
    mgr_a = RoomManager(lifecycle=lc_a)
    mgr_b = RoomManager(lifecycle=lc_b)
    ws = uuid.uuid4()
    await lc_a.apply(ws, generation=1, retired=True)
    # mgr_a refuses
    with pytest.raises(WorkspaceRetired):
        await mgr_a.acquire(
            (ws, "draft"),
            load=_empty_load,
            client=_make_client(),
            state=ClientState(user_id=1, can_write=True),
        )
    # mgr_b accepts (lc_b knows nothing about ws)
    await mgr_b.acquire(
        (ws, "draft"),
        load=_empty_load,
        client=_make_client(),
        state=ClientState(user_id=1, can_write=True),
    )


# --------------------------------------------------------------------------
# 5. Identity wiring: all four production references are the same object.
# --------------------------------------------------------------------------


def test_all_four_lifecycle_references_are_the_same_object(client_authed_owner):
    """Identity proof: the coordinator, collab room manager, turns registry, and
    app.state all share the one LIFECYCLE singleton."""
    app = client_authed_owner.app
    workspace_controller = app.state.workspace_controller
    assert app.state.workspace_lifecycle is LIFECYCLE
    assert MANAGER._lifecycle is app.state.workspace_lifecycle
    assert workspace_controller.registry._lifecycle is app.state.workspace_lifecycle
    assert app.state.workspace_retirement._lifecycle is app.state.workspace_lifecycle
