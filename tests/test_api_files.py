"""JSON /api image-attachment endpoints (Cockpit Slice C-media)."""

import io
import uuid

from PIL import Image
from sqlalchemy import func, select

from call_manager.db.models import Attachment
from tests.proposal_assist._refs_helpers import add_member, authed_client, build_app
from tests.proposal_assist.conftest import make_user, make_workspace


def _png_bytes(width: int = 4, height: int = 4) -> bytes:
    img = Image.new("RGB", (width, height), color=(0, 128, 64))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _setup(pg_session_factory):
    """Return (ws_id, owner_id, member_id, stranger_id). member is a viewer."""
    with pg_session_factory() as s:
        owner = make_user(s, email="owner@ki.se")
        member = make_user(s, email="member@ki.se")
        stranger = make_user(s, email="stranger@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        add_member(s, workspace_id=ws.id, user=member)
        return ws.id, owner.id, member.id, stranger.id


def _upload(app, *, user_id, ws_id, name="fig.png", data=None, mime="image/png"):
    return authed_client(app, user_id=user_id).post(
        f"/api/workspaces/{ws_id}/files",
        files={"file": (name, data if data is not None else _png_bytes(), mime)},
    )


def test_api_files_upload_owner_persists_row(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = _upload(app, user_id=owner_id, ws_id=ws_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mime_type"] == "image/png"
    aid = body["attachment_id"]
    with pg_session_factory() as s:
        att = s.get(Attachment, uuid.UUID(aid))
        assert att is not None
        assert att.kind == "figure"
        assert att.workspace_id == ws_id
        assert att.tokens_estimate is not None


def test_api_files_upload_viewer_is_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory)
    r = _upload(app, user_id=member_id, ws_id=ws_id)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_api_files_upload_non_image_is_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = _upload(app, user_id=owner_id, ws_id=ws_id, data=b"not an image", mime="image/png")
    assert r.status_code == 400


def test_api_files_upload_bad_mime_is_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = _upload(app, user_id=owner_id, ws_id=ws_id, mime="application/pdf")
    assert r.status_code == 400


def test_api_files_list_member_sees_rows(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, member_id, _ = _setup(pg_session_factory)
    _upload(app, user_id=owner_id, ws_id=ws_id, name="a.png")
    r = authed_client(app, user_id=member_id).get(f"/api/workspaces/{ws_id}/files")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["filename"] == "a.png"
    assert set(rows[0]) == {"id", "filename", "mime_type", "uploaded_at"}


def test_api_files_list_stranger_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, _, stranger_id = _setup(pg_session_factory)
    r = authed_client(app, user_id=stranger_id).get(f"/api/workspaces/{ws_id}/files")
    assert r.status_code == 403


def test_api_files_delete_owner_204_and_idempotent(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    aid = _upload(app, user_id=owner_id, ws_id=ws_id).json()["attachment_id"]
    c = authed_client(app, user_id=owner_id)
    r1 = c.delete(f"/api/workspaces/{ws_id}/files/{aid}")
    assert r1.status_code == 204
    with pg_session_factory() as s:
        n = s.scalar(
            select(func.count()).select_from(Attachment).where(Attachment.workspace_id == ws_id)
        )
    assert n == 0
    r2 = c.delete(f"/api/workspaces/{ws_id}/files/{aid}")  # already gone
    assert r2.status_code == 204


def test_api_files_delete_viewer_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, member_id, _ = _setup(pg_session_factory)
    aid = _upload(app, user_id=owner_id, ws_id=ws_id).json()["attachment_id"]
    r = authed_client(app, user_id=member_id).delete(f"/api/workspaces/{ws_id}/files/{aid}")
    assert r.status_code == 403
