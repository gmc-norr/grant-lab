"""Cockpit chat JSON/SSE turns surface (Slice B). Reuses the workspace test
harness (_app/_seed/_client) so the app, session factory, and session cookies
match the rest of the /api suite."""

from __future__ import annotations

import uuid

from call_manager.db.models import ChatTurn, TurnJob, Workspace, WorkspaceEvent
from tests.test_api_workspaces import _app, _client, _seed


def _ack_disclosure(pg_session_factory, ws_id, user_id):
    with pg_session_factory() as s:
        s.add(WorkspaceEvent(workspace_id=ws_id, kind="disclosure_acknowledged", actor_id=user_id))
        s.commit()


def test_submit_turn_queues_job_and_echoes(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    _ack_disclosure(pg_session_factory, ws_id, owner_id)
    r = _client(app, owner_id).post(
        f"/api/workspaces/{ws_id}/turns", json={"content": "Draft Methods"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["user_turn"]["role"] == "user"
    assert "job_id" in body
    with pg_session_factory() as s:
        job = s.get(TurnJob, uuid.UUID(body["job_id"]))
        assert job is not None and job.status == "queued"
        ut = s.get(ChatTurn, uuid.UUID(body["user_turn"]["id"]))
        assert ut is not None and ut.role == "user"


def test_submit_turn_non_owner_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    _ack_disclosure(pg_session_factory, ws_id, member_id)
    r = _client(app, member_id).post(f"/api/workspaces/{ws_id}/turns", json={"content": "hi"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_submit_turn_setup_phase_409(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    # Deliberately DO NOT ack disclosure: with phase=setup we must get 409 (setup),
    # not 403 (disclosure) — this is what proves setup beats disclosure in the gate.
    with pg_session_factory() as s:
        s.get(Workspace, ws_id).current_phase = "setup"
        s.commit()
    r = _client(app, owner_id).post(f"/api/workspaces/{ws_id}/turns", json={"content": "hi"})
    assert r.status_code == 409  # setup beats disclosure (canonical order)


def test_submit_turn_second_turn_in_flight_409(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    _ack_disclosure(pg_session_factory, ws_id, owner_id)
    c = _client(app, owner_id)
    assert c.post(f"/api/workspaces/{ws_id}/turns", json={"content": "first"}).status_code == 200
    # The first turn is now queued (no drain runs in the test); a second submit
    # must be rejected while a turn is in flight.
    r2 = c.post(f"/api/workspaces/{ws_id}/turns", json={"content": "second"})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "conflict"


def test_submit_turn_needs_disclosure_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, owner_id).post(f"/api/workspaces/{ws_id}/turns", json={"content": "hi"})
    # workspace is in active phase by default (seeded state);
    # no disclosure ack → disclosure gate fires 403
    assert r.status_code == 403


def test_submit_turn_token_cap_429(monkeypatch, pg_url, pg_session_factory, tmp_path):
    from call_manager.proposal_assist.turns.caps import TokenCapExceeded

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    _ack_disclosure(pg_session_factory, ws_id, owner_id)

    def _boom(**kwargs):
        raise TokenCapExceeded(
            uuid.uuid4(),
            used_in=100,
            used_out=100,
            cap_in=100,
            cap_out=100,
            reason="input_cap_exceeded",
        )

    monkeypatch.setattr("call_manager.web.api.turns.submit_user_turn", _boom)
    r = _client(app, owner_id).post(f"/api/workspaces/{ws_id}/turns", json={"content": "hi"})
    assert r.status_code == 429
