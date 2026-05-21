"""Files panel routes — B7 Task 2.

Endpoints under test:
- GET  /workspaces/<id>/files                  panel page (owner OR viewer)
- POST /workspaces/<id>/files                  upload (owner only)
- GET  /workspaces/<id>/files/<aid>            serve bytes (owner OR viewer)
- POST /workspaces/<id>/files/<aid>/delete     delete row + file (owner only)

The atomicity contract is exercised by ``test_upload_orphan_on_commit_failure``:
if the upload handler's ``session.commit`` raises after the file is on disk,
the file MUST be cleaned up via ``storage.delete_one`` — otherwise a partial
write leaves a permanent orphan that the row-source-of-truth model can't see.

Cross-workspace IDOR: the serve route returns 404 (not 403) when the
attachment row exists but belongs to a different workspace, so we don't
leak existence of attachments the caller doesn't own.
"""

from __future__ import annotations

import io
import uuid

import pytest
from PIL import Image
from sqlalchemy import select

from call_manager.db.models import Attachment, WorkspaceViewer
from tests.proposal_assist.conftest import make_user, make_workspace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_png_bytes(width: int = 4, height: int = 4) -> bytes:
    img = Image.new("RGB", (width, height), color=(0, 128, 64))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_jpeg_bytes(width: int = 8, height: int = 8) -> bytes:
    img = Image.new("RGB", (width, height), color=(200, 50, 50))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _settings_root(client_authed_owner):
    """Return the configured workspaces_dir for the live app."""
    from pathlib import Path

    return Path(client_authed_owner.app.state.settings.workspaces_dir)


@pytest.fixture(autouse=True)
def _isolated_workspaces_dir(client_authed_owner, tmp_path):
    """Point the live app at a fresh tmp_path so file writes don't leak
    between tests (or escape into the repo's var/workspaces/)."""
    client_authed_owner.app.state.settings.workspaces_dir = str(tmp_path)
    yield tmp_path


# ---------------------------------------------------------------------------
# GET /files (panel)
# ---------------------------------------------------------------------------


