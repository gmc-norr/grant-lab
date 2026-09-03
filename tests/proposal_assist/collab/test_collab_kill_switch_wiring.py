"""3b-5d.D4 Item 3: the two kill-switch handover choke points are wired ON only
when the switch is engaged — the main.py lifespan startup handover and the
scheduler scan-tick branch. Covers both the ON and OFF directions."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from call_manager.config import Settings
from call_manager.main import create_app
from call_manager.proposal_assist.collab import state as collab_state
from call_manager.scheduler import build_scheduler


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    """create_app re-arms the module global from Settings, and these tests flip the
    switch via env — a leaked True poisons every later collab test."""
    yield
    collab_state.set_kill_switch(False)


def _base_env(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://testserver",
        "DRAFTING_ADAPTER": "fake",
    }.items():
        monkeypatch.setenv(k, v)


# --- main.py lifespan startup handover ------------------------------------- #


def test_lifespan_runs_handover_when_kill_switch_on(monkeypatch, pg_url, tmp_path):
    """COLLAB_KILL_SWITCH=1 -> create_app re-arms the switch -> the lifespan
    startup handover runs BEFORE serving (TestClient context-manager triggers it)."""
    _base_env(monkeypatch, pg_url)
    monkeypatch.setenv("COLLAB_KILL_SWITCH", "1")
    calls: list[bool] = []
    # Deferred import inside lifespan resolves this attribute at call time.
    monkeypatch.setattr(
        "call_manager.proposal_assist.collab.handover.run_kill_switch_handover",
        lambda *a, **k: calls.append(True) or 0,
    )
    app = create_app()
    app.state.settings.workspaces_dir = str(tmp_path)
    with TestClient(app):  # __enter__ triggers lifespan startup
        pass
    assert calls == [True]


def test_lifespan_skips_handover_when_kill_switch_off(monkeypatch, pg_url, tmp_path):
    """Switch off -> the startup handover branch is never entered."""
    _base_env(monkeypatch, pg_url)
    monkeypatch.delenv("COLLAB_KILL_SWITCH", raising=False)
    calls: list[bool] = []
    monkeypatch.setattr(
        "call_manager.proposal_assist.collab.handover.run_kill_switch_handover",
        lambda *a, **k: calls.append(True) or 0,
    )
    app = create_app()
    app.state.settings.workspaces_dir = str(tmp_path)
    with TestClient(app):
        pass
    assert calls == []


# --- scheduler scan-tick branch -------------------------------------------- #


class _NullAlerter:
    def record_failure(self, *a, **kw):
        pass

    def record_success(self, *a, **kw):
        pass


def _collab_tick_func(monkeypatch, pg_url):
    """Build the scheduler (unstarted) from env-driven Settings and return its
    collab checkpoint tick closure — 'calling the job builder/tick function
    directly' without create_app in the way (the branch reads settings, not the
    module global)."""
    _base_env(monkeypatch, pg_url)
    from call_manager.proposal_assist.drafting.factory import build_workspace_controller

    settings = Settings()

    def session_factory():  # never invoked — handover/checkpoint are monkeypatched
        return None

    controller = build_workspace_controller(settings=settings, session_factory=session_factory)
    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        workspace_controller=controller,
        start=False,
    )
    try:
        return sched.get_job("collab:checkpoint_tails").func
    finally:
        sched.shutdown(wait=False)


def test_scheduler_tick_routes_to_handover_when_kill_switch_on(monkeypatch, pg_url):
    monkeypatch.setenv("COLLAB_KILL_SWITCH", "1")
    tick = _collab_tick_func(monkeypatch, pg_url)
    hits = {"handover": 0, "checkpoint": 0}
    monkeypatch.setattr(
        "call_manager.proposal_assist.collab.handover.run_kill_switch_handover",
        lambda *a, **k: hits.__setitem__("handover", hits["handover"] + 1),
    )
    monkeypatch.setattr(
        "call_manager.proposal_assist.collab.checkpoint.checkpoint_active_tails",
        lambda *a, **k: hits.__setitem__("checkpoint", hits["checkpoint"] + 1),
    )
    tick()
    assert hits == {"handover": 1, "checkpoint": 0}


def test_scheduler_tick_checkpoints_when_kill_switch_off(monkeypatch, pg_url):
    monkeypatch.delenv("COLLAB_KILL_SWITCH", raising=False)
    tick = _collab_tick_func(monkeypatch, pg_url)
    hits = {"handover": 0, "checkpoint": 0}
    monkeypatch.setattr(
        "call_manager.proposal_assist.collab.handover.run_kill_switch_handover",
        lambda *a, **k: hits.__setitem__("handover", hits["handover"] + 1),
    )
    monkeypatch.setattr(
        "call_manager.proposal_assist.collab.checkpoint.checkpoint_active_tails",
        lambda *a, **k: hits.__setitem__("checkpoint", hits["checkpoint"] + 1),
    )
    tick()
    assert hits == {"handover": 0, "checkpoint": 1}
