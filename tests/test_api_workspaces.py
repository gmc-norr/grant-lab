"""SP2a-3 cockpit JSON surface tests (GET /draft + POST /sections/{index}).

Repo layout is the PRODUCTION one (<workspaces_dir>/<workspace_id>/repo): we reuse
`_init_repo` from tests/test_editing_save.py (same pattern as tests/test_web_sections.py).
That helper does `git init -q` (NOT `git init -b main`, which needs git >= 2.28 and
would break collection on older CI/dev git) + an explicit `_git_env()` author env,
and writes+commits proposal.md at `workspace_repo_dir(tmp_path, ws_id)`.
"""

from __future__ import annotations

import subprocess
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from call_manager.db.models import (
    ArtifactVersion,
    Call,
    Profile,
    ProfileKind,
    Reference,
    Source,
    User,
    Workspace,
    WorkspaceSetupJob,
    WorkspaceViewer,
)
from call_manager.main import create_app
from call_manager.proposal_assist import row_locks
from call_manager.proposal_assist.artifacts import SPEC_FILENAME
from call_manager.proposal_assist.citations.gitio import _git_env
from call_manager.proposal_assist.citations.ledger import dump_ledger, empty_ledger
from call_manager.proposal_assist.editing.save import CITATIONS_FILENAME, PROPOSAL_FILENAME
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import stamp_for
from tests.test_editing_save import _init_repo

_CREATED_APPS = []


@pytest.fixture(autouse=True)
def _dispose_created_app_engines():
    yield
    while _CREATED_APPS:
        _CREATED_APPS.pop().state.engine.dispose()


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
    _CREATED_APPS.append(app)
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
        ws = Workspace(title="W", owner_id=owner.id, current_phase="active")
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
    # The fixture is deliberately in the ProseMirror-canonical ("loose") form
    # (blank line after the heading) post-3b-5d.D4: the first GET enable-on-touch
    # flips draft collab on and serves the room-canonical projection, and canonical
    # input is a round-trip fixed point — so the original intent (server split +
    # byte round-trip through the read model) holds verbatim in the enabled world.
    owner_id, _, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\n\noriginal.\n"
    )
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/draft")
    assert r.status_code == 200
    body = r.json()
    assert body["branch"] == "main"
    assert body["can_edit"] is True
    assert isinstance(body["head"], str) and len(body["head"]) == 40
    # Server split: preamble (index 0) + Methods (index 1). Byte round-trip.
    sections = body["sections"]
    assert [s["index"] for s in sections] == [0, 1]
    assert sections[0]["heading"] == ""
    assert sections[1]["heading"] == "Methods"
    assert "".join(s["text"] for s in sections) == "preamble\n\n## Methods\n\noriginal.\n"
    # sha is sha256 of the section text bytes (64 hex chars).
    assert all(len(s["sha"]) == 64 for s in sections)
    assert body["citations"] == {}


@pytest.mark.parametrize(
    ("artifact", "filename"),
    [("draft", PROPOSAL_FILENAME), ("spec", SPEC_FILENAME)],
)
def test_artifact_read_waits_for_an_uncommitted_branch_transition(
    monkeypatch, pg_url, pg_session_factory, tmp_path, artifact, filename
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "## Aims\n\nmain.\n")
    repo = workspace_repo_dir(str(tmp_path), ws_id)
    if artifact == "spec":
        (repo / SPEC_FILENAME).write_text("## Aims\n\nmain.\n")
        subprocess.run(["git", "add", SPEC_FILENAME], cwd=repo, check=True, env=_git_env())
        subprocess.run(["git", "commit", "-qm", "main spec"], cwd=repo, check=True, env=_git_env())
    subprocess.run(["git", "checkout", "-qb", "v2"], cwd=repo, check=True, env=_git_env())
    (repo / filename).write_text("## Aims\n\ntarget.\n")
    subprocess.run(["git", "commit", "-qam", "target"], cwd=repo, check=True, env=_git_env())
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True, env=_git_env())

    lock_attempted = threading.Event()
    finished = threading.Event()
    result: dict[str, object] = {}
    real_refresh = Session.refresh

    def tracked_refresh(self, instance, *args, **kwargs):
        if isinstance(instance, Workspace) and kwargs.get("with_for_update") == {"read": True}:
            lock_attempted.set()
        return real_refresh(self, instance, *args, **kwargs)

    monkeypatch.setattr(Session, "refresh", tracked_refresh)

    def read_artifact():
        client = _client(app, owner_id)
        try:
            result["response"] = client.get(f"/api/workspaces/{ws_id}/{artifact}")
        except BaseException as exc:  # pragma: no cover - asserted below
            result["error"] = exc
        finally:
            client.close()
            finished.set()

    with pg_session_factory() as switching:
        switching.execute(
            update(Workspace).where(Workspace.id == ws_id).values(current_branch="v2")
        )
        thread = threading.Thread(target=read_artifact)
        thread.start()
        assert lock_attempted.wait(timeout=5), "artifact read never requested the share lock"
        assert not finished.is_set(), "artifact read crossed an uncommitted branch transition"
        switching.commit()
        thread.join(timeout=20)

    assert finished.is_set(), "artifact read stayed blocked after the transition committed"
    assert "error" not in result
    response = result["response"]
    assert response.status_code == 200
    body = response.json()
    assert body["branch"] == "v2"
    assert "target." in "".join(section["text"] for section in body["sections"])
    app.state.engine.dispose()


def test_artifact_snapshot_lock_timeout_is_retryable(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "## Aims\n\nmain.\n")
    monkeypatch.setattr(row_locks, "WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS", 0.05)

    with pg_session_factory() as holding:
        holding.execute(update(Workspace).where(Workspace.id == ws_id).values(title="locked"))
        client = _client(app, owner_id)
        response = client.get(f"/api/workspaces/{ws_id}/draft")
        client.close()

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "2"
    app.state.engine.dispose()


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
        ws = Workspace(title="W", owner_id=owner.id, current_phase="active")
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
        ws = Workspace(title="W", owner_id=owner.id, current_phase="active")
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


