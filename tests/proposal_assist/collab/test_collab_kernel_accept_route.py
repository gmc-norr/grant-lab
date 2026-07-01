# tests/proposal_assist/collab/test_collab_kernel_accept_route.py
"""
Integration tests for the room-aware proposal-accept route (Slice 2b-1, Task 4).

Cases:
  1. kernel happy path: collab enabled, pending prose proposal → POST accept → 200 accepted +
     git committed + one ArtifactVersion.
  2. live client converges: WS client synced; from another HTTP request accept → client
     receives an update frame; after applying it ydoc_to_pm_json matches accepted content.
  3. stale vs live: a live (uncheckpointed) edit in the room first, then accept a proposal
     whose base is the pre-edit sha → 200 status=stale, git unchanged.
  4. citation deferred: pending citation_insert proposal on a collab-enabled artifact → 409.
  5. flag-off parity: collab NOT enabled → git-first path returns 200 accepted.

Patterns mirror test_collab_checkpoint_triggers.py + test_collab_kernel_accept.py.
"""

import subprocess
import uuid as uuidlib
from pathlib import Path

from fastapi.testclient import TestClient
from pycrdt import Doc
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import ArtifactVersion, ProposedChange
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.collab import protocol as P
from call_manager.proposal_assist.collab import state as collab_state
from call_manager.proposal_assist.collab import store
from call_manager.proposal_assist.collab.hydrate import load_or_hydrate
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json
from call_manager.proposal_assist.editing.sectionizer import split_sections
from tests.proposal_assist.conftest import make_user, make_workspace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cookie(app, user_id: int) -> str:
    signer: SessionSigner = app.state.session_signer
    return signer.make(user_id=user_id)


def _init_repo(repo: Path, content: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _git_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def _live_sections_from_md(pg_session_factory, wid, workspaces_dir):
    """Hydrate and return the Yjs-normalised live sections (from pm_to_markdown round-trip).

    section 0 is ALWAYS the empty preamble; "## Background" is section 1.
    base_sha must come from the live room because pm_to_markdown inserts a blank line
    between heading and paragraph (mirror test_collab_kernel_accept.py's lesson).
    """
    with pg_session_factory() as s:
        updates = load_or_hydrate(
            s, workspace_id=wid, artifact="draft", workspaces_dir=workspaces_dir
        )
        s.commit()
    doc = Doc()
    for u in updates:
        doc.apply_update(u)
    live_md = pm_to_markdown(ydoc_to_pm_json(doc))
    return split_sections(live_md)


def _mk_pc(
    session,
    wid,
    *,
    section_index,
    new_text,
    base_commit_sha,
    base_section_sha,
    kind="prose_edit",
    artifact="draft",
) -> uuidlib.UUID:
    pc = ProposedChange(
        id=uuidlib.uuid4(),
        workspace_id=wid,
        status="pending",
        target_artifact=artifact,
        section_index=section_index,
        base_commit_sha=base_commit_sha,
        base_section_sha=base_section_sha,
        kind=kind,
        payload={"new_text": new_text} if kind == "prose_edit" else {},
        origin_kind="human",
    )
    session.add(pc)
    session.commit()
    return pc.id


def _recv_sync_write(wsconn, tries: int = 10) -> bytes:
    """Read frames until a SYNC write (STEP2/UPDATE) arrives."""
    for _ in range(tries):
        frame = wsconn.receive_bytes()
        if P.is_sync_write(frame):
            return frame
    raise AssertionError("no SYNC write frame received")


# ---------------------------------------------------------------------------
# Case 1: kernel happy path via HTTP
# ---------------------------------------------------------------------------


def test_kernel_happy_path_via_http(_app, pg_session_factory, tmp_path):
    """Enable collab, repo + pending prose proposal → POST accept → 200 accepted,
    resulting_commit_sha set, git file shows accepted text, one ArtifactVersion."""
    _app.state.session_factory = pg_session_factory
    _app.state.settings.workspaces_dir = str(tmp_path)

    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        owner = make_user(s, email="ka-route-happy@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id

    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)

    # Derive base_sha from the live (Yjs-normalised) room, not raw git markdown.
    live_sections = _live_sections_from_md(pg_session_factory, wid, str(tmp_path))
    # section 1 = "## Background…"; section 0 is always the empty preamble
    base_sha = live_sections[1].sha
    head = _git_head(repo)

    with pg_session_factory() as s:
        pc_id = _mk_pc(
            s, wid,
            section_index=1,
            new_text="## Background\nACCEPTED via kernel.\n",
            base_commit_sha=head,
            base_section_sha=base_sha,
        )

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        r = client.post(f"/api/workspaces/{wid}/proposed-changes/{pc_id}/accept")
    finally:
        client.__exit__(None, None, None)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["resulting_commit_sha"] is not None

    disk = (repo / PROPOSAL_FILENAME).read_text()
    assert "ACCEPTED via kernel." in disk

    with pg_session_factory() as s:
        av = s.execute(
            select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid)
        ).scalars().all()
        assert len(av) == 1


