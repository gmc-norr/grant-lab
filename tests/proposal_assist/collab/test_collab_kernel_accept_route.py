# tests/proposal_assist/collab/test_collab_kernel_accept_route.py
"""
Integration tests for the room-aware proposal-accept route (Slice 2b-1 + 2b-2, Task 4).

Cases:
  1. kernel happy path: collab enabled, pending prose proposal → POST accept → 200 accepted +
     git committed + one ArtifactVersion.
  2. live client converges: WS client synced; from another HTTP request accept → client
     receives an update frame; after applying it ydoc_to_pm_json matches accepted content.
  3. stale vs live: a live (uncheckpointed) edit in the room first, then accept a proposal
     whose base is the pre-edit sha → 200 status=stale, git unchanged.
  4. collab citation accept via HTTP: collab enabled, pending citation_insert pc → 200 accepted,
     marker in checkpointed proposal.md, ledger row in HEAD.
  4b. flag-off citation parity: collab NOT enabled, citation_insert pc → git-first 200 accepted.
  4c. unsupported kind → 422: collab enabled, citation_remove pc → 422, pc status unchanged.
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
from call_manager.db.models import ArtifactVersion, ProposedChange, Reference, ReferenceChunk
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.citations.gitio import GitCommitError
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.citations.markers import find_markers
from call_manager.proposal_assist.collab import kernel as kernel_mod
from call_manager.proposal_assist.collab import protocol as P
from call_manager.proposal_assist.collab import state as collab_state
from call_manager.proposal_assist.collab import store
from call_manager.proposal_assist.collab.hydrate import load_or_hydrate
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.room import MANAGER
from call_manager.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json
from call_manager.proposal_assist.drafting.locking import CommitMutexTimeout
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.proposals.proposing import (
    capture_base,
    propose_citation_insert,
)
from call_manager.proposal_assist.tools.citations import CITATIONS_FILENAME
from tests.proposal_assist.conftest import make_user, make_workspace, stamp_for

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
            s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft",
            workspaces_dir=workspaces_dir,
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
# Citation route test helpers / constants
# ---------------------------------------------------------------------------

_EMBED_DIM = 1024
_CHUNK_TEXT = "Adjuvant therapy reduces recurrence by 30% in the treated cohort."
_ANCHOR = "original."
_EVIDENCE_QUOTE = "reduces recurrence by 30%"
_CSL_WITH_AUTHOR = {
    "author": [{"family": "Smith", "given": "J."}],
    "issued": {"date-parts": [[2021]]},
    "title": "Test paper",
    "type": "article-journal",
}
# Proposal text used for citation route tests; anchor lives in section 1 ("## Methods").
_CITE_MD0 = "preamble\n\n## Methods\nSee original.\n"


def _seed_ref(session, workspace_id, *, citekey="smith2021"):
    """Seed a Reference + one ReferenceChunk carrying _CHUNK_TEXT."""
    ref = Reference(
        workspace_id=workspace_id,
        kind="paper",
        source_type="uploaded",
        filename="seed.pdf",
        storage_path="seed/seed.pdf",
        mime_type="application/pdf",
        citekey=citekey,
        csl_json=_CSL_WITH_AUTHOR,
        metadata_status="approved",
    )
    session.add(ref)
    session.flush()
    session.add(
        ReferenceChunk(
            reference_id=ref.id,
            workspace_id=workspace_id,
            chunk_index=0,
            section_path="Results",
            text=_CHUNK_TEXT,
            embedding=[0.0] * _EMBED_DIM,
            embedding_model="fake-embedder",
        )
    )
    session.flush()
    return ref


def _mk_citation_pc_via_propose(
    session, wid, *, section_index, base_commit_sha, base_section_sha, ref_id
):
    """Create a citation_insert ProposedChange via the official proposer."""
    pc = propose_citation_insert(
        session=session,
        workspace_id=wid,
        section_index=section_index,
        base_commit_sha=base_commit_sha,
        base_section_sha=base_section_sha,
        reference_id=str(ref_id),
        anchor=_ANCHOR,
        chunk_index=0,
        evidence_quote=_EVIDENCE_QUOTE,
        stance="supports",
        confidence="high",
        origin_kind="human",
    )
    session.commit()
    return pc.id


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
        owner = make_user(s, email="ka-route-happy@umu.se")
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
        owner = make_user(s, email="ka-route-converge@umu.se")
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

            # The observer callback must have fired (the update was applied to our doc)
            assert len(received_updates) >= 1

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
        owner = make_user(s, email="ka-route-stale@umu.se")
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
            s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft",
            workspaces_dir=str(tmp_path),
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
# Case 4: collab citation accept via HTTP (Slice 2b-2, Task 4)
# ---------------------------------------------------------------------------


def test_collab_citation_accept_via_http(_app, pg_session_factory, tmp_path):
    """Collab enabled, pending citation_insert pc → POST accept → 200 accepted,
    marker in checkpointed proposal.md, ledger row in HEAD's citations.json."""
    _app.state.session_factory = pg_session_factory
    _app.state.settings.workspaces_dir = str(tmp_path)

    with pg_session_factory() as s:
        owner = make_user(s, email="ka-route-cite-accept@umu.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id

    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, _CITE_MD0)
    head = _git_head(repo)

    # Derive base_sha from the LIVE (Yjs-normalised) room — same lesson as prose tests.
    live_sections = _live_sections_from_md(pg_session_factory, wid, str(tmp_path))
    base_sha = live_sections[1].sha  # section 1 = "## Methods…"

    with pg_session_factory() as s:
        ref = _seed_ref(s, wid, citekey="smith2021")
        pc_id = _mk_citation_pc_via_propose(
            s, wid, section_index=1, base_commit_sha=head,
            base_section_sha=base_sha, ref_id=ref.id,
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
    assert _git_head(repo) != head  # a new commit was made

    # Marker in checkpointed proposal.md.
    disk = (repo / PROPOSAL_FILENAME).read_text()
    markers = find_markers(disk)
    assert len(markers) == 1, f"expected 1 marker in disk, got {markers}"
    ev_id = markers[0]["ev_id"]

    # Ledger row in HEAD's citations.json matches the marker ev_id.
    ledger = load_ledger((repo / CITATIONS_FILENAME).read_text())
    assert len(ledger["evidence"]) == 1
    assert ledger["evidence"][0]["id"] == ev_id


# ---------------------------------------------------------------------------
# Case 4b: flag-off citation parity (git-first path for citation_insert)
# ---------------------------------------------------------------------------


def test_flag_off_citation_parity(_app, pg_session_factory, tmp_path):
    """Without enable_collab, POST accept of a citation_insert pc → git-first path →
    200 accepted; both proposal.md and citations.json are in HEAD."""
    _app.state.session_factory = pg_session_factory
    _app.state.settings.workspaces_dir = str(tmp_path)

    with pg_session_factory() as s:
        owner = make_user(s, email="ka-route-cite-flagoff@umu.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        # Deliberately NOT calling enable_collab
        s.commit()
        wid, oid = ws.id, owner.id

    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, _CITE_MD0)
    head = _git_head(repo)

    # For the git-first path, base_section_sha is from raw git markdown (no Yjs round-trip).
    base_commit_sha, base_section_sha = capture_base(repo, 1)

    with pg_session_factory() as s:
        ref = _seed_ref(s, wid, citekey="smith2021")
        pc_id = _mk_citation_pc_via_propose(
            s, wid, section_index=1, base_commit_sha=base_commit_sha,
            base_section_sha=base_section_sha, ref_id=ref.id,
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
    assert _git_head(repo) != head  # git commit was made

    # Marker present in proposal.md.
    disk = (repo / PROPOSAL_FILENAME).read_text()
    markers = find_markers(disk)
    assert len(markers) == 1


# ---------------------------------------------------------------------------
# Case 4c: unsupported kind → 422
# ---------------------------------------------------------------------------


def test_unsupported_kind_returns_422(_app, pg_session_factory, tmp_path):
    """Collab enabled, a citation_remove pc (no producer) → POST accept → 422;
    pc status stays pending, room NOT evicted, git unchanged."""
    _app.state.session_factory = pg_session_factory
    _app.state.settings.workspaces_dir = str(tmp_path)

    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        owner = make_user(s, email="ka-route-unsupported@umu.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id

    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    head = _git_head(repo)

    live_sections = _live_sections_from_md(pg_session_factory, wid, str(tmp_path))
    base_sha = live_sections[1].sha

    # Insert a citation_remove pc directly (no producer for this kind).
    with pg_session_factory() as s:
        pc = ProposedChange(
            id=uuidlib.uuid4(),
            workspace_id=wid,
            status="pending",
            target_artifact="draft",
            section_index=1,
            base_commit_sha=head,
            base_section_sha=base_sha,
            kind="citation_remove",
            payload={"ev_id": "fake-ev"},
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

    assert r.status_code == 422, r.text

    # pc status UNCHANGED (still pending — no eviction).
    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).status == "pending"

    # Room NOT evicted (the dispatch raised before any mutation).
    # Room was never opened (or was evicted) — either is acceptable.
    assert MANAGER.get((wid, "draft")) is None
    # Git unchanged.
    assert _git_head(repo) == head


# ---------------------------------------------------------------------------
# Case 5: flag-off parity
# ---------------------------------------------------------------------------


def test_flag_off_parity_git_first(_app, pg_session_factory, tmp_path):
    """Without enable_collab, POST accept uses the git-first path → 200 accepted."""
    _app.state.session_factory = pg_session_factory
    _app.state.settings.workspaces_dir = str(tmp_path)

    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        owner = make_user(s, email="ka-route-flagoff@umu.se")
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


# ---------------------------------------------------------------------------
# Case 6: accept-race — second accept of the same pending proposal is rejected
# ---------------------------------------------------------------------------


def test_accept_race_second_accept_is_rejected(_app, pg_session_factory, tmp_path):
    """Serialized invariant: after two sequential accepts of the SAME pending
    proposal, the FIRST succeeds (status=accepted, 1 ArtifactVersion) and the
    SECOND returns HTTP 409 (ProposedChangeNotPending).  The FOR UPDATE row lock
    + status gate inside kernel_accept_prose guarantees this without a real race.

    No double-apply: exactly ONE ArtifactVersion row exists after both attempts,
    and the proposal status stays 'accepted'."""
    _app.state.session_factory = pg_session_factory
    _app.state.settings.workspaces_dir = str(tmp_path)

    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        owner = make_user(s, email="ka-route-race@umu.se")
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
            new_text="## Background\nrace accepted.\n",
            base_commit_sha=head,
            base_section_sha=base_sha,
        )

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        # First accept: must succeed.
        r1 = client.post(f"/api/workspaces/{wid}/proposed-changes/{pc_id}/accept")
        assert r1.status_code == 200, r1.text
        assert r1.json()["status"] == "accepted"

        # Second accept of the SAME (now-accepted) proposal: status gate fires → 409.
        r2 = client.post(f"/api/workspaces/{wid}/proposed-changes/{pc_id}/accept")
        assert r2.status_code == 409, r2.text
    finally:
        client.__exit__(None, None, None)

    # Exactly ONE ArtifactVersion landed — no double-apply.
    with pg_session_factory() as s:
        av = s.execute(
            select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid)
        ).scalars().all()
        assert len(av) == 1

        pc = s.get(ProposedChange, pc_id)
        assert pc.status == "accepted"


def test_kernel_commit_mutex_timeout_maps_to_429(_app, pg_session_factory, tmp_path, monkeypatch):
    """A CommitMutexTimeout on the kernel accept path → 429 + Retry-After. It is raised
    BEFORE any fragment mutation (it's in _CLEAN_ACCEPT_ERRORS), so the proposal stays
    pending and no accept update is staged (only the hydration seed remains)."""
    _app.state.session_factory = pg_session_factory
    _app.state.settings.workspaces_dir = str(tmp_path)

    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        owner = make_user(s, email="ka-route-429@umu.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id

    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    base_sha = _live_sections_from_md(pg_session_factory, wid, str(tmp_path))[1].sha
    head = _git_head(repo)
    with pg_session_factory() as s:
        pc_id = _mk_pc(
            s, wid, section_index=1, new_text="## Background\nnever applied.\n",
            base_commit_sha=head, base_section_sha=base_sha,
        )

    def _boom(*a, **k):
        raise CommitMutexTimeout("busy")

    # endgame Task 5: kernel_accept_prose now takes the mutex via Primitive B
    # (acquire_commit_mutex_at_generation), threaded from the accept route's stamp.
    # Patch that name in kernel_mod — a CommitMutexTimeout from it is still a
    # pre-mutation _CLEAN_ACCEPT_ERROR and must map to 429.
    monkeypatch.setattr(kernel_mod, "acquire_commit_mutex_at_generation", _boom)

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        r = client.post(f"/api/workspaces/{wid}/proposed-changes/{pc_id}/accept")
    finally:
        client.__exit__(None, None, None)

    assert r.status_code == 429, r.text
    assert r.headers.get("Retry-After") == "2"
    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).status == "pending"
        # mutex raised before any mutation → only the hydration seed row exists
        assert len(store.load_rows(s, workspace_id=wid, artifact="draft")) == 1


def test_kernel_evicts_room_on_post_mutation_failure(
    _app, pg_session_factory, tmp_path, monkeypatch
):
    """A GitCommitError from checkpoint (AFTER the fragment mutation) → 500; the single
    transaction rolls back (proposal stays pending, no accept update committed) and the
    room is evicted so the next open rebuilds cleanly from the durable log."""
    _app.state.session_factory = pg_session_factory
    _app.state.settings.workspaces_dir = str(tmp_path)

    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        owner = make_user(s, email="ka-route-evict@umu.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id

    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    base_sha = _live_sections_from_md(pg_session_factory, wid, str(tmp_path))[1].sha
    head = _git_head(repo)
    with pg_session_factory() as s:
        pc_id = _mk_pc(
            s, wid, section_index=1, new_text="## Background\nwill roll back.\n",
            base_commit_sha=head, base_section_sha=base_sha,
        )

    def _boom(*a, **k):
        raise GitCommitError("disk full")

    monkeypatch.setattr(kernel_mod, "checkpoint_artifact", _boom)

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        r = client.post(f"/api/workspaces/{wid}/proposed-changes/{pc_id}/accept")
    finally:
        client.__exit__(None, None, None)

    assert r.status_code == 500, r.text
    # Poison recovery: the room whose doc was mutated then rolled back is evicted.
    assert MANAGER.get((wid, "draft")) is None
    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).status == "pending"  # transaction rolled back
        # the staged accept update was rolled back — only the hydration seed remains
        assert len(store.load_rows(s, workspace_id=wid, artifact="draft")) == 1
