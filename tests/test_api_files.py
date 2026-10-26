"""JSON /api image-attachment endpoints (Cockpit Slice C-media)."""

import io
import uuid

import pytest
from PIL import Image
from sqlalchemy import func, select

from grant_lab.db.models import Attachment
from tests.proposal_assist._refs_helpers import add_member, authed_client, build_app
from tests.proposal_assist.conftest import make_user, make_workspace

# Every format in _file_uploads._PIL_FORMAT_TO_MIME, so a Pillow major bump
# witnesses all four header parsers the upload path accepts, not just PNG's.
_FORMAT_MIMES = [
    ("PNG", "image/png"),
    ("JPEG", "image/jpeg"),
    ("WEBP", "image/webp"),
    ("GIF", "image/gif"),
]


def _image_bytes(fmt: str = "PNG", width: int = 4, height: int = 4) -> bytes:
    img = Image.new("RGB", (width, height), color=(0, 128, 64))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _setup(pg_session_factory):
    """Return (ws_id, owner_id, member_id, stranger_id). member is a viewer."""
    with pg_session_factory() as s:
        owner = make_user(s, email="owner@umu.se")
        member = make_user(s, email="member@umu.se")
        stranger = make_user(s, email="stranger@umu.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        add_member(s, workspace_id=ws.id, user=member)
        return ws.id, owner.id, member.id, stranger.id


def _upload(app, *, user_id, ws_id, name="fig.png", data=None, mime="image/png"):
    return authed_client(app, user_id=user_id).post(
        f"/api/workspaces/{ws_id}/files",
        files={"file": (name, data if data is not None else _image_bytes(), mime)},
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


@pytest.mark.parametrize(("fmt", "mime"), _FORMAT_MIMES)
def test_api_files_upload_accepts_every_allowlisted_format(
    monkeypatch, pg_url, tmp_path, pg_session_factory, fmt, mime
):
    """Real Pillow-encoded bytes for each allow-listed format decode and persist."""
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = _upload(app, user_id=owner_id, ws_id=ws_id, data=_image_bytes(fmt), mime=mime)
    assert r.status_code == 200, r.text
    assert r.json()["mime_type"] == mime


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


def test_api_serve_file_member_roundtrips_bytes(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, member_id, _ = _setup(pg_session_factory)
    png = _image_bytes()
    aid = _upload(app, user_id=owner_id, ws_id=ws_id, data=png).json()["attachment_id"]
    r = authed_client(app, user_id=member_id).get(f"/api/workspaces/{ws_id}/files/{aid}")
    assert r.status_code == 200, r.text
    assert r.content == png
    assert r.headers["content-disposition"].startswith("inline;")
    assert r.headers["cache-control"] == "private, max-age=300"
    # Parity with the existing Jinja serve: the route handler does NOT set
    # X-Content-Type-Options (the SecurityHeadersMiddleware adds it globally
    # to all responses; only the reference serve sets it explicitly so that
    # the intent is visible in the route handler itself).
    # We verify the Content-Disposition is "inline" (not "attachment"), which
    # is the key parity difference vs the reference serve.


def test_api_serve_file_non_member_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, stranger_id = _setup(pg_session_factory)
    aid = _upload(app, user_id=owner_id, ws_id=ws_id).json()["attachment_id"]
    r = authed_client(app, user_id=stranger_id).get(f"/api/workspaces/{ws_id}/files/{aid}")
    assert r.status_code == 403, r.text


def test_api_serve_file_missing_attachment_404(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=member_id).get(f"/api/workspaces/{ws_id}/files/{uuid.uuid4()}")
    assert r.status_code == 404, r.text


def test_api_serve_file_cross_workspace_404(monkeypatch, pg_url, tmp_path, pg_session_factory):
    """IDOR: a member of workspace A cannot read an attachment owned by workspace B."""
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_a, _, member_a, _ = _setup(pg_session_factory)
    with pg_session_factory() as s:
        owner_b = make_user(s, email="ownerb@umu.se")
        ws_b = make_workspace(s, owner=owner_b)
        s.commit()
        ws_b_id, owner_b_id = ws_b.id, owner_b.id
    aid_b = _upload(app, user_id=owner_b_id, ws_id=ws_b_id).json()["attachment_id"]
    # member of A requests B's attachment id under A's URL -> 404 (not 403, not 200).
    r = authed_client(app, user_id=member_a).get(f"/api/workspaces/{ws_a}/files/{aid_b}")
    assert r.status_code == 404, r.text


def test_api_serve_file_with_a_cjk_name_is_not_a_500(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    """Review 2026-09-02 A1 item 8: `_sanitize_filename("研究.png")` keeps the
    characters (isalnum is Unicode-aware) and the header encode raised."""
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, member_id, _ = _setup(pg_session_factory)
    aid = _upload(app, user_id=owner_id, ws_id=ws_id, name="研究.png", data=_image_bytes()).json()[
        "attachment_id"
    ]
    r = authed_client(app, user_id=member_id).get(f"/api/workspaces/{ws_id}/files/{aid}")
    assert r.status_code == 200, r.text
    cd = r.headers["content-disposition"]
    assert cd.startswith("inline; filename=")
    assert "filename*=UTF-8''%E7%A0%94%E7%A9%B6.png" in cd