def test_draft_does_not_leak_cross_workspace_reference_title(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Security branch: a ledger reference_id that points at a Reference owned by a
    DIFFERENT workspace must NOT leak that other workspace's title. The ownership
    guard (ref.workspace_id == workspace_id) forces title=None, while citekey/number/
    stance still resolve from the marker + ledger. Workspace A is the one being read
    (owns the proposal/marker); workspace B owns the Reference the ledger points to."""
    app = _app(monkeypatch, pg_url, tmp_path)
    ev_id = "ev-abc123def456"  # production length: 12 hex (new_ev_id = ev-<uuid.hex[:12]>)
    proposal = f"preamble\n\n## Methods\nSee [@smith2021]<!-- ev:{ev_id} --> here.\n"
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        # Workspace A: the one being read (has the proposal + marker).
        ws_a = Workspace(title="A", owner_id=owner.id, current_phase="active")
        # Workspace B: owns the Reference the ledger's reference_id points to.
        ws_b = Workspace(title="B", owner_id=owner.id, current_phase="active")
        s.add_all([ws_a, ws_b])
        s.flush()
        # The Reference belongs to workspace B, NOT workspace A.
        ref = Reference(
            workspace_id=ws_b.id,
            kind="paper",
            source_type="uploaded",
            title="Cross-Workspace Secret Title",
            filename="smith2021.pdf",
            storage_path="refs/smith2021.pdf",
            mime_type="application/pdf",
        )
        s.add(ref)
        s.commit()
        owner_id, ws_a_id, ref_id = owner.id, ws_a.id, ref.id
    # Seed workspace A's repo with the marker-bearing proposal.
    _init_repo(tmp_path, ws_a_id, doc=proposal)
    # Ledger lives in workspace A, but its evidence row's reference_id points at
    # workspace B's Reference — the ownership guard must refuse to join the title.
    repo = workspace_repo_dir(tmp_path, ws_a_id)
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
    r = c.get(f"/api/workspaces/{ws_a_id}/draft")
    assert r.status_code == 200, r.text
    cites = r.json()["citations"]
    assert ev_id in cites
    assert cites[ev_id]["citekey"] == "smith2021"
    assert cites[ev_id]["number"] == 1
    assert cites[ev_id]["stance"] == "supports"
    # The cross-workspace Reference title must NOT be leaked.
    assert cites[ev_id]["title"] is None


def test_draft_collab_returns_room_canonical_sections(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    from call_manager.proposal_assist.collab import state, store
    from call_manager.proposal_assist.collab.hydrate import build_doc, load_or_hydrate
    from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
    from call_manager.proposal_assist.collab.yjs_bridge import apply_pm_json_to_fragment

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _m, _s, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=ws_id, artifact="draft")
        s.commit()
    # Hydrate then apply a LIVE, uncheckpointed edit to the room (git stays "original.").
    with pg_session_factory() as s:
        updates = load_or_hydrate(
            s,
            stamp=stamp_for(s, ws_id),
            workspace_id=ws_id,
            artifact="draft",
            workspaces_dir=str(tmp_path),
        )
        s.commit()
        room = build_doc(updates)
        upd = apply_pm_json_to_fragment(
            room, markdown_to_pm("preamble\n\n## Methods\nLIVE EDITED.\n")
        )
        store.append_update(
            s, workspace_id=ws_id, artifact="draft", actor_id=owner_id, update=upd, commit=True
        )
    body = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/draft").json()
    joined = "".join(sec["text"] for sec in body["sections"])
    assert "LIVE EDITED." in joined  # room-canonical, NOT git "original."
    assert "original." not in joined


from call_manager.proposal_assist.proposals.proposing import capture_base  # noqa: E402


def _capture(tmp_path, ws_id, index):
    repo = workspace_repo_dir(tmp_path, ws_id)
    return capture_base(repo, index)


def test_save_section_happy_path_commits_and_changes(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    base_commit, base_section = _capture(tmp_path, ws_id, 1)
    c = _client(app, owner_id)
    r = c.post(
        f"/api/workspaces/{ws_id}/sections/1",
        json={
            "content": "## Methods\nrewritten body.\n",
            "base_commit_sha": base_commit,
            "base_section_sha": base_section,
            "base_branch": "main",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True
    assert isinstance(body["commit_sha"], str) and len(body["commit_sha"]) == 40
    assert body["commit_sha"] != base_commit  # a new commit was made
    # The SPA's post-save anti-clobber base: head == the new commit, and
    # section_sha == sha256 of the just-saved section bytes (64 hex).
    assert body["head"] == body["commit_sha"]
    assert isinstance(body["section_sha"], str) and len(body["section_sha"]) == 64
    assert body["section_sha"] != base_section  # the section bytes changed
    assert isinstance(body["issues"], list)
    assert body["job_enqueued"] in (True, False)
    # The bytes actually landed.
    saved = (workspace_repo_dir(tmp_path, ws_id) / PROPOSAL_FILENAME).read_text()
    assert saved == "preamble\n\n## Methods\nrewritten body.\n"


def test_save_section_no_op_returns_changed_false(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    base_commit, base_section = _capture(tmp_path, ws_id, 1)
    c = _client(app, owner_id)
    r = c.post(
        f"/api/workspaces/{ws_id}/sections/1",
        json={
            "content": "## Methods\noriginal.\n",  # identical to live
            "base_commit_sha": base_commit,
            "base_section_sha": base_section,
            "base_branch": "main",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is False
    assert body["commit_sha"] == base_commit  # head unchanged
    assert body["head"] == base_commit  # explicit alias also unchanged
    assert body["section_sha"] == base_section  # base sha preserved on a no-op


def test_save_section_stale_is_409(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    base_commit, base_section = _capture(tmp_path, ws_id, 1)
    # Move HEAD AND change the section's bytes underneath the captured base.
    # (Use the same author env as _init_repo so the concurrent commit succeeds
    # without relying on a global git identity.)
    from call_manager.proposal_assist.citations.gitio import _git_env

    repo = workspace_repo_dir(tmp_path, ws_id)
    (repo / PROPOSAL_FILENAME).write_text("preamble\n\n## Methods\nSOMEONE ELSE EDITED.\n")
    subprocess.run(["git", "commit", "-aqm", "concurrent"], cwd=repo, check=True, env=_git_env())
    c = _client(app, owner_id)
    r = c.post(
        f"/api/workspaces/{ws_id}/sections/1",
        json={
            "content": "## Methods\nmy late edit.\n",
            "base_commit_sha": base_commit,
            "base_section_sha": base_section,
            "base_branch": "main",
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_save_section_structure_violation_is_400(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    base_commit, base_section = _capture(tmp_path, ws_id, 1)
    c = _client(app, owner_id)
    r = c.post(
        f"/api/workspaces/{ws_id}/sections/1",
        json={
            "content": "no heading any more\n",  # deletes the ## boundary
            "base_commit_sha": base_commit,
            "base_section_sha": base_section,
            "base_branch": "main",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_save_section_bad_index_is_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    base_commit, _ = _capture(tmp_path, ws_id, 1)
    c = _client(app, owner_id)
    r = c.post(
        f"/api/workspaces/{ws_id}/sections/99",
        json={
            "content": "## X\nbody.\n",
            "base_commit_sha": base_commit,
            "base_section_sha": "0" * 64,
            "base_branch": "main",
        },
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_save_section_non_owner_member_is_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, member_id, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    base_commit, base_section = _capture(tmp_path, ws_id, 1)
    c = _client(app, member_id)
    r = c.post(
        f"/api/workspaces/{ws_id}/sections/1",
        json={
            "content": "## Methods\nmember tried to edit.\n",
            "base_commit_sha": base_commit,
            "base_section_sha": base_section,
            "base_branch": "main",
        },
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_save_section_no_proposal_file_is_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """The workspace EXISTS and the caller is the owner (require_owner passes),
    but there is no proposal.md / no git HEAD yet. The API save route must return
    404 instead of letting save_section surface a raw FileNotFoundError or
    GitCommitError. Seeds its own owner+workspace WITHOUT `_init_repo`, like
    test_draft_no_proposal_file_is_404."""
    app = _app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(title="W", owner_id=owner.id, current_phase="active")
        s.add(ws)
        s.commit()
        owner_id, ws_id = owner.id, ws.id
    # Deliberately do NOT init the repo / write proposal.md.
    c = _client(app, owner_id)
    r = c.post(
        f"/api/workspaces/{ws_id}/sections/0",
        json={
            "content": "## Methods\nbody.\n",
            "base_commit_sha": "0" * 40,
            "base_section_sha": "0" * 64,
            "base_branch": "main",
        },
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_save_section_non_owner_no_proposal_is_403_not_404(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """AUTHZ-ORDERING regression: a non-owner probing an existing workspace that has
    NO proposal.md yet must get 403 (owner only) — NOT 404 ('no proposal.md yet').
    The pre-read 404 precheck must run AFTER require_owner so the draft-lifecycle
    state is never disclosed to non-owners."""
    app = _app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        member = User(status="active", email="member@ki.se")
        s.add_all([owner, member])
        s.flush()
        ws = Workspace(title="W", owner_id=owner.id, current_phase="active")
        s.add(ws)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=member.id))
        s.commit()
        member_id, ws_id = member.id, ws.id
    # Deliberately do NOT init the repo / write proposal.md.
    c = _client(app, member_id)
    r = c.post(
        f"/api/workspaces/{ws_id}/sections/0",
        json={
            "content": "## Methods\nbody.\n",
            "base_commit_sha": "0" * 40,
            "base_section_sha": "0" * 64,
            "base_branch": "main",
        },
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_save_section_busy_is_retriable_429_with_message(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """When the commit mutex times out, the JSON path must surface a RETRIABLE
    response that preserves the busy message. A 503 would be sanitized to a hard
    'internal_error' by the global >=500 handler (main.py:335), so the SPA could
    not distinguish busy from a real 500. The route maps CommitMutexTimeout to 429
    (too_many_requests) with BUSY_ERROR_MESSAGE + a Retry-After header, which the
    4xx branch (main.py:344) echoes verbatim."""
    from call_manager.proposal_assist.drafting.locking import (
        BUSY_ERROR_MESSAGE,
        CommitMutexTimeout,
    )

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    base_commit, base_section = _capture(tmp_path, ws_id, 1)

    def _boom(*args, **kwargs):
        raise CommitMutexTimeout("busy")

    monkeypatch.setattr("call_manager.web.api.workspaces.save_section", _boom)
    c = _client(app, owner_id)
    r = c.post(
        f"/api/workspaces/{ws_id}/sections/1",
        json={
            "content": "## Methods\nrewritten body.\n",
            "base_commit_sha": base_commit,
            "base_section_sha": base_section,
            "base_branch": "main",
        },
    )
    assert r.status_code == 429
    body = r.json()
    assert body["error"]["code"] == "too_many_requests"
    assert body["error"]["message"] == BUSY_ERROR_MESSAGE
    assert r.headers.get("Retry-After") == "2"


# --- SP2a-4a: citation source endpoint --------------------------------------
from call_manager.db.models import ReferenceChunk  # noqa: E402
from call_manager.proposal_assist.references.storage import ReferenceStorage  # noqa: E402


def _seed_source_reference(
    pg_session_factory,
    tmp_path,
    ws_id,
    *,
    rid_workspace_id=None,
    full_text="Intro.\n\nThe cat sat on the mat.\n\nEnd.\n",
    chunk_text="The cat sat on the mat.",
    text_status="done",
    title="The Smith 2021 Study",
    citekey="smith2021",
):
    """Seed a Reference (+ one ReferenceChunk + text.txt) for the source endpoint.

    `rid_workspace_id` lets the Reference belong to a DIFFERENT workspace than the
    one whose ledger points at it (the cross-workspace leak guard test)."""
    owner_ws = rid_workspace_id or ws_id
    with pg_session_factory() as s:
        ref = Reference(
            workspace_id=owner_ws,
            kind="paper",
            source_type="uploaded",
            title=title,
            filename="smith2021.pdf",
            storage_path="refs/smith2021.pdf",
            mime_type="application/pdf",
            citekey=citekey,
            text_status=text_status,
        )
        s.add(ref)
        s.flush()
        rid = ref.id
        s.add(
            ReferenceChunk(
                reference_id=rid,
                workspace_id=owner_ws,
                chunk_index=0,
                section_path=None,
                text=chunk_text,
                embedding=[0.0] * 1024,  # Vector(1024); value irrelevant to this route
                embedding_model="fake-embedder",
            )
        )
        s.commit()
    if text_status == "done":
        storage = ReferenceStorage(tmp_path)
        tp = storage.text_path_for(workspace_id=owner_ws, reference_id=rid)
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text(full_text, encoding="utf-8")
    return rid


def _commit_ledger(tmp_path, ws_id, rows):
    """Write citations.json with `rows` and commit it into the workspace repo."""
    from call_manager.proposal_assist.citations.gitio import _git_env

    repo = workspace_repo_dir(tmp_path, ws_id)
    ledger = empty_ledger()
    ledger["evidence"].extend(rows)
    (repo / CITATIONS_FILENAME).write_text(dump_ledger(ledger))
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=_git_env())
    subprocess.run(["git", "commit", "-qm", "ledger"], cwd=repo, check=True, env=_git_env())


def _source_setup(app, pg_session_factory, tmp_path, *, ev_id, quote, **ref_kwargs):
    owner_id, member_id, stranger_id, ws_id = _seed(
        app,
        pg_session_factory,
        tmp_path,
        f"preamble\n\n## Methods\nSee [@smith2021]<!-- ev:{ev_id} --> here.\n",
    )
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, **ref_kwargs)
    _commit_ledger(
        tmp_path,
        ws_id,
        [
            {
                "id": ev_id,
                "citekey": "smith2021",
                "reference_id": str(rid),
                "locator": {"chunk_index": 0, "section_path": None},
                "evidence_quote": quote,
                "stance": "supports",
                "claim_context": {"text": "The cat is relevant.", "hash": "x"},
            }
        ],
    )
    return owner_id, member_id, stranger_id, ws_id, rid


def test_citation_source_happy_path(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    ev_id = "ev-abc123def456"
    quote = "The cat sat on the mat."
    owner_id, _, _, ws_id, _ = _source_setup(
        app, pg_session_factory, tmp_path, ev_id=ev_id, quote=quote
    )
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/citations/{ev_id}/source")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["evidence_id"] == ev_id
    assert "The Smith 2021 Study" in b["source_line"]
    assert b["claim"] == "The cat is relevant."
    assert b["stance"] == "supports"
    assert b["index"] == 1 and b["total"] == 1
    assert b["prev_ev"] is None and b["next_ev"] is None
    assert b["no_text"] is False and b["tombstoned"] is False
    assert b["highlight"] is not None
    s, e = b["highlight"]
    assert b["text"][s:e] == quote


def test_citation_source_member_can_read(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    ev_id = "ev-abc123def456"
    _, member_id, _, ws_id, _ = _source_setup(
        app, pg_session_factory, tmp_path, ev_id=ev_id, quote="The cat sat on the mat."
    )
    c = _client(app, member_id)
    r = c.get(f"/api/workspaces/{ws_id}/citations/{ev_id}/source")
    assert r.status_code == 200, r.text


def test_citation_source_non_member_is_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    ev_id = "ev-abc123def456"
    _, _, stranger_id, ws_id, _ = _source_setup(
        app, pg_session_factory, tmp_path, ev_id=ev_id, quote="The cat sat on the mat."
    )
    c = _client(app, stranger_id)
    r = c.get(f"/api/workspaces/{ws_id}/citations/{ev_id}/source")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_citation_source_unknown_ev_is_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id, _ = _source_setup(
        app, pg_session_factory, tmp_path, ev_id="ev-abc123def456", quote="The cat sat on the mat."
    )
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/citations/ev-000000000000/source")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_citation_source_cross_workspace_reference_is_404(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """The ledger row's reference_id points at a Reference owned by a DIFFERENT
    workspace — the ownership guard must refuse it (no cross-workspace leak)."""
    app = _app(monkeypatch, pg_url, tmp_path)
    ev_id = "ev-abc123def456"
    # Make a second workspace to own the reference.
    with pg_session_factory() as s:
        from call_manager.db.models import User, Workspace

        other_owner = User(status="active", email="other@ki.se")
        s.add(other_owner)
        s.flush()
        ws_b = Workspace(title="B", owner_id=other_owner.id, current_phase="active")
        s.add(ws_b)
        s.commit()
        ws_b_id = ws_b.id
    owner_id, _, _, ws_id, _ = _source_setup(
        app,
        pg_session_factory,
        tmp_path,
        ev_id=ev_id,
        quote="The cat sat on the mat.",
        rid_workspace_id=ws_b_id,
        title="Cross-Workspace Secret",
    )
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/citations/{ev_id}/source")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_citation_source_not_indexed_returns_no_text(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    ev_id = "ev-abc123def456"
    owner_id, _, _, ws_id, _ = _source_setup(
        app,
        pg_session_factory,
        tmp_path,
        ev_id=ev_id,
        quote="The cat sat on the mat.",
        text_status="pending",
    )
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/citations/{ev_id}/source")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["no_text"] is True
    assert b["text"] == ""
    assert b["highlight"] is None


def test_citation_source_prev_next_navigation(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """Two cites of the SAME reference -> index/total + prev/next wiring."""
    app = _app(monkeypatch, pg_url, tmp_path)
    ev1, ev2 = "ev-aaaaaaaaaaaa", "ev-bbbbbbbbbbbb"
    owner_id, _, _, ws_id = _seed(
        app,
        pg_session_factory,
        tmp_path,
        f"preamble\n\n## Methods\nA [@smith2021]<!-- ev:{ev1} --> and "
        f"B [@smith2021]<!-- ev:{ev2} -->.\n",
    )
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id)
    _commit_ledger(
        tmp_path,
        ws_id,
        [
            {
                "id": ev1,
                "citekey": "smith2021",
                "reference_id": str(rid),
                "locator": {"chunk_index": 0, "section_path": None},
                "evidence_quote": "The cat sat on the mat.",
            },
            {
                "id": ev2,
                "citekey": "smith2021",
                "reference_id": str(rid),
                "locator": {"chunk_index": 0, "section_path": None},
                "evidence_quote": "The cat sat on the mat.",
            },
        ],
    )
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/citations/{ev1}/source")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["index"] == 1 and b["total"] == 2
    assert b["prev_ev"] is None and b["next_ev"] == ev2


def test_citation_source_corrupt_ledger_is_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """A corrupt citations.json must degrade to a clean 404, never a raw 500
    (load_ledger raises ValueError -> caught -> 404). Mirrors the Jinja drawer's
    fail-soft, but stricter: the JSON route turns it into a not_found envelope."""
    app = _app(monkeypatch, pg_url, tmp_path)
    ev_id = "ev-abc123def456"
    owner_id, _, _, ws_id, _ = _source_setup(
        app, pg_session_factory, tmp_path, ev_id=ev_id, quote="The cat sat on the mat."
    )
    from call_manager.proposal_assist.citations.gitio import _git_env

    repo = workspace_repo_dir(tmp_path, ws_id)
    (repo / CITATIONS_FILENAME).write_text("{ this is not valid json ")
    subprocess.run(["git", "commit", "-aqm", "corrupt"], cwd=repo, check=True, env=_git_env())
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/citations/{ev_id}/source")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_citation_source_tombstoned_reference_returns_200(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """A tombstoned (removed) reference must still 200 with tombstoned=True so the
    peek can show a 'removed' banner rather than erroring — the source bytes may
    still be on disk and worth reading."""
    app = _app(monkeypatch, pg_url, tmp_path)
    ev_id = "ev-abc123def456"
    owner_id, _, _, ws_id, rid = _source_setup(
        app, pg_session_factory, tmp_path, ev_id=ev_id, quote="The cat sat on the mat."
    )
    from datetime import UTC, datetime

    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        ref.tombstoned_at = datetime.now(UTC)
        s.commit()
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/citations/{ev_id}/source")
    assert r.status_code == 200, r.text
    assert r.json()["tombstoned"] is True


def test_citation_source_malformed_nested_fields_degrade_not_500(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Codex finding: a VALID-JSON ledger row (so load_ledger accepts it) with
    malformed NESTED fields — locator/claim_context not a dict, chunk_index not an
    int, evidence_quote/stance not a str — must DEGRADE to 200, never raise a raw
    500 (AttributeError on `.get()` / a Pydantic validation error). The earlier
    corrupt-ledger test only covers invalid JSON, so this pins the nested case."""
    app = _app(monkeypatch, pg_url, tmp_path)
    ev_id = "ev-abc123def456"
    owner_id, _, _, ws_id = _seed(
        app,
        pg_session_factory,
        tmp_path,
        f"preamble\n\n## Methods\nSee [@smith2021]<!-- ev:{ev_id} --> here.\n",
    )
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id)
    _commit_ledger(
        tmp_path,
        ws_id,
        [
            {
                "id": ev_id,
                "citekey": "smith2021",
                "reference_id": str(rid),
                "locator": "bad",  # not a dict -> would crash locator.get(...)
                "claim_context": "bad",  # not a dict -> would crash .get("text")
                "chunk_index": "nope",  # also stray top-level (ignored)
                "evidence_quote": ["bad"],  # not a str
                "stance": ["bad"],  # not a str -> would 500 Pydantic
            }
        ],
    )
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/citations/{ev_id}/source")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["claim"] is None
    assert b["stance"] is None


