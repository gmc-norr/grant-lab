"""The jobs hook's work escapes every lock, so the SQL is the linearization point.

Retention Slice C0 (spec 4.6). build_jobs_retirement_hook runs its DB work as
wait_for(to_thread(...)), and the timeout cancels the AWAIT, not the thread —
this repo documents that at tests/proposal_assist/test_phase0_retirement.py:317.
The thread therefore escapes the coordinator's lifecycle lock exactly as it
escapes the timeout, so the coordinator's gate does NOT cover this hook: an
orphaned thread can wake up long after a later transition committed. A stale
check before the thread starts proves nothing about when it runs; the predicate
has to be in the statement itself.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

import call_manager.proposal_assist.jobs_retirement as jr
from call_manager.db.models import Workspace, WorkspaceSetupJob
from call_manager.proposal_assist.jobs_retirement import build_jobs_retirement_hook
from tests.proposal_assist.conftest import make_workspace


def _seed(pg_session_factory, *, generation: int):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = ws.id
        s.get(Workspace, ws_id).lifecycle_generation = generation
        s.commit()
    return ws_id


def _status(pg_session_factory, ws_id):
    with pg_session_factory() as s:
        return s.query(WorkspaceSetupJob).filter_by(workspace_id=ws_id).one().status


async def test_the_hook_cancels_at_its_own_generation(pg_session_factory):
    ws_id = _seed(pg_session_factory, generation=1)
    await build_jobs_retirement_hook(pg_session_factory)(ws_id, 1)
    assert _status(pg_session_factory, ws_id) == "cancelled"


async def test_a_stale_generation_cancels_nothing(pg_session_factory):
    """The orphaned-thread case, reduced: the hook runs with generation 1 while
    the row has already moved to 2. It must update ZERO rows — asserted on the
    row, not on whether the hook was called."""
    ws_id = _seed(pg_session_factory, generation=2)
    await build_jobs_retirement_hook(pg_session_factory)(ws_id, 1)
    assert _status(pg_session_factory, ws_id) == "queued", (
        "a superseded retirement cancelled a setup job that a later transition "
        "had brought back"
    )


async def test_a_thread_orphaned_by_the_timeout_changes_nothing(
    pg_session_factory, monkeypatch
):
    """The real shape of spec 4.6, in the real order:

      1. park the hook's DB thread and WAIT until it is genuinely parked;
      2. let the hook's own wait_for bound (_JOBS_CANCEL_SECONDS = 1.0) expire,
         so the thread is orphaned rather than merely slow;
      3. commit a later generation;
      4. release the orphan and wait for it to finish its transaction.

    Step 1 is what makes this different from the previous test. Scheduling the
    hook and then doing synchronous work never yields to the loop, so the hook
    would not have started at all — the thread would find the release already
    set and the test would silently degrade into a duplicate of the one above.
    """
    ws_id = _seed(pg_session_factory, generation=1)

    entered = threading.Event()
    release = threading.Event()
    real = jr.cancel_setup_jobs_for_workspace

    def _blocking(session, workspace_id, generation):
        entered.set()
        release.wait(timeout=30)   # bounded, so a failing test cannot hang the suite
        return real(session, workspace_id, generation)

    monkeypatch.setattr(jr, "cancel_setup_jobs_for_workspace", _blocking)

    task = asyncio.ensure_future(build_jobs_retirement_hook(pg_session_factory)(ws_id, 1))

    for _ in range(1000):          # wait for the CONDITION, not for a duration
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the hook's DB thread never parked; this test proves nothing")

    with pytest.raises(TimeoutError):   # asyncio.TimeoutError is this on 3.11+
        await task

    with pg_session_factory() as s:
        s.get(Workspace, ws_id).lifecycle_generation = 2
        s.commit()

    release.set()
    # to_thread gives no handle on its thread. This is the only boundary that
    # includes _work()'s COMMIT: an Event set by the wrapper fires before the
    # commit, and the assertion below would race it.
    await asyncio.get_running_loop().shutdown_default_executor()

    assert _status(pg_session_factory, ws_id) == "queued", (
        "an orphaned retirement thread cancelled a setup job after a later "
        "transition had moved the generation on"
    )