# ---------------------------------------------------------------------------
# Case 2: live client converges
# ---------------------------------------------------------------------------


def test_live_client_converges(_app, pg_session_factory, tmp_path):
    """Connect a WS client; from another HTTP request accept; assert the client
    receives a SYNC update frame and after applying it ydoc_to_pm_json matches
    the accepted content."""
    _app.state.session_factory = pg_session_factory
    _app.state.settings.workspaces_dir = str(tmp_path)

    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        owner = make_user(s, email="ka-route-converge@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id

    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)

    live_sections = _live_sections_from_md(pg_session_factory, wid, str(tmp_path))
    base_sha = live_sections[1].sha
    head = _git_head(repo)

    with pg_session_factory() as s:
        pc_id = _mk_pc(
            s, wid,
            section_index=1,
            new_text="## Background\nconverged content.\n",
            base_commit_sha=head,
            base_section_sha=base_sha,
        )

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        with client.websocket_connect(f"/api/ws/workspaces/{wid}/draft") as wsconn:
            # Sync: drain initial STEP1, send our STEP1, receive STEP2
            observer_doc = Doc()
            wsconn.receive_bytes()  # server's initial STEP1
            wsconn.send_bytes(P.create_sync_message(observer_doc))  # our STEP1
            step2 = _recv_sync_write(wsconn)  # server's STEP2
            P.handle_sync_message(step2[1:], observer_doc)  # apply server state

            # Collect updates applied to our observer doc
            received_updates: list[bytes] = []
            observer_doc.observe(lambda e: received_updates.append(e.update))

            # POST accept from another HTTP connection (same TestClient, different request)
            r = client.post(f"/api/workspaces/{wid}/proposed-changes/{pc_id}/accept")
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "accepted"

            # The accept broadcasts an update frame; read it
            update_frame = _recv_sync_write(wsconn)
            update = P.extract_update(update_frame)
            observer_doc.apply_update(update)

        # After applying the broadcast update, the observer doc matches accepted content
        md_after = pm_to_markdown(ydoc_to_pm_json(observer_doc))
        assert "converged content." in md_after
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Case 3: stale vs live
# ---------------------------------------------------------------------------