# --- SP2a-4b: human select-to-cite endpoint ---------------------------------
def _cite_body(bc, bs, rid, **over):
    body = {
        "section_index": 1,
        "base_commit_sha": bc,
        "base_section_sha": bs,
        "anchor": "original.",
        "reference_id": str(rid),
        "chunk_index": 0,
        "evidence_quote": "azure",
        "quote_start": 0,
        "quote_end": 5,
        "stance": "supports",
    }
    body.update(over)
    return body


def test_post_citation_happy_path(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    bc, bs = _capture(tmp_path, ws_id, 1)
    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/citations", json=_cite_body(bc, bs, rid))
    assert r.status_code == 200, r.text
    b = r.json()
    assert set(b) == {"ev_id", "citekey", "commit_sha"}
    saved = (workspace_repo_dir(tmp_path, ws_id) / PROPOSAL_FILENAME).read_text()
    assert f"original.[@{b['citekey']}]<!-- ev:{b['ev_id']} -->" in saved


def test_post_citation_member_is_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, member_id, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    bc, bs = _capture(tmp_path, ws_id, 1)
    c = _client(app, member_id)
    r = c.post(f"/api/workspaces/{ws_id}/citations", json=_cite_body(bc, bs, rid))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_post_citation_stale_is_409(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    bc, bs = _capture(tmp_path, ws_id, 1)
    repo = workspace_repo_dir(tmp_path, ws_id)
    (repo / PROPOSAL_FILENAME).write_text("preamble\n\n## Methods\nCHANGED text.\n")
    subprocess.run(["git", "commit", "-aqm", "x"], cwd=repo, check=True, env=_git_env())
    c = _client(app, owner_id)
    r = c.post(
        f"/api/workspaces/{ws_id}/citations", json=_cite_body(bc, bs, rid, anchor="original.")
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_post_citation_double_submit_second_is_409(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    bc, bs = _capture(tmp_path, ws_id, 1)
    c = _client(app, owner_id)
    body = _cite_body(bc, bs, rid)
    r1 = c.post(f"/api/workspaces/{ws_id}/citations", json=body)
    assert r1.status_code == 200, r1.text
    r2 = c.post(f"/api/workspaces/{ws_id}/citations", json=body)  # same stale base
    assert r2.status_code == 409
    saved = (workspace_repo_dir(tmp_path, ws_id) / PROPOSAL_FILENAME).read_text()
    assert saved.count("ev:") == 1  # exactly one marker


def test_post_citation_quote_mismatch_is_422(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    bc, bs = _capture(tmp_path, ws_id, 1)
    c = _client(app, owner_id)
    r = c.post(
        f"/api/workspaces/{ws_id}/citations", json=_cite_body(bc, bs, rid, evidence_quote="WRONG")
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_post_citation_foreign_reference_is_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    bc, bs = _capture(tmp_path, ws_id, 1)
    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/citations", json=_cite_body(bc, bs, uuid.uuid4()))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_post_citation_busy_is_429(monkeypatch, pg_url, pg_session_factory, tmp_path):
    from call_manager.proposal_assist.drafting.locking import CommitMutexTimeout

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    bc, bs = _capture(tmp_path, ws_id, 1)

    def _boom(**kwargs):
        raise CommitMutexTimeout("busy")

    monkeypatch.setattr("call_manager.web.api.workspaces.insert_citation_for_user", _boom)
    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/citations", json=_cite_body(bc, bs, rid))
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "too_many_requests"
    assert r.headers.get("Retry-After") == "2"


def test_post_citation_unknown_workspace_is_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    # require_owner (proposal_assist/web/_perms.py:22-23) raises 404 for a workspace
    # UUID that doesn't exist — distinct from the foreign-owner 403 path.
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, _ = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    c = _client(app, owner_id)
    r = c.post(
        f"/api/workspaces/{uuid.uuid4()}/citations",
        json=_cite_body("0" * 40, "s", uuid.uuid4()),
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_post_citation_no_proposal_is_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    # Owner is valid (require_owner passes), but proposal.md is gone, so the FIRST
    # precheck branch — `if not (repo_dir / PROPOSAL_FILENAME).exists()` — trips → 404.
    # (.git is left intact, so current_head would still succeed; the exists() check
    # fires first.)
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    bc, bs = _capture(tmp_path, ws_id, 1)
    (workspace_repo_dir(tmp_path, ws_id) / PROPOSAL_FILENAME).unlink()
    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/citations", json=_cite_body(bc, bs, rid))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


# --- Collab cite: flag-on routes to live room (any writer); flag-off = owner-only --


def _room_base_sha(app, pg_session_factory, tmp_path, ws_id, section_index=1):
    from call_manager.proposal_assist.collab.canonical import canonical_artifact_markdown
    from call_manager.proposal_assist.editing.sectionizer import split_sections
    from call_manager.proposal_assist.paths import workspace_repo_dir

    with pg_session_factory() as s:
        raw = canonical_artifact_markdown(
            s, workspace_id=ws_id, artifact="draft", repo_dir=workspace_repo_dir(tmp_path, ws_id)
        )
    return split_sections(raw)[section_index].sha


def _enable_collab(pg_session_factory, ws_id):
    from call_manager.proposal_assist.collab import state

    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=ws_id, artifact="draft")
        s.commit()


def test_post_citation_collab_any_writer_allowed(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _stranger_id, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    _enable_collab(pg_session_factory, ws_id)
    bc, _ = _capture(tmp_path, ws_id, 1)  # git head (base_commit_sha, not the gate)
    bs = _room_base_sha(app, pg_session_factory, tmp_path, ws_id)
    c = _client(app, member_id)  # a NON-owner writer
    r = c.post(f"/api/workspaces/{ws_id}/citations", json=_cite_body(bc, bs, rid))
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"ev_id", "citekey", "commit_sha"}


def test_post_citation_flag_off_still_owner_only(monkeypatch, pg_url, pg_session_factory, tmp_path):
    # Parity: with collab OFF, a member is still 403 (unchanged git path).
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _s, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    bc, bs = _capture(tmp_path, ws_id, 1)
    r = _client(app, member_id).post(
        f"/api/workspaces/{ws_id}/citations", json=_cite_body(bc, bs, rid)
    )
    assert r.status_code == 403


def test_post_citation_collab_out_of_range_section_is_409(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    # Review A/B: an out-of-range section_index (a peer deleted the section mid-cite)
    # must return 409 on the flag-on path — parity with the flag-off CiteStale, NOT a 500.
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _m, _s, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    _enable_collab(pg_session_factory, ws_id)
    bc, _ = _capture(tmp_path, ws_id, 1)
    r = _client(app, owner_id).post(
        f"/api/workspaces/{ws_id}/citations",
        json=_cite_body(bc, "0" * 64, rid, section_index=99),
    )
    assert r.status_code == 409, r.text


def test_post_citation_collab_invalid_stance_is_422(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    # Parity: flag-off human_cite rejects a bad stance with 422; the flag-on direct
    # path must too (compute_citation_insert does not validate stance).
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _m, _s, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    _enable_collab(pg_session_factory, ws_id)
    bc, _ = _capture(tmp_path, ws_id, 1)
    bs = _room_base_sha(app, pg_session_factory, tmp_path, ws_id)
    r = _client(app, owner_id).post(
        f"/api/workspaces/{ws_id}/citations",
        json=_cite_body(bc, bs, rid, stance="not-a-real-stance"),
    )
    assert r.status_code == 422, r.text


def test_references_list_member_gated_excludes_tombstoned(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, stranger_id, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    live = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure")
    # Distinct citekey: (workspace_id, citekey) is uniquely constrained, so a second
    # reference in the same workspace must not reuse the helper's default "smith2021".
    dead = _seed_source_reference(
        pg_session_factory, tmp_path, ws_id, chunk_text="grey", citekey="jones2022"
    )
    from datetime import UTC, datetime

    with pg_session_factory() as s:
        s.get(Reference, dead).tombstoned_at = datetime.now(UTC)
        s.commit()
    # member can read; tombstoned excluded
    r = _client(app, member_id).get(f"/api/workspaces/{ws_id}/references")
    assert r.status_code == 200, r.text
    ids = {x["id"] for x in r.json()}
    assert str(live) in ids and str(dead) not in ids
    one = next(x for x in r.json() if x["id"] == str(live))
    assert one["title"] == "The Smith 2021 Study"
    assert one["text_status"] == "done"
    # stranger forbidden
    assert _client(app, stranger_id).get(f"/api/workspaces/{ws_id}/references").status_code == 403


def test_reference_chunks_ordered_and_guarded(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, stranger_id, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    # Seed a reference with two chunks (chunk_index 1 added after 0 to test ordering).
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="chunk zero")
    with pg_session_factory() as s:
        s.add(
            ReferenceChunk(
                reference_id=rid,
                workspace_id=ws_id,
                chunk_index=1,
                section_path="## B",
                text="chunk one",
                embedding=[0.0] * 1024,
                embedding_model="fake",
            )
        )
        s.commit()
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/references/{rid}/chunks")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reference_id"] == str(rid)
    assert [c["chunk_index"] for c in body["chunks"]] == [0, 1]
    assert body["chunks"][0]["text"] == "chunk zero"
    # stranger forbidden
    assert (
        _client(app, stranger_id)
        .get(f"/api/workspaces/{ws_id}/references/{rid}/chunks")
        .status_code
        == 403
    )


def test_reference_chunks_foreign_reference_is_404(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/references/{uuid.uuid4()}/chunks")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_reference_chunks_cross_workspace_reference_is_404(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """reference_id belongs to a DIFFERENT workspace — the ownership guard must
    refuse it (proves the guard, not just routing: the ref EXISTS elsewhere)."""
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    with pg_session_factory() as s:
        other = User(status="active", email="other@ki.se")
        s.add(other)
        s.flush()
        ws_b = Workspace(title="B", owner_id=other.id, current_phase="active")
        s.add(ws_b)
        s.commit()
        ws_b_id = ws_b.id
    foreign_rid = _seed_source_reference(
        pg_session_factory,
        tmp_path,
        ws_id,
        rid_workspace_id=ws_b_id,
        title="Cross-Workspace Secret",
        citekey="secret2099",
    )
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/references/{foreign_rid}/chunks")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_reference_chunks_tombstoned_reference_is_404(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "preamble\n\n## Methods\noriginal.\n"
    )
    rid = _seed_source_reference(pg_session_factory, tmp_path, ws_id, chunk_text="azure sky")
    from datetime import UTC, datetime

    with pg_session_factory() as s:
        s.get(Reference, rid).tombstoned_at = datetime.now(UTC)
        s.commit()
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/references/{rid}/chunks")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


# --- Task 1: GET /cockpit-meta -----------------------------------------------
def test_cockpit_meta_owner(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/cockpit-meta")
    assert r.status_code == 200
    b = r.json()
    assert b["workspace_title"] == "W"
    assert b["current_phase"] == "active"
    assert b["role"] == "owner"
    assert b["spec_locked"] is False
    assert b["member_count"] == 2  # owner + 1 viewer
    assert b["workspace_deadline"] is None


def test_cockpit_meta_member_sees_member_role(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, member_id).get(f"/api/workspaces/{ws_id}/cockpit-meta")
    assert r.status_code == 200
    assert r.json()["role"] == "member"


def test_cockpit_meta_non_member_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, _, stranger_id, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, stranger_id).get(f"/api/workspaces/{ws_id}/cockpit-meta")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_cockpit_meta_reads_workspace_deadline(monkeypatch, pg_url, pg_session_factory, tmp_path):
    # Pins the contract: cockpit-meta reads Workspace.deadline (NOT the optional
    # linked Call.deadline). Seed a deadline directly on the workspace and assert
    # it round-trips, so a future "read it off the Call" regression fails here.
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        ws.deadline = datetime(2099, 1, 1, tzinfo=UTC)
        s.commit()
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/cockpit-meta")
    assert r.status_code == 200
    assert r.json()["workspace_deadline"].startswith("2099-01-01")


# --- Task 2: GET /versions ---------------------------------------------------
def test_versions_newest_first(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        s.add_all(
            [
                ArtifactVersion(
                    workspace_id=ws_id,
                    commit_sha="a" * 40,
                    created_by="user",
                    message="older",
                    created_at=now - timedelta(hours=1),
                ),
                ArtifactVersion(
                    workspace_id=ws_id,
                    commit_sha="b" * 40,
                    created_by="agent",
                    message="newer",
                    created_at=now,
                ),
            ]
        )
        s.commit()
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/versions")
    assert r.status_code == 200
    b = r.json()
    assert [v["message"] for v in b] == ["newer", "older"]
    assert b[0]["created_by"] == "agent"
    assert b[0]["commit_sha"] == "b" * 40


def test_versions_empty_ok(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/versions")
    assert r.status_code == 200
    assert r.json() == []


def test_versions_non_member_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, _, stranger_id, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, stranger_id).get(f"/api/workspaces/{ws_id}/versions")
    assert r.status_code == 403


# --- Task 3: GET /members ---------------------------------------------------
def test_members_owner_sees_named_list(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/members")
    assert r.status_code == 200
    b = r.json()
    assert b[0]["email"] == "owner@ki.se" and b[0]["role"] == "owner"  # owner first
    assert any(m["email"] == "member@ki.se" and m["role"] == "member" for m in b)
    assert len(b) == 2


def test_members_non_owner_member_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    # A workspace MEMBER (not just a stranger) must NOT see the named list — emails
    # stay owner-only (mirrors the settings viewer surface; no PII expansion).
    app = _app(monkeypatch, pg_url, tmp_path)
    _, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, member_id).get(f"/api/workspaces/{ws_id}/members")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


# --- Slice A: GET /api/workspaces -------------------------------------------
def _mk_source_call(s, *, slug, title):
    """Seed a Source + Call (Call requires source_id/external_id/url/title)."""
    src = Source(name=slug, slug=slug)
    s.add(src)
    s.flush()
    call = Call(
        source_id=src.id,
        external_id=f"ext-{slug}",
        url=f"https://x.test/{slug}",
        title=title,
    )
    s.add(call)
    s.flush()
    return call


def test_list_workspaces_owned_and_viewer_only(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        other = User(status="active", email="other@ki.se")
        s.add_all([owner, other])
        s.flush()
        call = _mk_source_call(s, slug="test-funder", title="Linked Call")
        w_own = Workspace(title="Mine", owner_id=owner.id, current_phase="active", call_id=call.id)
        w_shared = Workspace(title="Shared", owner_id=other.id, current_phase="setup")
        w_hidden = Workspace(title="Hidden", owner_id=other.id)
        w_deleted = Workspace(title="Gone", owner_id=owner.id, soft_deleted_at=datetime.now(UTC))
        s.add_all([w_own, w_shared, w_hidden, w_deleted])
        s.flush()
        s.add(WorkspaceViewer(workspace_id=w_shared.id, user_id=owner.id))
        s.commit()
        owner_id = owner.id
        own_id, shared_id = str(w_own.id), str(w_shared.id)
    c = _client(app, owner_id)
    r = c.get("/api/workspaces")
    assert r.status_code == 200
    items = r.json()
    ids = [it["id"] for it in items]
    assert own_id in ids and shared_id in ids
    assert all(it["title"] != "Hidden" for it in items)  # not visible
    assert all(it["title"] != "Gone" for it in items)  # soft-deleted excluded
    mine = next(it for it in items if it["id"] == own_id)
    assert mine["is_owner"] is True
    assert mine["linked_call_title"] == "Linked Call"
    assert mine["current_phase"] == "active"
    shared = next(it for it in items if it["id"] == shared_id)
    assert shared["is_owner"] is False
    assert shared["linked_call_title"] is None


def test_list_workspaces_status_filter_and_validation(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        s.add_all(
            [
                Workspace(title="Draft one", owner_id=owner.id, status="drafting"),
                Workspace(title="Submitted one", owner_id=owner.id, status="submitted"),
            ]
        )
        s.commit()
        owner_id = owner.id
    c = _client(app, owner_id)
    r = c.get("/api/workspaces?status=submitted")
    assert r.status_code == 200
    assert [it["title"] for it in r.json()] == ["Submitted one"]
    assert c.get("/api/workspaces?status=bogus").status_code == 400


# --- Slice A Task 2: POST /api/workspaces ------------------------------------
def test_create_workspace_from_call(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        call = _mk_source_call(s, slug="test-funder-c1", title="Linked Call")
        s.commit()
        owner_id, call_id = owner.id, call.id
    c = _client(app, owner_id)
    r = c.post("/api/workspaces", json={"call_id": call_id, "title": "  My draft  "})
    assert r.status_code == 201
    new_id = r.json()["id"]
    with pg_session_factory() as s:
        ws = s.get(Workspace, uuid.UUID(new_id))
        assert ws.title == "My draft"  # trimmed
        assert ws.current_phase == "setup"
        assert ws.call_id == call_id
        jobs = s.scalars(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws.id)
        ).all()
        assert len(jobs) == 1  # Phase-0 enqueued


def test_create_workspace_validation(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        call = _mk_source_call(s, slug="test-funder-c2", title="C")
        s.commit()
        owner_id, call_id = owner.id, call.id
    c = _client(app, owner_id)
    assert c.post("/api/workspaces", json={"call_id": call_id, "title": "   "}).status_code == 400
    assert (
        c.post("/api/workspaces", json={"call_id": call_id, "title": "x" * 501}).status_code == 400
    )
    assert c.post("/api/workspaces", json={"call_id": 999999, "title": "ok"}).status_code == 400
    assert (
        c.post(
            "/api/workspaces",
            json={"call_id": call_id, "title": "ok", "deadline": "not-a-date"},
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/api/workspaces",
            json={"call_id": call_id, "title": "ok", "profile_id": 999999},
        ).status_code
        == 400
    )


def test_create_workspace_stores_profile_and_deadline(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        call = _mk_source_call(s, slug="test-funder-c3", title="C")
        prof = Profile(
            name="Grp",
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            description="d",
            structured={},
            created_by=owner.id,
        )
        s.add(prof)
        s.commit()
        owner_id, call_id, prof_id = owner.id, call.id, prof.id
    c = _client(app, owner_id)
    r = c.post(
        "/api/workspaces",
        json={
            "call_id": call_id,
            "title": "ok",
            "profile_id": prof_id,
            "deadline": "2099-07-12T00:00",
        },
    )
    assert r.status_code == 201
    with pg_session_factory() as s:
        ws = s.get(Workspace, uuid.UUID(r.json()["id"]))
        assert ws.profile_id == prof_id
        assert ws.deadline is not None and ws.deadline.year == 2099


def test_references_list_includes_library_fields(monkeypatch, pg_url, pg_session_factory, tmp_path):
    from call_manager.db.models import Reference

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "x\n")
    with pg_session_factory() as s:
        s.add(
            Reference(
                workspace_id=ws_id,
                kind="paper",
                source_type="uploaded",
                filename="seed.pdf",
                storage_path="seed/seed.pdf",
                mime_type="application/pdf",
                text_status="failed",
                text_error="Boom: bad pdf",
                metadata_status="pending",
            )
        )
        s.commit()
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/references")
    assert r.status_code == 200
    row = r.json()[0]
    assert row["metadata_status"] == "pending"
    assert row["metadata_waiver"] is False
    assert row["text_error"] == "Boom: bad pdf"
    assert row["filename"] == "seed.pdf"


# --- Task 1 (Slice D): MemberOut.user_id -----------------------------------
def test_members_list_includes_user_id(monkeypatch, pg_url, pg_session_factory, tmp_path):
    from call_manager.db.models import User, WorkspaceViewer

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "x\n")
    with pg_session_factory() as s:
        viewer = User(status="active", email="viewer-d1@ki.se")
        s.add(viewer)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws_id, user_id=viewer.id))
        s.commit()
        viewer_id = viewer.id
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/members")
    assert r.status_code == 200, r.text
    rows = r.json()
    by_email = {m["email"]: m for m in rows}
    assert by_email["viewer-d1@ki.se"]["user_id"] == viewer_id
    assert all("user_id" in m for m in rows)
