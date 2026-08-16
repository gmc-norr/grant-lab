"""GET /api/admin/workspaces/deleted/{id} — read a deleted workspace.

Five hazards, one test each:
  * the WRONG MUTEX. acquire_commit_mutex_live raises WorkspaceDeleted, which
    would refuse every workspace this endpoint exists to serve. And it is a
    plain call, not a context manager — `with acquire_commit_mutex(...)` raises
    AttributeError on every request.
  * a CONTENDED mutex must surface as the established 429 + Retry-After, not a
    500.
  * the endpoint must read HEAD, not the working tree. A workspace can be
    deleted with uncommitted edits on disk; the spec's contract is the
    committed draft.
  * a MISSING REPOSITORY is legitimate (deleted during setup) and must read as
    a state, not a crash.
  * the AUDIT EVENT must be committed before content is served, or a crash
    mid-response serves a draft with no record that anyone read it.
"""

import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from call_manager.db.models import User, Workspace, WorkspaceEvent
from call_manager.main import create_app
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import make_user, make_workspace


def _setup(monkeypatch, pg_url, tmp_path):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    # The route resolves repositories under this directory. Without it the test
    # reads the real configured workspaces_dir.
    app.state.settings.workspaces_dir = str(tmp_path)
    return app


def _client_for(app, sf, *, email, is_admin):
    with sf() as s:
        u = User(email=email, status="active", is_admin=is_admin)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid


def _delete(session, workspace_id):
    ws = session.get(Workspace, workspace_id)
    ws.soft_deleted_at = datetime.now(UTC)
    session.commit()


def _commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", message],
        cwd=repo,
        check=True,
    )


def _seed(sf, tmp_path, *, content="# Draft\n", n_commits=1, email=None):
    """Create a workspace row plus an initialised repo under tmp_path.

    Layout comes from `workspace_repo_dir` — do not hand-build a different one.
    Returns the workspace id.
    """
    with sf() as s:
        owner = make_user(s, email=email or f"o-{uuid.uuid4().hex[:8]}@ki.se")
        ws = make_workspace(s, owner=owner, title="Seeded")
        s.commit()
        ws_id = ws.id
    repo = workspace_repo_dir(Path(tmp_path), ws_id)
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    for i in range(n_commits):
        (repo / "proposal.md").write_text(content if i == n_commits - 1 else f"rev {i}\n")
        _commit(repo, f"commit {i}")
    return ws_id


def test_reads_a_deleted_workspace(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """The mutex test, two ways. `acquire_commit_mutex` returns None, so calling
    it in a `with` statement raises AttributeError; and the `_live` variant
    re-reads liveness and raises WorkspaceDeleted for exactly the workspaces
    this endpoint serves. Either mistake turns this 200 into a 500."""
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path, content="# Draft\n\nBody text.\n")
    with pg_session_factory() as s:
        _delete(s, ws_id)

    r = admin.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "Body text." in body["proposal_markdown"]
    assert body["repository_available"] is True
    assert len(body["commits"]) >= 1


def test_a_contended_mutex_is_429_with_retry_after(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """CommitMutexTimeout is raised by acquire_commit_mutex when lock_timeout
    fires. Unhandled it becomes a 500. Every other mutex caller in this codebase
    maps it to 429 + Retry-After: 2 with BUSY_ERROR_MESSAGE (see
    web/api/workspaces.py:444) and this must match, or the SPA's busy handling
    does not apply to admin routes."""
    from call_manager.proposal_assist.drafting.locking import CommitMutexTimeout
    from call_manager.web.api import admin_retention

    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        _delete(s, ws_id)

    def _timeout(*a, **k):
        raise CommitMutexTimeout("held by another writer")

    monkeypatch.setattr(admin_retention, "acquire_commit_mutex", _timeout)

    r = admin.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert r.status_code == 429, r.text
    assert r.headers["Retry-After"] == "2"


def test_serves_the_committed_draft_not_the_working_tree(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Spec 3.2: "proposal markdown at HEAD". A workspace can be deleted while a
    collab session had written proposal.md without committing, so the working
    tree and HEAD legitimately differ. `read_text()` on the file would serve
    whichever bytes happened to be on disk — content that was never part of the
    proposal's history and that the commit list does not explain."""
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path, content="# Committed\n")
    repo = workspace_repo_dir(Path(tmp_path), ws_id)
    (repo / "proposal.md").write_text("# UNCOMMITTED SCRATCH\n")
    with pg_session_factory() as s:
        _delete(s, ws_id)

    body = admin.get(f"/api/admin/workspaces/deleted/{ws_id}").json()
    assert body["proposal_markdown"] == "# Committed\n"
    assert "UNCOMMITTED" not in body["proposal_markdown"]


def test_live_workspace_is_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path, content="# Live\n")
    r = admin.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert r.status_code == 404


