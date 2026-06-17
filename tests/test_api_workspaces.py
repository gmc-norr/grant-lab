"""SP2a-3 cockpit JSON surface tests (GET /draft + POST /sections/{index}).

Repo layout is the PRODUCTION one (<workspaces_dir>/<workspace_id>/repo): we reuse
`_init_repo` from tests/test_editing_save.py (same pattern as tests/test_web_sections.py).
That helper does `git init -q` (NOT `git init -b main`, which needs git >= 2.28 and
would break collection on older CI/dev git) + an explicit `_git_env()` author env,
and writes+commits proposal.md at `workspace_repo_dir(tmp_path, ws_id)`.
"""

from __future__ import annotations

import subprocess
import uuid

from fastapi.testclient import TestClient

from call_manager.db.models import Reference, User, Workspace, WorkspaceViewer
from call_manager.main import create_app
from call_manager.proposal_assist.citations.ledger import dump_ledger, empty_ledger
from call_manager.proposal_assist.editing.save import CITATIONS_FILENAME
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.test_editing_save import _init_repo


def _app(monkeypatch, pg_url, tmp_path):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    # Redirect the workspace filesystem root at the per-test tmp dir so
    # workspace_repo_dir(settings.workspaces_dir, ws.id) == tmp_path/<ws.id>/repo.
    app.state.settings.workspaces_dir = str(tmp_path)
    return app


def _seed(app, pg_session_factory, tmp_path, content):
    """Create owner + member + a workspace whose repo holds `content`.

    NOTE: we do NOT override `app.state.session_factory` — create_app() already
    binds it from DATABASE_URL=pg_url (the migrated testcontainers engine), and the
    request path (get_current_user, the handlers) reads `app.state.session_factory`.
    `pg_session_factory` rides the SAME engine, so seeding through it and serving
    through the app's own factory both hit one Postgres. This matches the house
    convention in tests/test_api_calls.py / tests/test_api_errors.py.
    """
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        member = User(status="active", email="member@ki.se")
        stranger = User(status="active", email="stranger@ki.se")
        s.add_all([owner, member, stranger])
        s.flush()
        ws = Workspace(title="W", owner_id=owner.id, current_phase="spec")
        s.add(ws)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=member.id))
        s.commit()
        owner_id, member_id, stranger_id, ws_id = owner.id, member.id, stranger.id, ws.id
    # _init_repo builds <tmp_path>/<ws_id>/repo with proposal.md committed at the
    # production layout (workspace_repo_dir(tmp_path, ws_id)).
    _init_repo(tmp_path, ws_id, doc=content)
    return owner_id, member_id, stranger_id, ws_id


def _client(app, user_id):
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=user_id))
    return c


def test_draft_returns_server_split_sections_and_head(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/draft")
    assert r.status_code == 200
    body = r.json()
    assert body["can_edit"] is True
    assert isinstance(body["head"], str) and len(body["head"]) == 40
    # Server split: preamble (index 0) + Methods (index 1). Byte round-trip.
    sections = body["sections"]
    assert [s["index"] for s in sections] == [0, 1]
    assert sections[0]["heading"] == ""
    assert sections[1]["heading"] == "Methods"
    assert "".join(s["text"] for s in sections) == "preamble\n\n## Methods\noriginal.\n"
    # sha is sha256 of the section text bytes (64 hex chars).
    assert all(len(s["sha"]) == 64 for s in sections)
    assert body["citations"] == {}


def test_draft_member_can_read_but_can_edit_false(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, member_id, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    c = _client(app, member_id)
    r = c.get(f"/api/workspaces/{ws_id}/draft")
    assert r.status_code == 200
    assert r.json()["can_edit"] is False


def test_draft_non_member_is_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, _, stranger_id, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    c = _client(app, stranger_id)
    r = c.get(f"/api/workspaces/{ws_id}/draft")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_draft_missing_workspace_is_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, _ = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{uuid.uuid4()}/draft")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_draft_no_proposal_file_is_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """The workspace EXISTS and the caller is the owner (so require_member passes),
    but there is no proposal.md yet. This pins the 'no proposal.md yet' branch
    DISTINCTLY from the membership 404 above — a regression that dropped the
    `.exists()` check would otherwise stay green."""
    app = _app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(title="W", owner_id=owner.id, current_phase="spec")
        s.add(ws)
        s.commit()
        owner_id, ws_id = owner.id, ws.id
    # Deliberately do NOT init the repo / write proposal.md.
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/draft")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_draft_folds_whole_proposal_citation_map(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """The headline value of /draft: the citation map is computed over the WHOLE
    proposal (build_citation_numbering) and joined to the Reference title via the
    ledger. Seeds a real full marker + a valid citations.json + a Reference row,
    then asserts ev_id -> {citekey, number, stance, title}."""
    app = _app(monkeypatch, pg_url, tmp_path)
    ev_id = "ev-abc123def456"  # production length: 12 hex (new_ev_id = ev-<uuid.hex[:12]>)
    proposal = f"preamble\n\n## Methods\nSee [@smith2021]<!-- ev:{ev_id} --> here.\n"
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(title="W", owner_id=owner.id, current_phase="spec")
        s.add(ws)
        s.flush()
        ref = Reference(
            workspace_id=ws.id,
            kind="paper",
            source_type="uploaded",
            title="The Smith 2021 Study",
            filename="smith2021.pdf",
            storage_path="refs/smith2021.pdf",
            mime_type="application/pdf",
        )
        s.add(ref)
        s.commit()
        owner_id, ws_id, ref_id = owner.id, ws.id, ref.id
    # Seed the repo with the marker-bearing proposal.
    _init_repo(tmp_path, ws_id, doc=proposal)
    # Add a valid v2 ledger whose evidence row joins the marker -> the Reference.
    repo = workspace_repo_dir(tmp_path, ws_id)
    ledger = empty_ledger()
    ledger["evidence"].append(
        {
            "id": ev_id,
            "citekey": "smith2021",
            "reference_id": str(ref_id),
            "stance": "supports",
        }
    )
    (repo / CITATIONS_FILENAME).write_text(dump_ledger(ledger))
    from call_manager.proposal_assist.citations.gitio import _git_env

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=_git_env())
    subprocess.run(["git", "commit", "-qm", "ledger"], cwd=repo, check=True, env=_git_env())

    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/draft")
    assert r.status_code == 200, r.text
    cites = r.json()["citations"]
    assert ev_id in cites
    assert cites[ev_id]["citekey"] == "smith2021"
    assert cites[ev_id]["number"] == 1  # first (and only) citekey numbered over the whole doc
    assert cites[ev_id]["stance"] == "supports"
    assert cites[ev_id]["title"] == "The Smith 2021 Study"
