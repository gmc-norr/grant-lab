"""Review 2026-09-02 §2.7: `WorkspaceController.launch` did its lock session,
orphan sweep, git-drift and citekey reconciliation synchronously before its
first await — a 10 s advisory-lock wait or a git walk froze every other request
on the loop. That work now runs in a worker thread.
"""

from __future__ import annotations

import threading

import pytest

from grant_lab.proposal_assist.drafting import controller as controller_module
from tests.proposal_assist.test_turns_processor import _build_controller_and_submit


@pytest.mark.asyncio
async def test_launch_pre_await_db_and_git_work_runs_off_the_loop(
    pg_session_factory, _settings_env, monkeypatch
):
    threads: dict[str, int] = {}
    real_sweep = controller_module.sweep_orphaned_turn_jobs

    def _sweep(session, workspace_id):
        threads["sweep"] = threading.get_ident()
        return real_sweep(session, workspace_id)

    def _drift(*a, **k):
        threads["drift"] = threading.get_ident()

    def _citekeys(*a, **k):
        threads["citekeys"] = threading.get_ident()

    monkeypatch.setattr(controller_module, "sweep_orphaned_turn_jobs", _sweep)
    monkeypatch.setattr(controller_module, "reconcile_workspace_git_drift", _drift)
    monkeypatch.setattr(controller_module, "reconcile_workspace_citekeys", _citekeys)

    wsid, _job_id, controller, _ = _build_controller_and_submit(
        pg_session_factory, _settings_env, []
    )
    loop_thread = threading.get_ident()
    try:
        await controller.launch(wsid)
    finally:
        await controller.terminate(wsid)

    assert set(threads) == {"sweep", "drift", "citekeys"}
    assert all(t != loop_thread for t in threads.values()), threads