def test_non_admin_is_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _setup(monkeypatch, pg_url, tmp_path)
    member, _ = _client_for(app, pg_session_factory, email="member@ki.se", is_admin=False)
    ws_id = _seed(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        _delete(s, ws_id)
    r = member.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert r.status_code == 403


def test_missing_repository_is_a_state_not_an_error(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """A workspace deleted before setup created a repo. Legitimate, not a bug."""
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, admin_id = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    with pg_session_factory() as s:
        owner = make_user(s, email="norepo@ki.se")
        ws = make_workspace(s, owner=owner, title="never set up")
        s.commit()
        ws_id = ws.id
        _delete(s, ws_id)

    r = admin.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert r.status_code == 200, "a missing repo is a state of the workspace, not a failure"
    body = r.json()
    assert body["repository_available"] is False
    assert body["proposal_markdown"] is None
    assert body["commits"] == []

    # The unavailable case is audited too: "an admin opened this record" is the
    # fact being traced, and it is true whether or not bytes came back.
    with pg_session_factory() as s:
        events = (
            s.query(WorkspaceEvent)
            .filter_by(workspace_id=ws_id, kind="admin_deleted_workspace_inspected")
            .all()
        )
    assert len(events) == 1
    assert events[0].payload["content_served"] is False
    assert events[0].payload["commit_count"] == 0


def test_inspection_writes_exactly_one_audit_event_naming_the_admin(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Spec 3.3 fixes the payload: workspace_id, admin_user_id, whether content
    was served, commit count, truncation flag. `actor_id is not None` would pass
    with the wrong admin, a duplicate event, or an empty payload."""
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, admin_id = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    other, other_id = _client_for(app, pg_session_factory, email="other@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path, n_commits=2)
    with pg_session_factory() as s:
        _delete(s, ws_id)

    assert admin.get(f"/api/admin/workspaces/deleted/{ws_id}").status_code == 200

    with pg_session_factory() as s:
        events = (
            s.query(WorkspaceEvent)
            .filter_by(workspace_id=ws_id, kind="admin_deleted_workspace_inspected")
            .all()
        )
    assert len(events) == 1, "one read must leave one row, not zero and not two"
    ev = events[0]
    assert ev.actor_id == admin_id, "the trace names the wrong admin"
    assert ev.actor_id != other_id
    assert ev.payload == {
        "workspace_id": str(ws_id),
        "admin_user_id": admin_id,
        "content_served": True,
        "commit_count": 2,
        "history_truncated": False,
    }


def test_a_second_read_appends_a_second_event(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """The trace is per-read, not per-workspace. An implementation that
    de-duplicated would hide repeat access to someone's draft."""
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        _delete(s, ws_id)

    admin.get(f"/api/admin/workspaces/deleted/{ws_id}")
    admin.get(f"/api/admin/workspaces/deleted/{ws_id}")

    with pg_session_factory() as s:
        n = (
            s.query(WorkspaceEvent)
            .filter_by(workspace_id=ws_id, kind="admin_deleted_workspace_inspected")
            .count()
        )
    assert n == 2


def test_no_content_is_served_when_the_audit_write_fails(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """The ordering guarantee. If the audit cannot be recorded, the draft must
    not be served — otherwise a crash mid-response leaves content read with no
    trace that anyone read it."""
    from call_manager.web.api import admin_retention

    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path, content="# Secret draft\n")
    with pg_session_factory() as s:
        _delete(s, ws_id)

    def _boom(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(admin_retention, "_record_inspection", _boom)

    with pytest.raises(RuntimeError, match="audit write failed"):
        # TestClient re-raises server exceptions by default; that IS the
        # assertion — no response body was produced, so no content escaped.
        admin.get(f"/api/admin/workspaces/deleted/{ws_id}")


def test_inspection_starts_no_collab_room(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """A deleted workspace has no live collab room and must not acquire one.
    Inspection that spun up a room would resurrect machinery deletion tore
    down, and Slice E's purge would then have a reader nothing knows about."""
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        _delete(s, ws_id)

    # The room manager is a module-level singleton (collab/room.py:776), NOT an
    # app.state attribute — `app.state.collab_manager` does not exist. This is
    # how tests/proposal_assist/test_workspaces_delete_route.py:58 reaches it.
    from call_manager.proposal_assist.collab.room import MANAGER

    before = len(MANAGER._rooms)
    admin.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert len(MANAGER._rooms) == before, "inspection must not create a collab room"


def test_truncated_history_is_reported_to_the_caller(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """A capped list presented as a complete one is a lie the admin cannot
    detect. The flag is the only thing that distinguishes them."""
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path, n_commits=205)
    with pg_session_factory() as s:
        _delete(s, ws_id)

    body = admin.get(f"/api/admin/workspaces/deleted/{ws_id}").json()
    assert len(body["commits"]) == 200
    assert body["history_truncated"] is True


def test_untruncated_history_says_so(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path, n_commits=3)
    with pg_session_factory() as s:
        _delete(s, ws_id)

    body = admin.get(f"/api/admin/workspaces/deleted/{ws_id}").json()
    assert body["history_truncated"] is False


def test_inspection_creates_no_commit(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """Reading must not write. A new commit would rewrite the history an admin
    is inspecting."""
    from call_manager.proposal_assist.citations.gitio import current_head

    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    ws_id = _seed(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        _delete(s, ws_id)

    repo = workspace_repo_dir(Path(tmp_path), ws_id)
    before = current_head(repo)
    admin.get(f"/api/admin/workspaces/deleted/{ws_id}")
    assert current_head(repo) == before
