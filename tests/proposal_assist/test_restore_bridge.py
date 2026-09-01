"""signal_workspace_restored: the bridge that must REPORT failure.

Retention Slice C1 (spec 5.4). Retirement's bridge swallows everything because
its delete has already committed. Restore's caller has to answer 503 when the
reactivation is unconfirmed, so this one returns a verdict.
"""

from __future__ import annotations

import asyncio
import threading
import uuid

from call_manager.proposal_assist.lifecycle import (
    _RESTORE_BARRIER_SECONDS,
    WorkspaceLifecycle,
    signal_workspace_restored,
)
from call_manager.proposal_assist.retirement import _RETIREMENT_BARRIER_SECONDS


class _FakeState:
    pass


class _FakeApp:
    def __init__(self, loop, lifecycle):
        self.state = _FakeState()
        self.state.app_loop = loop
        self.state.workspace_lifecycle = lifecycle


class _FakeRequest:
    def __init__(self, app):
        self.app = app


def test_the_barrier_exceeds_the_retirement_fan_out():
    """A restore racing a retirement waits for the lifecycle lock the retirement
    coordinator holds across its hooks. If this bound were the smaller one, an
    ordinary race would surface to the admin as a 503."""
    assert _RESTORE_BARRIER_SECONDS >= _RETIREMENT_BARRIER_SECONDS


def test_a_landed_apply_reports_true_and_takes_effect():
    lifecycle = WorkspaceLifecycle()
    ws = uuid.uuid4()
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        req = _FakeRequest(_FakeApp(loop, lifecycle))
        assert signal_workspace_restored(req, workspace_id=ws, generation=5) is True
        assert lifecycle.is_retired(ws) is False
        assert lifecycle.applied_generation(ws) == 5
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)
        loop.close()


def test_a_missing_loop_or_lifecycle_reports_False():
    """Not an exception: the route turns this into 503, and a bridge that raised
    would make every caller wrap it."""
    app = _FakeApp(None, WorkspaceLifecycle())
    assert (
        signal_workspace_restored(_FakeRequest(app), workspace_id=uuid.uuid4(), generation=1)
        is False
    )


def test_a_timed_out_apply_reports_False_and_is_NOT_cancelled(monkeypatch):
    """Spec 5.4: the timed-out apply keeps running and may land later. Safe only
    because apply is monotonic — asserted here by letting it land AFTER the
    bridge already reported False."""
    lifecycle = WorkspaceLifecycle()
    ws = uuid.uuid4()
    release = threading.Event()
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    real_apply = lifecycle.apply

    async def _slow_apply(workspace_id, *, generation, retired):
        await asyncio.to_thread(release.wait, 30)
        return await real_apply(workspace_id, generation=generation, retired=retired)

    monkeypatch.setattr(lifecycle, "apply", _slow_apply)
    monkeypatch.setattr("call_manager.proposal_assist.lifecycle._RESTORE_BARRIER_SECONDS", 0.2)
    try:
        req = _FakeRequest(_FakeApp(loop, lifecycle))
        assert signal_workspace_restored(req, workspace_id=ws, generation=5) is False
        assert lifecycle.applied_generation(ws) == 0, "it should not have landed yet"

        release.set()
        for _ in range(500):  # wait for the CONDITION, not a duration
            if lifecycle.applied_generation(ws) == 5:
                break
            threading.Event().wait(0.01)
        assert lifecycle.applied_generation(ws) == 5, (
            "the timed-out apply was cancelled; spec 5.4 requires it to keep "
            "running so a late arrival and a retry converge"
        )
    finally:
        release.set()
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)
        loop.close()
