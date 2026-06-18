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
from call_manager.proposal_assist.citations.gitio import _git_env
from call_manager.proposal_assist.citations.ledger import dump_ledger, empty_ledger
from call_manager.proposal_assist.editing.save import CITATIONS_FILENAME, PROPOSAL_FILENAME
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
        ws_a = Workspace(title="A", owner_id=owner.id, current_phase="spec")
        # Workspace B: owns the Reference the ledger's reference_id points to.
        ws_b = Workspace(title="B", owner_id=owner.id, current_phase="spec")
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
        },
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_save_section_no_proposal_file_is_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """The workspace EXISTS and the caller is the owner (require_owner passes),
    but there is no proposal.md / no git HEAD yet. The API save route must mirror
    the HTMX route's pre-read 404 (sections.py:73-89, used at sections.py:240)
    instead of letting save_section surface a raw FileNotFoundError / GitCommitError
    (save.py:168-170). Seeds its own owner+workspace WITHOUT `_init_repo`, like
    test_draft_no_proposal_file_is_404."""
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
    r = c.post(
        f"/api/workspaces/{ws_id}/sections/0",
        json={
            "content": "## Methods\nbody.\n",
            "base_commit_sha": "0" * 40,
            "base_section_sha": "0" * 64,
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
    state is never disclosed to non-owners (matches the HTMX sibling sections.py:233
    then :240, which authorizes before any repo I/O)."""
    app = _app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        member = User(status="active", email="member@ki.se")
        s.add_all([owner, member])
        s.flush()
        ws = Workspace(title="W", owner_id=owner.id, current_phase="spec")
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
        ws_b = Workspace(title="B", owner_id=other_owner.id, current_phase="spec")
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
