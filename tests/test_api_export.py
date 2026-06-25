"""JSON /api Markdown export endpoints (Cockpit Slice F)."""

from fastapi.testclient import TestClient

from call_manager.db.models import Reference, User, Workspace, WorkspaceViewer
from call_manager.main import create_app
from call_manager.proposal_assist.citations.ledger import dump_ledger, empty_ledger
from call_manager.proposal_assist.paths import workspace_repo_dir


def _app(monkeypatch, pg_url, tmp_path):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    app.state.settings.workspaces_dir = str(tmp_path)
    return app


def _seed(pg_session_factory):
    """owner + viewer + stranger + active workspace. Returns ids + ws_id."""
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        viewer = User(status="active", email="viewer@ki.se")
        stranger = User(status="active", email="stranger@ki.se")
        s.add_all([owner, viewer, stranger])
        s.flush()
        ws = Workspace(title="My Proposal", owner_id=owner.id, current_phase="active")
        s.add(ws)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=viewer.id))
        s.commit()
        return owner.id, viewer.id, stranger.id, ws.id


def _write_repo(tmp_path, ws_id, *, proposal=None, citations=None):
    repo = workspace_repo_dir(tmp_path, ws_id)
    repo.mkdir(parents=True, exist_ok=True)
    if proposal is not None:
        (repo / "proposal.md").write_text(proposal)
    if citations is not None:
        (repo / "citations.json").write_text(citations)
    return repo


def _client(app, user_id):
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=user_id))
    return c


def test_export_markdown_returns_attachment(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(pg_session_factory)
    _write_repo(tmp_path, ws_id, proposal="# Draft\n\nhello world\n")
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/export.md")
    assert r.status_code == 200, r.text
    assert r.text == "# Draft\n\nhello world\n"
    assert 'filename="My_Proposal.md"' in r.headers["content-disposition"]


def test_export_markdown_no_draft_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(pg_session_factory)
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/export.md")
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "not_found"
    # Assert the ROUTE's own message: an UNMOUNTED /api/* 404 says "Not Found", so this
    # makes the test genuinely fail-before-mount (adversarial-review finding).
    assert body["error"]["message"] == "no draft to export yet"


def test_export_markdown_viewer_ok_stranger_forbidden(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, viewer_id, stranger_id, ws_id = _seed(pg_session_factory)
    _write_repo(tmp_path, ws_id, proposal="x\n")
    assert _client(app, viewer_id).get(f"/api/workspaces/{ws_id}/export.md").status_code == 200
    assert _client(app, stranger_id).get(f"/api/workspaces/{ws_id}/export.md").status_code == 403


def test_export_composed_no_citations_ok(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(pg_session_factory)
    _write_repo(
        tmp_path, ws_id, proposal="Hello, no citations.\n", citations=dump_ledger(empty_ledger())
    )
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/export-composed.md")
    assert r.status_code == 200, r.text
    assert "Hello, no citations." in r.text
    assert 'filename="My_Proposal-composed.md"' in r.headers["content-disposition"]


def test_export_composed_corrupt_ledger_409(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(pg_session_factory)
    _write_repo(tmp_path, ws_id, proposal="x\n", citations="{ not valid json")
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/export-composed.md")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "citation_ledger_invalid"


def test_export_composed_blocked_lists_refs(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(pg_session_factory)
    # Seed an UN-approved reference cited by the draft → build_bibliography raises Blocked.
    with pg_session_factory() as s:
        ref = Reference(
            workspace_id=ws_id,
            kind="paper",
            source_type="uploaded",
            filename="seed.pdf",
            storage_path="seed/seed.pdf",
            mime_type="application/pdf",
            citekey="smith2021",
            metadata_status="pending",
        )
        s.add(ref)
        s.flush()
        rid = str(ref.id)
        s.commit()
    ledger = empty_ledger()
    ledger["evidence"].append(
        {"id": "ev-aaaaaaaaaaaa", "citekey": "smith2021", "reference_id": rid}
    )
    _write_repo(
        tmp_path,
        ws_id,
        proposal="We argue [@smith2021]<!-- ev:ev-aaaaaaaaaaaa --> that...\n",
        citations=dump_ledger(ledger),
    )
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/export-composed.md")
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["error"]["code"] == "export_blocked"
    blocked = body["error"]["detail"]["blocked"]
    assert any(b["citekey"] == "smith2021" for b in blocked)
