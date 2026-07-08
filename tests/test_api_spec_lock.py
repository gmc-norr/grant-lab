"""Cockpit spec lock/unlock endpoints + enforcement. Reuses the _app/_seed/
_client helpers + _add_spec from tests/test_api_spec.py."""

from __future__ import annotations

import pytest

from call_manager.db.models import SpecLockEvent
from call_manager.proposal_assist.collab import state as collab_state
from tests.test_api_spec import _add_spec
from tests.test_api_workspaces import _app, _client, _seed


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    """The switch is a module global — a leaked True poisons every collab test."""
    yield
    collab_state.set_kill_switch(False)


@pytest.fixture
def _cm6_world(monkeypatch):
    """Run this test in the kill-switch world, where the legacy CM6
    whole-section save path is still the write path (3b-5d.D4 severed
    CM6 saves on collab-enabled artifacts; the kill-switch masks
    enablement, so enable-on-touch is suppressed and legacy saves work).

    Set via the env var (not a direct set_kill_switch(True) call): the test
    body's _app() call runs create_app() AFTER this fixture, and create_app()
    re-arms the module global from Settings().collab_kill_switch — a bare
    set_kill_switch(True) here would be clobbered before the request ever
    lands. monkeypatch.setenv persists through that reset since Settings()
    reads it fresh, and the module scope makes _reset_kill_switch's teardown
    the belt to this env var's suspenders.
    """
    monkeypatch.setenv("COLLAB_KILL_SWITCH", "1")
    collab_state.set_kill_switch(True)


def _events(pg_session_factory, ws_id):
    with pg_session_factory() as s:
        return s.query(SpecLockEvent).filter(SpecLockEvent.workspace_id == ws_id).all()


def test_lock_owner_writes_one_event_and_pins_head(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")
    c = _client(app, owner_id)
    head = c.get(f"/api/workspaces/{ws_id}/spec").json()["head"]
    r = c.post(f"/api/workspaces/{ws_id}/spec/lock")
    assert r.status_code == 200
    body = r.json()
    assert body["locked"] is True and body["locked_by"] == "owner@ki.se"
    evs = _events(pg_session_factory, ws_id)
    assert len(evs) == 1
    assert evs[0].action == "lock" and evs[0].artifact_commit_sha == head


def test_lock_empty_spec_is_409_no_event(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "   \n")  # whitespace-only
    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/spec/lock")
    assert r.status_code == 409
    assert _events(pg_session_factory, ws_id) == []


def test_lock_is_idempotent_no_second_event(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    c = _client(app, owner_id)
    c.post(f"/api/workspaces/{ws_id}/spec/lock")
    r2 = c.post(f"/api/workspaces/{ws_id}/spec/lock")
    assert r2.status_code == 200 and r2.json()["locked"] is True
    assert len(_events(pg_session_factory, ws_id)) == 1


def test_unlock_clears_state_and_audits(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    c = _client(app, owner_id)
    c.post(f"/api/workspaces/{ws_id}/spec/lock")
    r = c.post(f"/api/workspaces/{ws_id}/spec/unlock")
    assert r.status_code == 200 and r.json()["locked"] is False
    actions = [e.action for e in _events(pg_session_factory, ws_id)]
    assert actions == ["lock", "unlock"]


def test_unlock_when_unlocked_is_noop(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/spec/unlock")
    assert r.status_code == 200 and r.json()["locked"] is False
    assert _events(pg_session_factory, ws_id) == []


def test_lock_member_forbidden(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    assert _client(app, member_id).post(f"/api/workspaces/{ws_id}/spec/lock").status_code == 403


def test_lock_no_head_is_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    # A GitCommitError from current_head (unborn HEAD) maps to 404, not a
    # sanitized 500 — mirrors the /spec read/save routes. Patch current_head to
    # raise so the test is independent of git's unborn-branch behaviour.
    from call_manager.proposal_assist.citations.gitio import GitCommitError

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")  # file exists so the pre-check passes

    def _boom(*args, **kwargs):
        raise GitCommitError("no HEAD")

    monkeypatch.setattr("call_manager.web.api.workspaces.current_head", _boom)
    r = _client(app, owner_id).post(f"/api/workspaces/{ws_id}/spec/lock")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_locked_spec_save_is_409(monkeypatch, pg_url, pg_session_factory, tmp_path, _cm6_world):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")
    c = _client(app, owner_id)
    spec = c.get(f"/api/workspaces/{ws_id}/spec").json()
    aims = spec["sections"][1]
    c.post(f"/api/workspaces/{ws_id}/spec/lock")
    r = c.post(
        f"/api/workspaces/{ws_id}/spec/sections/1",
        json={
            "content": "## Aims\nedited.\n",
            "base_commit_sha": spec["head"],
            "base_section_sha": aims["sha"],
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"
    # Prove the 409 is the spec-lock guard, not a coincidental staleness
    # conflict (both map to 409/conflict). The lock message is distinctive.
    assert "spec is locked" in r.json()["error"]["message"].lower()


def test_locked_spec_init_is_409(monkeypatch, pg_url, pg_session_factory, tmp_path):
    # Lock a seeded (non-empty) spec, then call init: post_init_spec bypasses
    # save_section, so its OWN guard must fire. The guard runs before the
    # whitespace/no-op check, so 409 regardless of file content.
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    c = _client(app, owner_id)
    c.post(f"/api/workspaces/{ws_id}/spec/lock")
    assert c.post(f"/api/workspaces/{ws_id}/spec").status_code == 409


def test_draft_unaffected_by_spec_lock(
    monkeypatch, pg_url, pg_session_factory, tmp_path, _cm6_world
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n\n## Methods\nm.\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    c = _client(app, owner_id)
    c.post(f"/api/workspaces/{ws_id}/spec/lock")
    draft = c.get(f"/api/workspaces/{ws_id}/draft").json()
    methods = draft["sections"][1]
    r = c.post(
        f"/api/workspaces/{ws_id}/sections/1",
        json={
            "content": "## Methods\nm2.\n",
            "base_commit_sha": draft["head"],
            "base_section_sha": methods["sha"],
        },
    )
    assert r.status_code == 200 and r.json()["changed"] is True


def test_unlock_reenables_spec_save(
    monkeypatch, pg_url, pg_session_factory, tmp_path, _cm6_world
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")
    c = _client(app, owner_id)
    c.post(f"/api/workspaces/{ws_id}/spec/lock")
    c.post(f"/api/workspaces/{ws_id}/spec/unlock")
    spec = c.get(f"/api/workspaces/{ws_id}/spec").json()
    aims = spec["sections"][1]
    r = c.post(
        f"/api/workspaces/{ws_id}/spec/sections/1",
        json={
            "content": "## Aims\nedited after unlock.\n",
            "base_commit_sha": spec["head"],
            "base_section_sha": aims["sha"],
        },
    )
    assert r.status_code == 200 and r.json()["changed"] is True