def test_get_files_panel_owner_200(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    resp = client_authed_owner.get(f"/workspaces/{wsid}/files")
    assert resp.status_code == 200
    assert b"Files" in resp.content


def test_get_files_panel_viewer_200(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    """A viewer can browse the files panel (read-only access to existing
    chat images)."""
    with pg_session_factory() as s:
        owner = make_user(s, email="files-owner@ki.se")
        ws = make_workspace(s, owner=owner)
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        wsid = ws.id

    resp = client_authed_user.get(f"/workspaces/{wsid}/files")
    assert resp.status_code == 200


def test_get_files_panel_stranger_403(
    client_authed_user,
    pg_session_factory,
    current_user,  # noqa: ARG001 — fixture creates current_user as a stranger here
):
    with pg_session_factory() as s:
        owner = make_user(s, email="stranger-owner@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wsid = ws.id

    resp = client_authed_user.get(f"/workspaces/{wsid}/files")
    assert resp.status_code == 403


def test_get_files_panel_lists_existing_attachments(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """Existing Attachment rows appear in the rendered panel."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        att = Attachment(
            workspace_id=ws.id,
            kind="figure",
            filename="seed.png",
            storage_path=f"{uuid.uuid4()}.png",
            mime_type="image/png",
            uploaded_by=current_user.id,
        )
        s.add(att)
        s.commit()
        wsid = ws.id

    resp = client_authed_owner.get(f"/workspaces/{wsid}/files")
    assert resp.status_code == 200
    assert b"seed.png" in resp.content


# ---------------------------------------------------------------------------
# POST /files (upload)
# ---------------------------------------------------------------------------


def test_upload_png_owner_persists_row_and_file(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    data = _make_png_bytes()
    resp = client_authed_owner.post(
        f"/workspaces/{wsid}/files",
        files={"file": ("a small fig.png", data, "image/png")},
    )
    assert resp.status_code == 200, resp.text
    # Row inserted.
    with pg_session_factory() as s:
        atts = list(s.scalars(select(Attachment).where(Attachment.workspace_id == wsid)))
    assert len(atts) == 1
    att = atts[0]
    assert att.kind == "figure"
    assert att.mime_type == "image/png"
    assert att.uploaded_by == current_user.id
    # File on disk at expected path.
    root = _settings_root(client_authed_owner)
    p = root / str(wsid) / "attachments" / f"{att.id}.png"
    assert p.exists()
    assert p.read_bytes() == data
    # The rendered partial mentions the attachment id (so HTMX can swap it in).
    assert str(att.id).encode() in resp.content


def test_upload_jpeg_uses_jpg_suffix(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """JPEG content-type → ``.jpg`` on disk (suffix derived from MIME)."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    data = _make_jpeg_bytes()
    resp = client_authed_owner.post(
        f"/workspaces/{wsid}/files",
        files={"file": ("photo.jpeg", data, "image/jpeg")},
    )
    assert resp.status_code == 200
    with pg_session_factory() as s:
        att = s.scalar(select(Attachment).where(Attachment.workspace_id == wsid))
    root = _settings_root(client_authed_owner)
    assert (root / str(wsid) / "attachments" / f"{att.id}.jpg").exists()


def test_upload_viewer_403(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    with pg_session_factory() as s:
        owner = make_user(s, email="upload-viewer-owner@ki.se")
        ws = make_workspace(s, owner=owner)
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        wsid = ws.id

    data = _make_png_bytes()
    resp = client_authed_user.post(
        f"/workspaces/{wsid}/files",
        files={"file": ("f.png", data, "image/png")},
    )
    assert resp.status_code == 403


def test_upload_stranger_403(
    client_authed_user,
    pg_session_factory,
    current_user,  # noqa: ARG001
):
    with pg_session_factory() as s:
        owner = make_user(s, email="stranger@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wsid = ws.id

    resp = client_authed_user.post(
        f"/workspaces/{wsid}/files",
        files={"file": ("f.png", _make_png_bytes(), "image/png")},
    )
    assert resp.status_code == 403


def test_upload_oversize_rejected_400_no_leak(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """Body larger than 5 MB is rejected with 400 BEFORE any disk write;
    no row, no orphan file."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    # 6 MB of bytes (any payload that exceeds the cap).
    payload = b"\x00" * (6 * 1024 * 1024)
    resp = client_authed_owner.post(
        f"/workspaces/{wsid}/files",
        files={"file": ("big.png", payload, "image/png")},
    )
    assert resp.status_code == 400
    with pg_session_factory() as s:
        rows = list(s.scalars(select(Attachment).where(Attachment.workspace_id == wsid)))
    assert rows == []
    root = _settings_root(client_authed_owner)
    att_dir = root / str(wsid) / "attachments"
    assert not att_dir.exists() or list(att_dir.iterdir()) == []


def test_upload_bad_mime_rejected_400_no_leak(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """text/plain is not in the MIME allowlist → 400, no row, no file."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    resp = client_authed_owner.post(
        f"/workspaces/{wsid}/files",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 400
    with pg_session_factory() as s:
        rows = list(s.scalars(select(Attachment).where(Attachment.workspace_id == wsid)))
    assert rows == []
    root = _settings_root(client_authed_owner)
    att_dir = root / str(wsid) / "attachments"
    assert not att_dir.exists() or list(att_dir.iterdir()) == []


def test_upload_mime_lies_about_format_rejected_400(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """Pillow probe (defense layer 2): if the bytes aren't actually a PNG
    despite content_type="image/png", reject with 400."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    resp = client_authed_owner.post(
        f"/workspaces/{wsid}/files",
        files={"file": ("fake.png", b"not a real png", "image/png")},
    )
    assert resp.status_code == 400
    with pg_session_factory() as s:
        rows = list(s.scalars(select(Attachment).where(Attachment.workspace_id == wsid)))
    assert rows == []


def test_upload_orphan_on_commit_failure(
    client_authed_owner,
    pg_session_factory,
    current_user,
    monkeypatch,
):
    """If session.commit raises after the file is on disk, the upload
    handler MUST clean up the file via storage.delete_one — otherwise
    the row-source-of-truth contract leaks orphan files."""
    from sqlalchemy.orm import Session as SAOrmSession

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    # Patch Session.commit to raise on the very next call.
    original_commit = SAOrmSession.commit
    call_state = {"raised": False}

    def boom_commit(self):
        if call_state["raised"]:
            return original_commit(self)
        call_state["raised"] = True
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(SAOrmSession, "commit", boom_commit)

    data = _make_png_bytes()
    resp = client_authed_owner.post(
        f"/workspaces/{wsid}/files",
        files={"file": ("img.png", data, "image/png")},
    )
    # Restore so subsequent assertion queries can commit normally.
    monkeypatch.setattr(SAOrmSession, "commit", original_commit)

    # Handler observed the commit failure and must surface an error.
    assert resp.status_code >= 400
    # No row.
    with pg_session_factory() as s:
        rows = list(s.scalars(select(Attachment).where(Attachment.workspace_id == wsid)))
    assert rows == []
    # No file on disk.
    root = _settings_root(client_authed_owner)
    att_dir = root / str(wsid) / "attachments"
    leftover = list(att_dir.iterdir()) if att_dir.exists() else []
    assert leftover == [], f"orphan files left on disk: {leftover}"


# ---------------------------------------------------------------------------
# GET /files/<id> (serve bytes)
# ---------------------------------------------------------------------------


def test_serve_owner_returns_bytes(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    data = _make_png_bytes()
    up = client_authed_owner.post(
        f"/workspaces/{wsid}/files",
        files={"file": ("served.png", data, "image/png")},
    )
    assert up.status_code == 200
    with pg_session_factory() as s:
        att = s.scalar(select(Attachment).where(Attachment.workspace_id == wsid))
        aid = att.id

    resp = client_authed_owner.get(f"/workspaces/{wsid}/files/{aid}")
    assert resp.status_code == 200
    assert resp.content == data
    assert resp.headers["content-type"].startswith("image/png")
    # Private cache header so a shared proxy can't leak between users.
    assert "private" in resp.headers.get("cache-control", "").lower()
    # Sanitized filename appears in Content-Disposition.
    assert "served.png" in resp.headers.get("content-disposition", "")


def test_serve_viewer_returns_bytes(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    with pg_session_factory() as s:
        owner = make_user(s, email="serve-viewer-owner@ki.se")
        ws = make_workspace(s, owner=owner)
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        att = Attachment(
            workspace_id=ws.id,
            kind="figure",
            filename="hi.png",
            storage_path=f"{uuid.uuid4()}.png",
            mime_type="image/png",
            uploaded_by=owner.id,
        )
        s.add(att)
        s.commit()
        wsid = ws.id
        aid = att.id

    # Drop the file on disk where the serve route will look for it.
    from pathlib import Path

    root = Path(client_authed_user.app.state.settings.workspaces_dir)
    att_dir = root / str(wsid) / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    data = _make_png_bytes()
    (att_dir / f"{aid}.png").write_bytes(data)

    resp = client_authed_user.get(f"/workspaces/{wsid}/files/{aid}")
    assert resp.status_code == 200
    assert resp.content == data


def test_serve_stranger_403(
    client_authed_user,
    pg_session_factory,
    current_user,  # noqa: ARG001
):
    with pg_session_factory() as s:
        owner = make_user(s, email="serve-stranger@ki.se")
        ws = make_workspace(s, owner=owner)
        att = Attachment(
            workspace_id=ws.id,
            kind="figure",
            filename="x.png",
            storage_path=f"{uuid.uuid4()}.png",
            mime_type="image/png",
            uploaded_by=owner.id,
        )
        s.add(att)
        s.commit()
        wsid = ws.id
        aid = att.id

    resp = client_authed_user.get(f"/workspaces/{wsid}/files/{aid}")
    assert resp.status_code == 403


def test_serve_cross_workspace_404_no_leak(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """An attachment that exists but belongs to a DIFFERENT workspace
    returns 404 (not 403) so we don't leak the attachment_id namespace
    across workspaces."""
    with pg_session_factory() as s:
        my_ws = make_workspace(s, owner=current_user)
        other_owner = make_user(s, email="other-owner@ki.se")
        other_ws = make_workspace(s, owner=other_owner)
        att = Attachment(
            workspace_id=other_ws.id,
            kind="figure",
            filename="not yours.png",
            storage_path=f"{uuid.uuid4()}.png",
            mime_type="image/png",
            uploaded_by=other_owner.id,
        )
        s.add(att)
        s.commit()
        my_wsid = my_ws.id
        aid = att.id

    resp = client_authed_owner.get(f"/workspaces/{my_wsid}/files/{aid}")
    assert resp.status_code == 404


def test_serve_missing_file_on_disk_404(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """If the DB row exists but the file is missing on disk, return 404."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        att = Attachment(
            workspace_id=ws.id,
            kind="figure",
            filename="gone.png",
            storage_path=f"{uuid.uuid4()}.png",
            mime_type="image/png",
            uploaded_by=current_user.id,
        )
        s.add(att)
        s.commit()
        wsid = ws.id
        aid = att.id

    resp = client_authed_owner.get(f"/workspaces/{wsid}/files/{aid}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /files/<id>/delete
# ---------------------------------------------------------------------------


def test_delete_owner_removes_row_and_file(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    up = client_authed_owner.post(
        f"/workspaces/{wsid}/files",
        files={"file": ("del.png", _make_png_bytes(), "image/png")},
    )
    assert up.status_code == 200
    with pg_session_factory() as s:
        att = s.scalar(select(Attachment).where(Attachment.workspace_id == wsid))
        aid = att.id

    root = _settings_root(client_authed_owner)
    file_path = root / str(wsid) / "attachments" / f"{aid}.png"
    assert file_path.exists()

    resp = client_authed_owner.post(
        f"/workspaces/{wsid}/files/{aid}/delete",
    )
    assert resp.status_code in (200, 303)
    # Row is gone.
    with pg_session_factory() as s:
        rows = list(s.scalars(select(Attachment).where(Attachment.id == aid)))
    assert rows == []
    # File is gone.
    assert not file_path.exists()


def test_delete_viewer_403(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    with pg_session_factory() as s:
        owner = make_user(s, email="del-viewer-owner@ki.se")
        ws = make_workspace(s, owner=owner)
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        att = Attachment(
            workspace_id=ws.id,
            kind="figure",
            filename="x.png",
            storage_path=f"{uuid.uuid4()}.png",
            mime_type="image/png",
            uploaded_by=owner.id,
        )
        s.add(att)
        s.commit()
        wsid = ws.id
        aid = att.id

    resp = client_authed_user.post(f"/workspaces/{wsid}/files/{aid}/delete")
    assert resp.status_code == 403


def test_delete_is_idempotent(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """Calling delete on a non-existent attachment_id returns 200 / 303
    (idempotent — no row, no file, no error)."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    missing_aid = uuid.uuid4()
    resp = client_authed_owner.post(
        f"/workspaces/{wsid}/files/{missing_aid}/delete",
    )
    assert resp.status_code in (200, 303, 404)


def test_delete_cross_workspace_returns_404(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """Deleting an attachment from a workspace the caller owns, but whose
    attachment_id actually belongs to a DIFFERENT workspace, must not
    succeed — 404 (or 200 idempotent no-op) and the other ws's row is
    untouched."""
    with pg_session_factory() as s:
        my_ws = make_workspace(s, owner=current_user)
        other_owner = make_user(s, email="x-del@ki.se")
        other_ws = make_workspace(s, owner=other_owner)
        att = Attachment(
            workspace_id=other_ws.id,
            kind="figure",
            filename="protected.png",
            storage_path=f"{uuid.uuid4()}.png",
            mime_type="image/png",
            uploaded_by=other_owner.id,
        )
        s.add(att)
        s.commit()
        my_wsid = my_ws.id
        aid = att.id

    resp = client_authed_owner.post(f"/workspaces/{my_wsid}/files/{aid}/delete")
    assert resp.status_code in (200, 303, 404)
    # Other workspace's row is intact.
    with pg_session_factory() as s:
        survivor = s.get(Attachment, aid)
    assert survivor is not None
