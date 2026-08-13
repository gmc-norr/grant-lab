"""WorkspaceRetirement coordinator + signal_workspace_retirement bridge (B1).

The coordinator fans out to injected async hooks concurrently and in isolation;
a missing REQUIRED hook is a startup invariant (raise at construction). The
bridge reaches the coordinator from the SYNC delete route via the app loop and
is bounded so a hanging hook can never wedge the route.
"""

import asyncio
import inspect
import threading
import uuid

import pytest

from call_manager.proposal_assist import retirement
from call_manager.proposal_assist.retirement import (
    MissingRetirementHook,
    WorkspaceRetirement,
    signal_workspace_retirement,
)


def _noop_hook():
    async def _h(_ws):
        return None
    return _h


def test_the_barrier_exceeds_every_hooks_own_worst_case():
    """The barrier is a BOUND, not a hope. If a hook's own timeouts can exceed it,
    the delete route routinely logs retirement.barrier_timeout and returns to run
    scrub_workspace_bytes while teardown is still in flight — deleting files a live
    subprocess may still be reading. Hooks run concurrently (asyncio.gather), so the
    budget is the MAX of the hooks, not the sum.

    Reads the LIVE constants (and the adapter's real signature defaults) so raising
    any sub-bound fails here instead of silently eroding the barrier."""
    from call_manager.proposal_assist.collab import room as room_module
    from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
    from call_manager.proposal_assist.jobs_retirement import _JOBS_CANCEL_SECONDS

    # collab: the two artifact rooms retire CONCURRENTLY (see
    # test_collab_retire.test_both_artifact_rooms_retire_concurrently), so the hook's
    # worst case is one room's, not two.
    collab_worst = (
        room_module._RETIRE_LOCK_WAIT_SECONDS + room_module._RETIRE_OPS_DRAIN_SECONDS
    )
    params = inspect.signature(ClaudeAgentSDKAdapter.__init__).parameters
    turns_worst = (
        params["disconnect_deadline_seconds"].default + params["reap_deadline_seconds"].default
    )
    jobs_worst = _JOBS_CANCEL_SECONDS

    assert max(collab_worst, turns_worst, jobs_worst) < retirement._RETIREMENT_BARRIER_SECONDS, (
        f"barrier {retirement._RETIREMENT_BARRIER_SECONDS}s does not cover "
        f"collab {collab_worst}s / turns {turns_worst}s / jobs {jobs_worst}s"
    )


def test_jobs_is_a_required_hook():
    from call_manager.proposal_assist import retirement

    assert "jobs" in retirement.REQUIRED_HOOKS
    with pytest.raises(retirement.MissingRetirementHook):
        retirement.WorkspaceRetirement(
            hooks={"turns": _noop_hook(), "collab": _noop_hook()}  # no "jobs"
        )


def test_missing_required_hook_raises_at_construction():
    with pytest.raises(MissingRetirementHook):
        WorkspaceRetirement(hooks={"turns": _noop_hook()})  # 'collab' and 'jobs' missing
    with pytest.raises(MissingRetirementHook):
        WorkspaceRetirement(hooks={})                        # all missing


@pytest.mark.asyncio
async def test_hooks_run_concurrently_and_in_isolation():
    order: list[str] = []
    a_can_finish = asyncio.Event()

    async def hook_a(_ws):
        order.append("a-start")
        # If the hooks ran sequentially (a before b) this would deadlock, since
        # only hook_b sets the event — reaching "a-done" proves they overlap.
        await asyncio.wait_for(a_can_finish.wait(), timeout=5)
        order.append("a-done")

    async def hook_b(_ws):
        order.append("b-start")
        a_can_finish.set()          # release hook_a → concurrency
        raise ValueError("boom")    # isolation: must NOT stop hook_a

    coord = WorkspaceRetirement(hooks={"turns": hook_a, "collab": hook_b, "jobs": _noop_hook(), "passes": _noop_hook()})
    await coord.retire(uuid.uuid4())   # must NOT raise despite hook_b

    assert "a-done" in order            # hook_a completed even though hook_b raised
    assert "b-start" in order


@pytest.mark.asyncio
async def test_return_values_are_ignored():
    async def turns_hook(_ws):
        return None

    async def collab_hook(_ws):
        return 3                       # int count (mirrors revoke_user_sockets)

    coord = WorkspaceRetirement(
        hooks={
            "turns": turns_hook,
            "collab": collab_hook,
            "jobs": _noop_hook(),
            "passes": _noop_hook(),
        }
    )
    assert await coord.retire(uuid.uuid4()) is None


