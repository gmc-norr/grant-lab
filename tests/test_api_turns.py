"""Cockpit chat JSON/SSE turns surface (Slice B). Reuses the workspace test
harness (_app/_seed/_client) so the app, session factory, and session cookies
match the rest of the /api suite."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

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
    assert b["needs_disclosure"] is True  # no ack yet
    assert b["can_send"] is True  # owner + active + drafting_enabled
    assert b["turns"] == []
    assert b["running_job_id"] is None


def test_turns_read_model_member_cannot_send(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    b = _client(app, member_id).get(f"/api/workspaces/{ws_id}/turns").json()
    assert b["can_send"] is False  # viewer


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
            session_factory=pg_session_factory,
            workspace_id=ws_id,
            turn_job_id=job_id,
            since_position=u_pos,
            poll_seconds=0.0,
            request=None,
        ):
            out.append(chunk)
        return out

    chunks = asyncio.run(_collect())
    joined = "".join(chunks)
    assert "event: turn" in joined
    assert "id: " in joined  # SSE id line = position (resume cursor)
    assert '"role": "assistant"' in joined or '"role":"assistant"' in joined
    assert "event: done" in joined
    assert "completed" in joined


def test_disclosure_ack_idempotent_and_flips_flag(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    c = _client(app, owner_id)
    assert c.get(f"/api/workspaces/{ws_id}/turns").json()["needs_disclosure"] is True
    r1 = c.post(f"/api/workspaces/{ws_id}/disclosure")
    assert r1.status_code == 200 and r1.json()["acknowledged"] is True
    r2 = c.post(f"/api/workspaces/{ws_id}/disclosure")  # idempotent
    assert r2.status_code == 200
    assert c.get(f"/api/workspaces/{ws_id}/turns").json()["needs_disclosure"] is False
    with pg_session_factory() as s:
        n = len(
            list(
                s.scalars(
                    select(WorkspaceEvent.id).where(
                        WorkspaceEvent.workspace_id == ws_id,
                        WorkspaceEvent.kind == "disclosure_acknowledged",
                        WorkspaceEvent.actor_id == owner_id,
                    )
                )
            )
        )
        assert n == 1  # not duplicated


def test_api_submit_turn_with_attachment_builds_image_block(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    import uuid as _uuid

    from call_manager.db.models import Attachment, ChatTurn

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "x\n")
    _ack_disclosure(pg_session_factory, ws_id, owner_id)
    aid = _uuid.uuid4()
    with pg_session_factory() as s:
        s.add(
            Attachment(
                id=aid,
                workspace_id=ws_id,
                kind="figure",
                filename="f.png",
                storage_path=f"{aid}.png",
                mime_type="image/png",
                uploaded_by=owner_id,
                width_px=10,
                height_px=10,
                tokens_estimate=80,
            )
        )
        s.commit()
    r = _client(app, owner_id).post(
        f"/api/workspaces/{ws_id}/turns",
        json={"content": "what is in this figure?", "attachment_ids": [str(aid)]},
    )
    assert r.status_code == 200, r.text
    with pg_session_factory() as s:
        turn = s.scalars(
            select(ChatTurn)
            .where(ChatTurn.workspace_id == ws_id, ChatTurn.role == "user")
            .order_by(ChatTurn.position.desc())
        ).first()
        assert isinstance(turn.content, list)
        assert {"type": "image", "attachment_id": str(aid)} in turn.content


def test_api_submit_turn_cross_workspace_attachment_is_400(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    import uuid as _uuid

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "x\n")
    _ack_disclosure(pg_session_factory, ws_id, owner_id)
    r = _client(app, owner_id).post(
        f"/api/workspaces/{ws_id}/turns",
        json={"content": "hi", "attachment_ids": [str(_uuid.uuid4())]},
    )
    assert r.status_code == 400


def test_turns_disabled_reason_branches(monkeypatch, pg_url, pg_session_factory, tmp_path):
    from call_manager.db.models import Workspace

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "x\n")

    # not_owner: a member sees not_owner
    r = _client(app, member_id).get(f"/api/workspaces/{ws_id}/turns")
    assert r.json()["disabled_reason"] == "not_owner"
    assert r.json()["is_owner"] is False

    # setup: owner on a setup-phase workspace
    with pg_session_factory() as s:
        s.get(Workspace, ws_id).current_phase = "setup"
        s.commit()
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/turns")
    assert r.json()["disabled_reason"] == "setup"
    assert r.json()["is_owner"] is True

    # active again
    with pg_session_factory() as s:
        s.get(Workspace, ws_id).current_phase = "active"
        s.commit()

    # drafting_disabled: global kill-switch off (owner, active)
    app.state.settings.drafting_enabled = False
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/turns")
    assert r.json()["disabled_reason"] == "drafting_disabled"
    app.state.settings.drafting_enabled = True

    # needs_disclosure: owner, active, drafting on, no ack yet
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/turns")
    assert r.json()["disabled_reason"] == "needs_disclosure"

    # null: after acking disclosure
    _ack_disclosure(pg_session_factory, ws_id, owner_id)
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/turns")
    assert r.json()["disabled_reason"] is None
    assert r.json()["can_send"] is True


def test_turn_stream_response_carries_sse_anti_buffer_headers(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """The production turn-stream endpoint MUST carry X-Accel-Buffering: no so
    Nginx / reverse-proxies do not buffer SSE chunks and clients see real-time
    events.  This test hits the HTTP surface (not the generator directly) to
    verify the StreamingResponse headers are wired correctly."""
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    # A completed job gives the stream something to emit and terminate quickly.
    from call_manager.db.models import ChatTurn as CT

    with pg_session_factory() as s:
        u = CT(workspace_id=ws_id, role="user", content={"type": "text", "text": "hi"})
        s.add(u)
        s.flush()
        job = TurnJob(workspace_id=ws_id, user_turn_id=u.id, status="completed")
        s.add(job)
        s.commit()
        job_id = job.id

    r = _client(app, owner_id).get(
        f"/api/workspaces/{ws_id}/turns/{job_id}/stream"
    )
    assert r.status_code == 200
    assert r.headers.get("x-accel-buffering") == "no", (
        "SSE prod stream missing X-Accel-Buffering: no — Nginx will buffer chunks"
    )
    assert r.headers.get("cache-control") == "no-cache"
