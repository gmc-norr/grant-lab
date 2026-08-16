"""Retirement carries — and is gated on — the generation its transition allocated.

Retention Slice C0 (spec 4.4, 4.5). Two defects are closed here:

  * revision 6 had signal_workspace_retirement READ the current generation,
    which happens after the delete commits — so a restore that committed in
    between made the delete's own teardown look current;
  * forwarding a generation that nothing branches on leaves a stale retirement
    just as destructive as before. The coordinator must run NO hook when its
    transition has been superseded.
"""

from __future__ import annotations

import asyncio
import uuid

from call_manager.proposal_assist.lifecycle import WorkspaceLifecycle
from call_manager.proposal_assist.retirement import WorkspaceRetirement


def _coordinator(lifecycle, **hooks):
    """Build a coordinator with the four REQUIRED hook names, defaulting any the
    caller did not supply to a recording no-op."""
    seen: dict[str, tuple[uuid.UUID, int]] = {}

    def _recorder(name):
        async def _h(workspace_id, generation):
            seen[name] = (workspace_id, generation)
        return _h

    built = {n: hooks.get(n) or _recorder(n) for n in ("turns", "collab", "jobs", "passes")}
    return WorkspaceRetirement(hooks=built, lifecycle=lifecycle), seen


async def test_every_hook_receives_the_generation():
    lc = WorkspaceLifecycle()
    coordinator, seen = _coordinator(lc)
    ws = uuid.uuid4()
    await coordinator.retire(ws, generation=7)

    assert set(seen) == {"turns", "collab", "jobs", "passes"}
    assert all(v == (ws, 7) for v in seen.values())


async def test_the_coordinator_applies_the_transition_itself():
    """The hooks do not tombstone any more — the coordinator does, once, before
    any of them runs. Anything that consults is_retired must therefore see the
    workspace retired from inside the very first hook."""
    lc = WorkspaceLifecycle()
    observed: list[bool] = []

    async def _observe(workspace_id, generation):
        observed.append(lc.is_retired(workspace_id))

    coordinator, _ = _coordinator(lc, collab=_observe)
    ws = uuid.uuid4()
    await coordinator.retire(ws, generation=1)

    assert observed == [True], "a hook ran before the workspace was marked retired"
    assert lc.is_retired(ws) is True
    assert lc.applied_generation(ws) == 1


async def test_a_superseded_retirement_runs_NO_hook():
    """Spec 4.5. This is the finding that revision 1 of this plan missed: a
    coordinator that forwards the generation but fans out anyway still evicts
    rooms, closes sockets, pops subprocesses and cancels passes on a workspace a
    later transition brought back."""
    lc = WorkspaceLifecycle()
    coordinator, seen = _coordinator(lc)
    ws = uuid.uuid4()

    await lc.apply(ws, generation=2, retired=False)   # a restore got there first
    await coordinator.retire(ws, generation=1)

    assert seen == {}, f"a superseded retirement tore down a live workspace: {seen}"
    assert lc.is_retired(ws) is False
    assert lc.applied_generation(ws) == 2


async def test_the_lifecycle_lock_is_held_across_the_fan_out():
    """A transition landing DURING the teardown must wait for it, or it would
    un-retire a workspace whose rooms are still being evicted."""
    lc = WorkspaceLifecycle()
    started = asyncio.Event()
    order: list[str] = []

    async def _slow(workspace_id, generation):
        started.set()
        await asyncio.sleep(0.05)
        order.append("hook-done")

    coordinator, _ = _coordinator(lc, collab=_slow)
    ws = uuid.uuid4()

    task = asyncio.ensure_future(coordinator.retire(ws, generation=1))
    await started.wait()

    await lc.apply(ws, generation=2, retired=False)
    order.append("restore-applied")
    await task

    assert order == ["hook-done", "restore-applied"], (
        "a later transition landed while the teardown it supersedes was still "
        f"running: {order}"
    )


async def test_one_hook_failing_does_not_stop_the_others_receiving_it():
    """The existing isolation guarantee must survive both changes."""
    lc = WorkspaceLifecycle()

    async def _boom(workspace_id, generation):
        raise RuntimeError("hook exploded")

    coordinator, seen = _coordinator(lc, turns=_boom)
    await coordinator.retire(uuid.uuid4(), generation=1)
    assert sorted(seen) == ["collab", "jobs", "passes"]
