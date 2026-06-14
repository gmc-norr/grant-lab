"""Citations web view (Task 3 — human-facing evidence-ledger panel, read-only)."""

from pathlib import Path

from fastapi.testclient import TestClient

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Reference, User, Workspace, WorkspaceViewer
from call_manager.main import create_app
from call_manager.proposal_assist.citations.ledger import (
    append_evidence,
    build_evidence_row,
    dump_ledger,
    empty_ledger,
)
from call_manager.proposal_assist.citations.markers import build_marker
from call_manager.proposal_assist.tools.citations import CITATIONS_FILENAME
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME


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
    owner = User(status="active", email=email)
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
        viewer = User(status="active", email="cit-viewer@ki.se")
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
        other = User(status="active", email="cit-403-other@ki.se")
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


# ---------------------------------------------------------------------------
# Task 4: POST /workspaces/{id}/citations/validate — deterministic audit panel
# ---------------------------------------------------------------------------


def test_validate_citations_clean_ledger_no_issues(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Owner POST with empty ledger (no citations) → 200 fragment saying no issues."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s, email="val-clean@ki.se")
    # Empty ledger (no citations.json written at all)
    client = _authed(app, user_id=uid)
    try:
        resp = client.post(f"/workspaces/{wsid}/citations/validate")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    body = resp.text
    # Must be a fragment — no full HTML page wrapper
    assert "<html" not in body
    assert "<body" not in body
    # Should report no issues
    assert "No issues found" in body or "0 issue" in body


def test_validate_citations_fragment_has_semantic_note(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Fragment must contain the note about semantic checks being agent-driven."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s, email="val-note@ki.se")
    client = _authed(app, user_id=uid)
    try:
        resp = client.post(f"/workspaces/{wsid}/citations/validate")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    # Fragment must contain the semantic-audit note
    assert "Semantic" in resp.text or "semantic" in resp.text


def test_validate_citations_orphan_row_issue(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """Ledger row referencing a non-existent reference_id → deterministic emits
    orphan_row (no inline marker in proposal.md) and/or reference_missing issues.
    Either way the fragment must surface the issue type and severity."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s, email="val-orphan@ki.se")

    # Write a ledger with a row that has no matching marker in proposal.md
    # (proposal.md doesn't exist → treat as empty text → all rows are orphan_row).
    row = build_evidence_row(
        ev_id="ev-orphan000001",
        citekey="ghost2024",
        reference_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        chunk_index=0,
        section_path=None,
        source_content_hash=None,
        chunk_content_hash="deadbeef00",
        evidence_quote="Some text.",
        evidence_quote_offsets=[0, 9],
        stance="supports",
        confidence="high",
        claim_context_text="We claim something.",
        claim_context_hash="00cafef00d",
        citation_snapshot="Ghost 2024",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    ledger = append_evidence(empty_ledger(), row)
    repo = _repo_dir(tmp_path, wsid)
    (repo / CITATIONS_FILENAME).write_text(dump_ledger(ledger))
    # No proposal.md → empty proposal text → orphan_row for the ledger row

    client = _authed(app, user_id=uid)
    try:
        resp = client.post(f"/workspaces/{wsid}/citations/validate")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "<html" not in body
    # The issue type and/or severity must appear in the fragment
    assert "orphan_row" in body
    assert "error" in body


def test_validate_citations_corrupt_ledger_returns_error_fragment_not_500(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Corrupt citations.json → 200 error fragment (not 500)."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s, email="val-corr@ki.se")
    repo = _repo_dir(tmp_path, wsid)
    (repo / CITATIONS_FILENAME).write_text("{this is not valid json at all")
    client = _authed(app, user_id=uid)
    try:
        resp = client.post(f"/workspaces/{wsid}/citations/validate")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "<html" not in body
    # Must show an error message rather than crashing
    assert "error" in body.lower() or "could not" in body.lower()


def test_validate_citations_non_member_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """Non-member POST → 403."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        _, wsid = _seed_owner_and_workspace(s, email="val-403-owner@ki.se")
        other = User(status="active", email="val-403-other@ki.se")
        s.add(other)
        s.commit()
        other_id = other.id
    client = _authed(app, user_id=other_id)
    try:
        resp = client.post(f"/workspaces/{wsid}/citations/validate")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403


def test_validate_citations_viewer_allowed(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """Viewer POST → 200 (read-only validation is allowed for viewers)."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        _, wsid = _seed_owner_and_workspace(s, email="val-viewer-owner@ki.se")
        viewer = User(status="active", email="val-viewer@ki.se")
        s.add(viewer)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=wsid, user_id=viewer.id))
        s.commit()
        viewer_id = viewer.id
    client = _authed(app, user_id=viewer_id)
    try:
        resp = client.post(f"/workspaces/{wsid}/citations/validate")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "<html" not in body


# ---------------------------------------------------------------------------
# Task 6: GET /workspaces/{id}/export-composed.md — inline citations + bib
# ---------------------------------------------------------------------------

_SMITH_CSL = {
    "author": [{"family": "Smith", "given": "Jane"}],
    "title": "Adjuvant therapy outcomes",
    "container-title": "Journal of Oncology",
    "issued": {"date-parts": [[2021]]},
}


def _seed_composed_workspace(session, tmp_path, *, email, metadata_status="approved"):
    """Owner + workspace + one approved Reference + proposal.md/citations.json
    wired together by a single citekey marker. Returns (owner_id, wsid)."""
    owner = User(status="active", email=email)
    session.add(owner)
    session.flush()
    ws = Workspace(owner_id=owner.id, title="T", citation_style="vancouver")
    session.add(ws)
    session.flush()
    ref = Reference(
        workspace_id=ws.id,
        kind="paper",
        source_type="uploaded",
        filename="seed.pdf",
        storage_path="seed/seed.pdf",
        mime_type="application/pdf",
        citekey="smith2021",
        csl_json=_SMITH_CSL,
        metadata_status=metadata_status,
    )
    session.add(ref)
    session.flush()
    rid, wsid, oid = ref.id, ws.id, owner.id
    session.commit()

    repo = _repo_dir(tmp_path, wsid)
    marker = build_marker("smith2021", "ev-aaaaaaaaaaaa")
    (repo / PROPOSAL_FILENAME).write_text(f"# P\n\nWe argue {marker} here.\n")
    row = build_evidence_row(
        ev_id="ev-aaaaaaaaaaaa",
        citekey="smith2021",
        reference_id=str(rid),
        chunk_index=0,
        section_path="Intro",
        source_content_hash=None,
        chunk_content_hash="deadbeef",
        evidence_quote="x",
        evidence_quote_offsets=[0, 1],
        stance="supports",
        confidence="high",
        claim_context_text="We argue.",
        claim_context_hash="cafef00d",
        citation_snapshot="Smith 2021",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    (repo / CITATIONS_FILENAME).write_text(dump_ledger(append_evidence(empty_ledger(), row)))
    return oid, wsid


def test_export_composed_renders_inline_and_bibliography(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_composed_workspace(s, tmp_path, email="exp-ok@ki.se")
    client = _authed(app, user_id=uid)
    try:
        resp = client.get(f"/workspaces/{wsid}/export-composed.md")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "-composed.md" in resp.headers.get("content-disposition", "")
    body = resp.text
    # Markers transformed, no leak.
    assert "[@" not in body
    assert "<!-- ev:" not in body
    # Inline vancouver number + appended bibliography.
    assert "[1]" in body
    assert "# References" in body
    assert "Smith" in body


def test_export_composed_blocked_when_unapproved(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_composed_workspace(
            s, tmp_path, email="exp-blocked@ki.se", metadata_status="pending"
        )
    client = _authed(app, user_id=uid)
    try:
        resp = client.get(f"/workspaces/{wsid}/export-composed.md")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 409, resp.text
    # The blocked citekey + reason are surfaced as HTML; no half-doc served.
    body = resp.text
    assert "Export blocked" in body
    assert "smith2021" in body
    assert "metadata_unapproved" in body
    assert "[@" not in body


def test_export_composed_corrupt_ledger_409(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s, email="exp-corrupt@ki.se")
    repo = _repo_dir(tmp_path, wsid)
    (repo / PROPOSAL_FILENAME).write_text("# P\n")
    (repo / CITATIONS_FILENAME).write_text("{not valid json")
    client = _authed(app, user_id=uid)
    try:
        resp = client.get(f"/workspaces/{wsid}/export-composed.md")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 409, resp.text
    assert "could not be read" in resp.text


def test_export_composed_non_member_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        _, wsid = _seed_composed_workspace(s, tmp_path, email="exp-403-owner@ki.se")
        other = User(status="active", email="exp-403-other@ki.se")
        s.add(other)
        s.commit()
        other_id = other.id
    client = _authed(app, user_id=other_id)
    try:
        resp = client.get(f"/workspaces/{wsid}/export-composed.md")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403


def test_export_composed_no_proposal_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s, email="exp-404@ki.se")
    client = _authed(app, user_id=uid)
    try:
        resp = client.get(f"/workspaces/{wsid}/export-composed.md")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 404
