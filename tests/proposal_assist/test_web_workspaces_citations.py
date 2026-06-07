"""Citations web view (Task 3 — human-facing evidence-ledger panel, read-only)."""

from pathlib import Path

from fastapi.testclient import TestClient

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import User, Workspace, WorkspaceViewer
from call_manager.main import create_app
from call_manager.proposal_assist.citations.ledger import (
    append_evidence,
    build_evidence_row,
    dump_ledger,
    empty_ledger,
)
from call_manager.proposal_assist.tools.citations import CITATIONS_FILENAME


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


def _repo_dir(tmp_path, wsid) -> Path:
    repo = tmp_path / str(wsid) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    return repo


def _write_ledger_with_one_row(tmp_path, wsid):
    """Write a valid v2 citations.json with a single evidence row into the
    workspace's repo_dir. Returns the row's display-relevant fields."""
    row = build_evidence_row(
        ev_id="ev-abc123def456",
        citekey="smith2021",
        reference_id="11111111-1111-1111-1111-111111111111",
        chunk_index=0,
        section_path="Introduction",
        source_content_hash=None,
        chunk_content_hash="deadbeef",
        evidence_quote="The treatment reduced mortality by 30%.",
        evidence_quote_offsets=[0, 39],
        stance="supports",
        confidence="high",
        claim_context_text="Our approach lowers mortality.",
        claim_context_hash="cafef00d",
        citation_snapshot="Smith 2021",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    ledger = append_evidence(empty_ledger(), row)
    repo = _repo_dir(tmp_path, wsid)
    (repo / CITATIONS_FILENAME).write_text(dump_ledger(ledger))
    return row


def test_citations_panel_owner_renders_row(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    _write_ledger_with_one_row(tmp_path, wsid)
    client = _authed(app, user_id=uid)
    try:
        resp = client.get(f"/workspaces/{wsid}/citations")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "smith2021" in body
    assert "Our approach lowers mortality." in body
    assert "The treatment reduced mortality by 30%." in body
    assert "supports" in body


def test_citations_panel_viewer_can_read(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        _, wsid = _seed_owner_and_workspace(s, email="cit-viewer-owner@ki.se")
        viewer = User(email="cit-viewer@ki.se")
        s.add(viewer)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=wsid, user_id=viewer.id))
        s.commit()
        viewer_id = viewer.id
    _write_ledger_with_one_row(tmp_path, wsid)
    client = _authed(app, user_id=viewer_id)
    try:
        resp = client.get(f"/workspaces/{wsid}/citations")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    assert "smith2021" in resp.text


def test_citations_panel_non_member_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        _, wsid = _seed_owner_and_workspace(s, email="cit-403-owner@ki.se")
        other = User(email="cit-403-other@ki.se")
        s.add(other)
        s.commit()
        other_id = other.id
    client = _authed(app, user_id=other_id)
    try:
        resp = client.get(f"/workspaces/{wsid}/citations")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403


def test_citations_panel_empty_state(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s, email="cit-empty@ki.se")
    # No citations.json written.
    client = _authed(app, user_id=uid)
    try:
        resp = client.get(f"/workspaces/{wsid}/citations")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    assert "No citations yet" in resp.text


def test_citations_panel_corrupt_ledger_shows_error_not_500(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s, email="cit-corrupt@ki.se")
    repo = _repo_dir(tmp_path, wsid)
    (repo / CITATIONS_FILENAME).write_text("{bad json")
    client = _authed(app, user_id=uid)
    try:
        resp = client.get(f"/workspaces/{wsid}/citations")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    # An error notice must be shown rather than a 500.
    assert "status-failed" in resp.text
