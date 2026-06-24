"""Cockpit chat JSON/SSE turns surface (Slice B). Reuses the workspace test
harness (_app/_seed/_client) so the app, session factory, and session cookies
match the rest of the /api suite."""

from __future__ import annotations

import asyncio
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


def test_turns_read_model_owner(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/turns")
    assert r.status_code == 200
    b = r.json()
    assert b["phase"] == "active"
    assert b["needs_disclosure"] is True         # no ack yet
    assert b["can_send"] is True                  # owner + active + drafting_enabled
    assert b["turns"] == []
    assert b["running_job_id"] is None


def test_turns_read_model_member_cannot_send(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    b = _client(app, member_id).get(f"/api/workspaces/{ws_id}/turns").json()
    assert b["can_send"] is False                 # viewer


def test_turns_read_model_non_member_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, _, stranger_id, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, stranger_id).get(f"/api/workspaces/{ws_id}/turns")
    assert r.status_code == 403


def test_json_turn_stream_emits_turns_then_done(monkeypatch, pg_url, pg_session_factory, tmp_path):
    from call_manager.db.models import ChatTurn as CT
    from call_manager.web.api.turns import _json_turn_stream

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    # Seed a user turn + a completed job + one assistant turn after it.
    with pg_session_factory() as s:
        u = CT(workspace_id=ws_id, role="user", content={"type": "text", "text": "hi"})
        s.add(u)
        s.flush()
        job = TurnJob(workspace_id=ws_id, user_turn_id=u.id, status="completed")
        s.add(job)
        s.add(CT(workspace_id=ws_id, role="assistant", content={"type": "text", "text": "ok"}))
        s.commit()
        job_id, u_pos = job.id, u.position

    async def _collect():
        out = []
        async for chunk in _json_turn_stream(
            session_factory=pg_session_factory, workspace_id=ws_id,
            turn_job_id=job_id, since_position=u_pos, poll_seconds=0.0, request=None,
        ):
            out.append(chunk)
        return out

    chunks = asyncio.run(_collect())
    joined = "".join(chunks)
    assert "event: turn" in joined
    assert "id: " in joined                 # SSE id line = position (resume cursor)
    assert '"role": "assistant"' in joined or '"role":"assistant"' in joined
    assert "event: done" in joined
    assert "completed" in joined
