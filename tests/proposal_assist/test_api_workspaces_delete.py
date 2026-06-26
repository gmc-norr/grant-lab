"""DELETE /api/workspaces/{id} — owner-only soft-delete (cockpit gap-closer)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from call_manager.db.models import (
    ChatTurn,
    CoherenceJob,
    TurnJob,
    Workspace,
    WorkspaceEvent,
    WorkspaceViewer,
)
from tests.proposal_assist.conftest import make_user, make_workspace


def test_owner_delete_204_cascades_and_scrubs(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        s.add(CoherenceJob(workspace_id=ws.id, status="queued"))
        ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(ut)
        s.flush()
        s.add(
            TurnJob(
                workspace_id=ws.id,
                user_turn_id=ut.id,
                submitted_by_user_id=current_user.id,
                status="queued",
                queued_at=datetime.now(UTC),
            )
        )
        s.commit()
        ws_id = ws.id

    attach_dir = tmp_path / str(ws_id) / "attachments"
    attach_dir.mkdir(parents=True)
    (attach_dir / "blob.bin").write_bytes(b"x")

    r = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
    assert r.status_code == 204

    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is not None
        assert (
            s.scalar(select(CoherenceJob.status).where(CoherenceJob.workspace_id == ws_id))
            == "failed"
        )
        # Carry forward the emit_chat=False guarantee (G2 deletes the Jinja-route test):
        assert (
            s.scalar(select(TurnJob.status).where(TurnJob.workspace_id == ws_id)) == "cancelled"
        )
        assert (
            s.scalar(
                select(WorkspaceEvent.id).where(WorkspaceEvent.kind == "cloud_turn_cancelled")
            )
            is not None
        )
        assert s.scalar(select(ChatTurn).where(ChatTurn.role == "system")) is None
    assert not attach_dir.exists()


def test_non_owner_delete_403(client_authed_owner, current_user, pg_session_factory):
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger@ki.se")
        ws = make_workspace(s, owner=stranger)  # current_user has no relationship to it
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
    assert r.status_code == 403
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is None  # untouched


def test_viewer_delete_403(client_authed_owner, current_user, pg_session_factory):
    # A workspace member (viewer) is still not the owner → 403 (spec §4.1: viewer AND stranger).
    with pg_session_factory() as s:
        stranger = make_user(s, email="viewer-owner@ki.se")
        ws = make_workspace(s, owner=stranger)
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        ws_id = ws.id
    r = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
    assert r.status_code == 403
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is None  # untouched


def test_unknown_workspace_delete_404(client_authed_owner, current_user, pg_session_factory):
    # require_owner returns 404 for a workspace that does not exist (spec §4.1).
    assert client_authed_owner.delete(f"/api/workspaces/{uuid.uuid4()}").status_code == 404


def test_redelete_is_404(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id
    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204
    # require_owner filters soft_deleted_at IS NULL → second delete is 404 (not idempotent).
    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 404