def test_stale_vs_live(_app, pg_session_factory, tmp_path):
    """Apply a live (uncheckpointed) edit via the store first; then accept a
    proposal whose base is the pre-edit sha → 200 with status=stale, git unchanged."""
    _app.state.session_factory = pg_session_factory
    _app.state.settings.workspaces_dir = str(tmp_path)

    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        owner = make_user(s, email="ka-route-stale@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id

    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    head_before = _git_head(repo)

    # Hydrate the room, get base sha (pre-edit)
    with pg_session_factory() as s:
        updates = load_or_hydrate(
            s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path)
        )
        s.commit()
    doc = Doc()
    for u in updates:
        doc.apply_update(u)

    live_md = pm_to_markdown(ydoc_to_pm_json(doc))
    live_sections = split_sections(live_md)
    base_sha = live_sections[1].sha  # the ORIGINAL sha before the live edit

    # Apply a live (uncheckpointed) edit directly to the store — git HEAD stays the same
    edited_md = "".join(
        "## Background\nlive edit, uncommitted.\n" if sec.index == 1 else sec.text
        for sec in live_sections
    )
    from call_manager.proposal_assist.collab.yjs_bridge import apply_pm_json_to_fragment
    live_update = apply_pm_json_to_fragment(doc, markdown_to_pm(edited_md))
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=oid, update=live_update
        )
        s.commit()

    assert _git_head(repo) == head_before  # git unchanged

    # Create a proposal with the PRE-edit base_sha
    with pg_session_factory() as s:
        pc_id = _mk_pc(
            s, wid,
            section_index=1,
            new_text="## Background\nproposed (will be stale).\n",
            base_commit_sha=head_before,
            base_section_sha=base_sha,
        )

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        r = client.post(f"/api/workspaces/{wid}/proposed-changes/{pc_id}/accept")
    finally:
        client.__exit__(None, None, None)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "stale"

    # git HEAD unchanged
    assert _git_head(repo) == head_before
    disk = (repo / PROPOSAL_FILENAME).read_text()
    assert "proposed (will be stale)." not in disk


# ---------------------------------------------------------------------------
# Case 4: citation deferred
# ---------------------------------------------------------------------------


def test_citation_deferred_returns_409(_app, pg_session_factory, tmp_path):
    """A pending citation_insert proposal on a collab-enabled artifact → 409."""
    _app.state.session_factory = pg_session_factory
    _app.state.settings.workspaces_dir = str(tmp_path)

    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        owner = make_user(s, email="ka-route-citation@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id

    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    head = _git_head(repo)

    live_sections = _live_sections_from_md(pg_session_factory, wid, str(tmp_path))
    base_sha = live_sections[1].sha

    # Create a citation_insert proposal (not prose_edit)
    with pg_session_factory() as s:
        pc = ProposedChange(
            id=uuidlib.uuid4(),
            workspace_id=wid,
            status="pending",
            target_artifact="draft",
            section_index=1,
            base_commit_sha=head,
            base_section_sha=base_sha,
            kind="citation_insert",
            payload={"citekey": "ref1", "anchor": "original.", "ev_id": 1},
            origin_kind="human",
        )
        s.add(pc)
        s.commit()
        pc_id = pc.id

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        r = client.post(f"/api/workspaces/{wid}/proposed-changes/{pc_id}/accept")
    finally:
        client.__exit__(None, None, None)

    assert r.status_code == 409, r.text
    # The app uses a custom error envelope: {"error": {"code": ..., "message": ...}}
    body = r.json()
    assert "citation" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Case 5: flag-off parity
# ---------------------------------------------------------------------------


def test_flag_off_parity_git_first(_app, pg_session_factory, tmp_path):
    """Without enable_collab, POST accept uses the git-first path → 200 accepted."""
    _app.state.session_factory = pg_session_factory
    _app.state.settings.workspaces_dir = str(tmp_path)

    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        owner = make_user(s, email="ka-route-flagoff@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        # Deliberately NOT calling enable_collab
        s.commit()
        wid, oid = ws.id, owner.id

    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)

    # For the git-first path, base_section_sha is from raw git markdown (no Yjs round-trip)
    from call_manager.proposal_assist.proposals.proposing import capture_base
    base_commit_sha, base_section_sha = capture_base(repo, 1)

    with pg_session_factory() as s:
        pc_id = _mk_pc(
            s, wid,
            section_index=1,
            new_text="## Background\naccepted via git-first.\n",
            base_commit_sha=base_commit_sha,
            base_section_sha=base_section_sha,
        )

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        r = client.post(f"/api/workspaces/{wid}/proposed-changes/{pc_id}/accept")
    finally:
        client.__exit__(None, None, None)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["resulting_commit_sha"] is not None

    disk = (repo / PROPOSAL_FILENAME).read_text()
    assert "accepted via git-first." in disk
