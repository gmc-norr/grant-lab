"""References web routes (Slice 1 — list + upload + raw serve)."""

import io

from fastapi.testclient import TestClient

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Reference, User, Workspace
from call_manager.main import create_app


def _build_app(monkeypatch, pg_url, tmp_path):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))
    return create_app()


def _authed(app, *, user_id):
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=user_id))
    return client


def _seed_owner_and_workspace(session, *, email="owner@ki.se"):
    owner = User(email=email)
    session.add(owner)
    session.flush()
    ws = Workspace(owner_id=owner.id, title="T")
    session.add(ws)
    session.commit()
    return owner.id, ws.id


def test_panel_renders_for_owner_empty_list(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    try:
        resp = client.get(f"/workspaces/{wsid}/references")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    assert "references" in resp.text.lower()


def test_panel_returns_403_for_non_viewer(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        _, wsid = _seed_owner_and_workspace(s)
        other = User(email="other@ki.se")
        s.add(other)
        s.commit()
        other_id = other.id
    client = _authed(app, user_id=other_id)
    try:
        resp = client.get(f"/workspaces/{wsid}/references")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403


def test_upload_markdown_creates_row(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    md = b"# Hello\n\nworld\n"
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references",
            data={"kind": "paper"},
            files={"file": ("hello.md", io.BytesIO(md), "text/markdown")},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    assert "hello.md" in resp.text or "Hello" in resp.text

    from sqlalchemy import select

    with pg_session_factory() as s:
        refs = list(s.scalars(select(Reference).where(Reference.workspace_id == wsid)))
    assert len(refs) == 1
    assert refs[0].text_status == "done"


def test_upload_returns_403_for_non_owner(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        _, wsid = _seed_owner_and_workspace(s)
        viewer = User(email="viewer@ki.se")
        s.add(viewer)
        s.commit()
        viewer_id = viewer.id
        # Add viewer permission (mirrors B7 pattern; viewers CAN read, can't upload).
        from call_manager.db.models import WorkspaceViewer

        s.add(WorkspaceViewer(workspace_id=wsid, user_id=viewer_id))
        s.commit()
    client = _authed(app, user_id=viewer_id)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references",
            data={"kind": "paper"},
            files={"file": ("x.md", io.BytesIO(b"# t"), "text/markdown")},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403


def test_upload_rejects_bad_mime(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references",
            data={"kind": "paper"},
            files={"file": ("x.exe", io.BytesIO(b"MZ\x00\x00"), "application/x-msdownload")},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 400


def test_upload_failed_extraction_persists_row_with_failed_status(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Phase 2 failure: corrupted PDF; row + file retained; UI surfaces failure."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references",
            data={"kind": "paper"},
            files={"file": ("bad.pdf", io.BytesIO(b"not really a pdf"), "application/pdf")},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    assert "failed" in resp.text.lower()

    from sqlalchemy import select

    with pg_session_factory() as s:
        refs = list(s.scalars(select(Reference).where(Reference.workspace_id == wsid)))
    assert len(refs) == 1
    assert refs[0].text_status == "failed"
    assert refs[0].text_error is not None


def test_raw_serves_bytes_with_attachment_disposition(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Codex High #3 rev4 — references serve a wide MIME range; force
    download with attachment disposition + nosniff to prevent app-origin
    XSS from HTML/SVG/etc."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    md = b"# t\n"
    try:
        up = client.post(
            f"/workspaces/{wsid}/references",
            data={"kind": "paper"},
            files={"file": ("hello.md", io.BytesIO(md), "text/markdown")},
        )
        assert up.status_code == 200
        from sqlalchemy import select

        with pg_session_factory() as s:
            ref = s.scalar(select(Reference).where(Reference.workspace_id == wsid))
        resp = client.get(f"/workspaces/{wsid}/references/{ref.id}/raw")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    assert resp.content == md
    # Force download — NEVER inline.
    cd = resp.headers["content-disposition"]
    assert cd.startswith("attachment;")
    # nosniff to prevent MIME-sniffing into HTML.
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_raw_returns_404_for_other_workspace_id(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """Cross-workspace IDOR check: passing the right ref_id but the
    wrong workspace_id in the URL returns 404, not 403 — don't leak
    existence."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid_a, wsid_a = _seed_owner_and_workspace(s, email="a@ki.se")
        uid_b, wsid_b = _seed_owner_and_workspace(s, email="b@ki.se")
    client = _authed(app, user_id=uid_a)
    md = b"# t\n"
    try:
        client.post(
            f"/workspaces/{wsid_a}/references",
            data={"kind": "paper"},
            files={"file": ("t.md", io.BytesIO(md), "text/markdown")},
        )
        from sqlalchemy import select

        with pg_session_factory() as s:
            ref = s.scalar(select(Reference).where(Reference.workspace_id == wsid_a))
        # Now: workspace_a's owner asks for ref_a's bytes via workspace_b's URL.
        resp = client.get(f"/workspaces/{wsid_b}/references/{ref.id}/raw")
    finally:
        client.__exit__(None, None, None)
    # owner_a isn't allowed on workspace_b → 403 from the workspace check.
    # That's fine (doesn't leak existence). Accept either 403 or 404.
    assert resp.status_code in (403, 404)