class _FakeState:
    pass


class _FakeApp:
    def __init__(self, *, loop, coordinator):
        self.state = _FakeState()
        self.state.app_loop = loop
        self.state.workspace_retirement = coordinator


class _FakeRequest:
    def __init__(self, app):
        self.app = app


def _loop_on_thread():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    return loop, t


def _stop_loop(loop, t):
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5)
    loop.close()


def test_bridge_runs_hooks_on_a_separate_thread_loop():
    """The sync route reaches the coordinator via run_coroutine_threadsafe on the
    app loop, which lives on a DIFFERENT thread (the event loop) than the route
    worker (a threadpool thread)."""
    seen: dict = {}
    ran = threading.Event()

    async def turns_hook(ws):
        seen["turns"] = ws

    async def collab_hook(ws):
        seen["collab"] = ws
        ran.set()

    coord = WorkspaceRetirement(
        hooks={
            "turns": turns_hook,
            "collab": collab_hook,
            "jobs": _noop_hook(),
            "passes": _noop_hook(),
        }
    )
    loop, t = _loop_on_thread()
    try:
        ws = uuid.uuid4()
        req = _FakeRequest(_FakeApp(loop=loop, coordinator=coord))
        signal_workspace_retirement(req, workspace_id=ws)   # blocks until retire returns
        assert ran.wait(timeout=5)
        assert seen == {"turns": ws, "collab": ws}
    finally:
        _stop_loop(loop, t)


def test_bridge_returns_at_the_bound_when_a_hook_hangs(monkeypatch):
    """A hook that never returns must not wedge the route: the barrier times out
    and the bridge degrades to a logged warning and RETURNS."""
    monkeypatch.setattr(retirement, "_RETIREMENT_BARRIER_SECONDS", 0.2)
    started = threading.Event()

    async def turns_hook(_ws):
        started.set()
        await asyncio.sleep(3600)      # hang well past the bound

    async def collab_hook(_ws):
        return None

    coord = WorkspaceRetirement(
        hooks={
            "turns": turns_hook,
            "collab": collab_hook,
            "jobs": _noop_hook(),
            "passes": _noop_hook(),
        }
    )
    loop, t = _loop_on_thread()
    try:
        req = _FakeRequest(_FakeApp(loop=loop, coordinator=coord))
        signal_workspace_retirement(req, workspace_id=uuid.uuid4())  # must return ~0.2s
        assert started.wait(timeout=5)           # the hook really started
    finally:
        _stop_loop(loop, t)


def test_bridge_never_raises_without_a_loop():
    """No app_loop / no coordinator → logged no-op, never an exception into the route."""
    class _Empty:
        pass

    app = _Empty()
    app.state = _FakeState()
    app.state.app_loop = None
    app.state.workspace_retirement = None
    signal_workspace_retirement(_FakeRequest(app), workspace_id=uuid.uuid4())  # no raise


def test_passes_is_a_required_hook():
    """Dropping the wiring must fail at BOOT, not silently skip teardown on every
    delete. This structural guard is the one that previously caught an unwired
    turns hook."""
    with pytest.raises(MissingRetirementHook, match="passes"):
        WorkspaceRetirement(
            hooks={"turns": _noop_hook(), "collab": _noop_hook(), "jobs": _noop_hook()}
        )


def test_the_barrier_exceeds_every_hooks_own_worst_case_including_passes():
    """Read from the LIVE constants, so raising any sub-bound fails loudly instead
    of silently eroding the barrier."""
    import inspect

    from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
    from call_manager.proposal_assist.jobs_retirement import _JOBS_CANCEL_SECONDS
    from call_manager.proposal_assist.passes import _PASS_RETIRE_SECONDS
    from call_manager.proposal_assist.retirement import _RETIREMENT_BARRIER_SECONDS

    sig = inspect.signature(ClaudeAgentSDKAdapter.__init__)
    disconnect = sig.parameters["disconnect_deadline_seconds"].default
    reap = sig.parameters["reap_deadline_seconds"].default

    # The passes bound must clear the adapter's own internal worst case...
    assert _PASS_RETIRE_SECONDS > disconnect + reap
    # ...and every hook must fit under the barrier. Hooks run CONCURRENTLY, so the
    # budget is the max, not the sum.
    worst = max(3.0, disconnect + reap, _PASS_RETIRE_SECONDS, _JOBS_CANCEL_SECONDS)
    assert worst < _RETIREMENT_BARRIER_SECONDS
