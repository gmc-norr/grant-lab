# tests/proposal_assist/collab/test_collab_kernel_citation.py  (Task 1 + Task 3)
"""Characterization tests for compute_citation_insert (Task 1) and the git-first
citation-accept path, PLUS kernel_accept_citation (Task 3): apply a citation to
the LIVE Yjs room and co-commit the evidence ledger in ONE transaction.

The Task-3 kernel tests mirror the harness in test_collab_kernel_accept.py
(_live_room / _mk_pc) and add a _mk_ref_and_chunk helper. Section indexing
lesson (from test_collab_kernel_accept.py): "## …" is section 1, the empty
preamble is section 0; base_section_sha must be taken from the Yjs-normalised
LIVE room markdown, not the raw on-disk text.
"""
import subprocess
import uuid as uuidlib
from pathlib import Path

from sqlalchemy import select

from call_manager.db.models import (
    ArtifactVersion,
    ProposedChange,
    Reference,
    ReferenceChunk,
)
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.citations.markers import find_markers
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.hydrate import build_doc, load_or_hydrate
from call_manager.proposal_assist.collab.kernel import kernel_accept_citation
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import (
    apply_pm_json_to_fragment,
    ydoc_to_pm_json,
)
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.ports.stamp import stamp_for_workspace
from call_manager.proposal_assist.proposals.accepting import (
    accept_proposed_change,
)
from call_manager.proposal_assist.proposals.proposing import (
    capture_base,
    propose_citation_insert,
)
from call_manager.proposal_assist.tools.citations import CITATIONS_FILENAME
from tests.proposal_assist.conftest import make_user, make_workspace, stamp_for

EMBED_DIM = 1024
CHUNK_TEXT = "Adjuvant therapy reduces recurrence by 30% in the treated cohort."
ANCHOR = "original."
EVIDENCE_QUOTE = "reduces recurrence by 30%"


def _init_repo(repo_dir: Path, content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def _files_in_head(repo: Path) -> set[str]:
    out = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def _setup(s, tmp_path):
    ws = make_workspace(s)
    s.flush()
    repo = Path(tmp_path) / str(ws.id) / "repo"
    _init_repo(repo, "preamble\n\n## Methods\noriginal.\n")
    return ws, repo


_CSL_WITH_AUTHOR = {
    "author": [{"family": "Smith", "given": "J."}],
    "issued": {"date-parts": [[2021]]},
    "title": "Test paper",
    "type": "article-journal",
}


def _seed_reference(s, workspace_id, *, chunk_text, citekey=None, csl_json=None):
    ref = Reference(
        workspace_id=workspace_id,
        kind="paper",
        source_type="uploaded",
        filename="seed.pdf",
        storage_path="seed/seed.pdf",
        mime_type="application/pdf",
        citekey=citekey,
        csl_json=csl_json,
        metadata_status="approved",
    )
    s.add(ref)
    s.flush()
    s.add(
        ReferenceChunk(
            reference_id=ref.id,
            workspace_id=workspace_id,
            chunk_index=0,
            section_path="Results",
            text=chunk_text,
            embedding=[0.0] * EMBED_DIM,
            embedding_model="fake-embedder",
        )
    )
    s.flush()
    return ref


def test_compute_citation_insert_is_pure(pg_session_factory, tmp_path, monkeypatch):
    """compute_citation_insert must NOT commit to git, write disk, take the mutex,
    or mutate ref.citekey. It DOES read the DB (ref/chunk/citekey resolution).

    NB: new_proposal_text is NOT byte-stable across calls — the marker embeds a
    fresh new_ev_id() each call. Assert on the STABLE facts (nothing written,
    citekey stable, ref.citekey untouched); to assert new_proposal_text identity,
    monkeypatch new_ev_id to a fixed value first.
    """
    from call_manager.proposal_assist.citations import core as citations_mod
    from call_manager.proposal_assist.citations.core import compute_citation_insert

    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        # Provide CSL JSON so generate_citekey yields a deterministic "smith2021"
        # rather than the random "ref-<hex>" it produces when CSL is None/empty.
        # This makes the citekey stable across both calls (the core never caches it).
        ref = _seed_reference(
            s, ws.id, chunk_text=CHUNK_TEXT, citekey=None, csl_json=_CSL_WITH_AUTHOR
        )
        ref_id = ref.id
        s.flush()

        # Capture HEAD before any core call.
        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Build inputs (no mutex, no disk read in the core — we pass them in).
        proposal_text = (repo / PROPOSAL_FILENAME).read_text()
        ledger = load_ledger(None)  # empty ledger (no citations.json yet)

        # Pin new_ev_id so new_proposal_text is byte-stable across both calls.
        fixed_ev = "ev-" + "ab" * 4
        monkeypatch.setattr(citations_mod, "new_ev_id", lambda: fixed_ev)

        r1 = compute_citation_insert(
            s,
            workspace_id=ws.id,
            proposal_text=proposal_text,
            ledger=ledger,
            ref_uuid=ref_id,
            anchor=ANCHOR,
            chunk_index=0,
            evidence_quote=EVIDENCE_QUOTE,
            stance="supports",
            confidence="high",
        )
        r2 = compute_citation_insert(
            s,
            workspace_id=ws.id,
            proposal_text=proposal_text,
            ledger=ledger,
            ref_uuid=ref_id,
            anchor=ANCHOR,
            chunk_index=0,
            evidence_quote=EVIDENCE_QUOTE,
            stance="supports",
            confidence="high",
        )

        # --- STABLE FACT 1: HEAD is unchanged (no git commit) ---
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert head_after == head_before, "compute_citation_insert must not commit to git"

        # --- STABLE FACT 2: No new files on disk ---
        assert not (repo / CITATIONS_FILENAME).exists(), (
            "compute_citation_insert must not write citations.json to disk"
        )

        # --- STABLE FACT 3: Both calls succeed (no error key) ---
        assert "error" not in r1, f"first call returned error: {r1}"
        assert "error" not in r2, f"second call returned error: {r2}"

        # --- STABLE FACT 4: citekeys from both calls agree ---
        assert r1["citekey"] == r2["citekey"], (
            "citekey must be stable across two calls with the same inputs"
        )

        # --- STABLE FACT 5: ref.citekey is STILL None (core must not cache-write) ---
        s.refresh(ref)
        assert ref.citekey is None, (
            "compute_citation_insert must not mutate ref.citekey (cache write is caller's job)"
        )

        # --- STABLE FACT 6: with fixed ev_id, new_proposal_text is byte-stable ---
        assert r1["new_proposal_text"] == r2["new_proposal_text"], (
            "with monkeypatched new_ev_id, new_proposal_text must be byte-stable"
        )
        # Marker is present in the new text.
        markers = find_markers(r1["new_proposal_text"])
        assert len(markers) == 1
        assert markers[0]["citekey"] == r1["citekey"]

        # --- STABLE FACT 7: result shape has the expected keys ---
        assert {"new_proposal_text", "new_ledger", "ev_id", "citekey", "ref"} <= set(r1)

        # --- STABLE FACT 8: returned ref is the session-attached Reference ---
        assert r1["ref"] is ref


def test_git_first_citation_accept_commits_both_files(pg_session_factory, tmp_path):
    """After a git-first (non-collab) citation accept, HEAD's commit must
    include BOTH proposal.md and citations.json, and the marker must be in
    proposal.md.

    This is the parity characterization alongside test_accepting.py; it stays
    green before AND after the refactor.
    """
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver-collab@umu.se")
        s.flush()
        ref = _seed_reference(s, ws.id, chunk_text=CHUNK_TEXT, citekey="smith2021")
        s.flush()

        base_commit, base_section = capture_base(repo, 1)
        pc = propose_citation_insert(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            reference_id=str(ref.id),
            anchor=ANCHOR,
            chunk_index=0,
            evidence_quote=EVIDENCE_QUOTE,
            stance="supports",
            confidence="high",
            origin_kind="interactive_turn",
        )
        s.flush()

        accept_proposed_change(
            session=s,
            proposed_change_id=pc.id,
            workspace_id=ws.id,
            approver=approver,
            workspaces_dir=str(tmp_path),
            stamp=stamp_for_workspace(ws),
        )
        s.flush()

        # Both files must be in the HEAD commit.
        files = _files_in_head(repo)
        assert PROPOSAL_FILENAME in files, (
            f"proposal.md not in HEAD commit files: {files}"
        )
        assert CITATIONS_FILENAME in files, (
            f"citations.json not in HEAD commit files: {files}"
        )

        # Marker is present in proposal.md.
        proposal_text = (repo / PROPOSAL_FILENAME).read_text()
        markers = find_markers(proposal_text)
        assert len(markers) == 1, f"expected 1 marker, got {markers}"
        assert markers[0]["citekey"] == "smith2021"


# ---------------------------------------------------------------------------
# Task 3: kernel_accept_citation — apply the citation to the LIVE room and
# co-commit the ledger in ONE transaction (mirrors test_collab_kernel_accept).
# ---------------------------------------------------------------------------

# The live proposal for the kernel tests: anchor "original." lives in section 1
# ("## Methods"); section 0 is always the empty preamble.
KERNEL_MD0 = "preamble\n\n## Methods\nSee original.\n"


def _live_room(session, wid, workspaces_dir):
    """Hydrate + return the collab room doc (seeded from HEAD, persisted as update #1)."""
    updates = load_or_hydrate(
        session, stamp=stamp_for(session, wid), workspace_id=wid, artifact="draft",
        workspaces_dir=workspaces_dir,
    )
    session.commit()
    return build_doc(updates)


def _git_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def _mk_ref_and_chunk(s, workspace_id, *, citekey=None, csl_json=None):
    """Seed a Reference + a single ReferenceChunk (index 0) carrying CHUNK_TEXT."""
    ref = Reference(
        workspace_id=workspace_id,
        kind="paper",
        source_type="uploaded",
        filename="seed.pdf",
        storage_path="seed/seed.pdf",
        mime_type="application/pdf",
        citekey=citekey,
        csl_json=csl_json,
        metadata_status="approved",
    )
    s.add(ref)
    s.flush()
    s.add(
        ReferenceChunk(
            reference_id=ref.id,
            workspace_id=workspace_id,
            chunk_index=0,
            section_path="Results",
            text=CHUNK_TEXT,
            embedding=[0.0] * EMBED_DIM,
            embedding_model="fake-embedder",
        )
    )
    s.flush()
    return ref


def _mk_citation_pc(
    s,
    wid,
    *,
    section_index,
    base_commit_sha,
    base_section_sha,
    reference_id,
    anchor=ANCHOR,
    chunk_index=0,
    evidence_quote=EVIDENCE_QUOTE,
    stance="supports",
    confidence="high",
):
    pc = propose_citation_insert(
        session=s,
        workspace_id=wid,
        section_index=section_index,
        base_commit_sha=base_commit_sha,
        base_section_sha=base_section_sha,
        reference_id=str(reference_id),
        anchor=anchor,
        chunk_index=chunk_index,
        evidence_quote=evidence_quote,
        stance=stance,
        confidence=confidence,
        origin_kind="human",
    )
    s.commit()
    return pc.id


def _kernel_setup(pg_session_factory, tmp_path, *, ref_citekey=None, ref_csl=None):
    """Create workspace + repo + collab-enabled draft + hydrated room + seeded ref.

    Returns (wid, oid, repo, room_doc, base_sha).
    """
    with pg_session_factory() as s:
        o = make_user(s, email=f"k-cite-{uuidlib.uuid4().hex[:8]}@umu.se")
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, KERNEL_MD0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        room_doc = _live_room(s, wid, str(tmp_path))
    # base_sha comes from the Yjs-normalised LIVE room markdown, section 1.
    live_md = pm_to_markdown(ydoc_to_pm_json(room_doc))
    base_sha = split_sections(live_md)[1].sha
    with pg_session_factory() as s:
        _mk_ref_and_chunk(s, wid, citekey=ref_citekey, csl_json=ref_csl)
        s.commit()
    return wid, oid, repo, room_doc, base_sha


def _get_ref(s, wid):
    return s.scalar(select(Reference).where(Reference.workspace_id == wid))


def test_kernel_accept_citation_applies_marker_and_cocommits_ledger(pg_session_factory, tmp_path):
    """Happy path: marker lands in the checkpointed proposal.md AND the ledger row
    lands in HEAD's citations.json (matching ev_id), with exactly one ArtifactVersion."""
    wid, oid, repo, room_doc, base_sha = _kernel_setup(
        pg_session_factory, tmp_path, ref_citekey="smith2021"
    )
    head_before = _git_head(repo)
    with pg_session_factory() as s:
        ref = _get_ref(s, wid)
        pc_id = _mk_citation_pc(
            s, wid, section_index=1, base_commit_sha=head_before,
            base_section_sha=base_sha, reference_id=ref.id,
        )
    with pg_session_factory() as s:
        from call_manager.db.models import User
        approver = s.get(User, oid)
        res = kernel_accept_citation(
            s,
            stamp=stamp_for(s, wid),
            room_doc=room_doc,
            proposed_change_id=pc_id,
            workspace_id=wid,
            approver=approver,
            workspaces_dir=str(tmp_path),
        )
        s.commit()
        assert res.pc.status == "accepted"
        assert res.committed is True
        assert res.update is not None
        assert res.evict is True

    # Both files are in the HEAD commit (proposal.md + citations.json).
    files_out = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=repo, capture_output=True, text=True,
    ).stdout
    files = {ln.strip() for ln in files_out.splitlines() if ln.strip()}
    assert PROPOSAL_FILENAME in files
    assert CITATIONS_FILENAME in files
    assert _git_head(repo) != head_before

    # Marker present in the checkpointed proposal.md, in section 1.
    disk = (repo / PROPOSAL_FILENAME).read_text()
    markers = find_markers(disk)
    assert len(markers) == 1, f"expected 1 marker, got {markers}"
    disk_ev_id = markers[0]["ev_id"]

    # Ledger row in HEAD matches the marker ev_id.
    ledger = load_ledger((repo / CITATIONS_FILENAME).read_text())
    assert len(ledger["evidence"]) == 1
    assert ledger["evidence"][0]["id"] == disk_ev_id

    # Exactly one ArtifactVersion.
    with pg_session_factory() as s:
        av = (
            s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid))
            .scalars()
            .all()
        )
        assert len(av) == 1 and av[0].created_by == "user"
        # The kernel stamped resulting_commit_sha == HEAD.
        assert s.get(ProposedChange, pc_id).resulting_commit_sha == _git_head(repo)


def test_kernel_accept_citation_stale_against_live_room(pg_session_factory, tmp_path):
    """A live uncheckpointed edit changes section 1's SHA → a citation pc whose
    base_section_sha is the pre-edit SHA is stale; git unchanged, no ledger row."""
    wid, oid, repo, room_doc, base_sha = _kernel_setup(
        pg_session_factory, tmp_path, ref_citekey="smith2021"
    )
    head = _git_head(repo)
    # a live edit to section 1 lands in the room + store, NOT checkpointed
    live_md = pm_to_markdown(ydoc_to_pm_json(room_doc))
    secs = split_sections(live_md)
    edited = "".join(
        "## Methods\nSee original. Plus a live uncommitted edit.\n" if x.index == 1 else x.text
        for x in secs
    )
    live_update = apply_pm_json_to_fragment(room_doc, markdown_to_pm(edited))
    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=oid, update=live_update)
        s.commit()
    assert _git_head(repo) == head
    with pg_session_factory() as s:
        ref = _get_ref(s, wid)
        pc_id = _mk_citation_pc(
            s, wid, section_index=1, base_commit_sha=head,
            base_section_sha=base_sha, reference_id=ref.id,
        )
    with pg_session_factory() as s:
        from call_manager.db.models import User
        approver = s.get(User, oid)
        res = kernel_accept_citation(
            s, stamp=stamp_for(s, wid), room_doc=room_doc, proposed_change_id=pc_id,
            workspace_id=wid, approver=approver, workspaces_dir=str(tmp_path),
        )
        s.commit()
        assert res.pc.status == "stale"
        assert res.update is None and res.evict is False
    # git untouched, no ledger row.
    assert _git_head(repo) == head
    assert not (repo / CITATIONS_FILENAME).exists()


def test_kernel_accept_citation_verify_fail_is_stale(pg_session_factory, tmp_path):
    """A citation pc whose evidence_quote is absent from the chunk → stale (git-first
    parity: all verify failures are terminal stale); git unchanged, no ledger row."""
    wid, oid, repo, room_doc, base_sha = _kernel_setup(
        pg_session_factory, tmp_path, ref_citekey="smith2021"
    )
    head = _git_head(repo)
    with pg_session_factory() as s:
        ref = _get_ref(s, wid)
        # A quote that is NOT present in CHUNK_TEXT.
        pc_id = _mk_citation_pc(
            s, wid, section_index=1, base_commit_sha=head,
            base_section_sha=base_sha, reference_id=ref.id,
            evidence_quote="this text is nowhere in the chunk",
        )
    with pg_session_factory() as s:
        from call_manager.db.models import User
        approver = s.get(User, oid)
        res = kernel_accept_citation(
            s, stamp=stamp_for(s, wid), room_doc=room_doc, proposed_change_id=pc_id,
            workspace_id=wid, approver=approver, workspaces_dir=str(tmp_path),
        )
        s.commit()
        assert res.pc.status == "stale"
        assert res.update is None and res.evict is False
    assert _git_head(repo) == head
    assert not (repo / CITATIONS_FILENAME).exists()


def test_kernel_accept_citation_anchor_moved_is_stale(pg_session_factory, tmp_path):
    """Duplicating the anchor string into a DIFFERENT section (target section SHA
    still fresh) makes insert_marker raise AnchorError (ambiguous) → caught → stale.

    The duplicate goes into the PREAMBLE (section 0), which precedes the target
    section 1: appending a NEW trailing section would flip section 1 from final to
    non-final and change its trailing-newline SHA, breaking the "target still fresh"
    premise. Editing the preamble leaves section 1 byte-for-byte identical.
    """
    wid, oid, repo, room_doc, base_sha = _kernel_setup(
        pg_session_factory, tmp_path, ref_citekey="smith2021"
    )
    head = _git_head(repo)
    live_md = pm_to_markdown(ydoc_to_pm_json(room_doc))
    secs = split_sections(live_md)
    assert secs[1].sha == base_sha  # sanity: target section unchanged pre-edit
    # Duplicate the anchor "original." into the preamble; the anchor is now
    # ambiguous over the WHOLE doc, which is what insert_marker scans.
    edited = "".join(
        ("preamble with original. also here\n\n" if x.index == 0 else x.text) for x in secs
    )
    dup_update = apply_pm_json_to_fragment(room_doc, markdown_to_pm(edited))
    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=oid, update=dup_update)
        s.commit()
    # Section 1's SHA is still base_sha (only the preamble changed).
    live_md2 = pm_to_markdown(ydoc_to_pm_json(room_doc))
    assert split_sections(live_md2)[1].sha == base_sha
    with pg_session_factory() as s:
        ref = _get_ref(s, wid)
        pc_id = _mk_citation_pc(
            s, wid, section_index=1, base_commit_sha=head,
            base_section_sha=base_sha, reference_id=ref.id,
        )
    with pg_session_factory() as s:
        from call_manager.db.models import User
        approver = s.get(User, oid)
        res = kernel_accept_citation(
            s, stamp=stamp_for(s, wid), room_doc=room_doc, proposed_change_id=pc_id,
            workspace_id=wid, approver=approver, workspaces_dir=str(tmp_path),
        )
        s.commit()
        assert res.pc.status == "stale"
        assert res.update is None and res.evict is False
    assert _git_head(repo) == head
    assert not (repo / CITATIONS_FILENAME).exists()


def test_kernel_accept_citation_idempotent_no_new_version(pg_session_factory, tmp_path):
    """Accept the same citation twice → the second accept is `accepted` with no new
    ArtifactVersion and resulting_commit_sha == HEAD (idempotent no-op derived from
    the LIVE room, not disk)."""
    wid, oid, repo, room_doc, base_sha = _kernel_setup(
        pg_session_factory, tmp_path, ref_citekey="smith2021"
    )
    head0 = _git_head(repo)
    from call_manager.db.models import User

    # First accept — commits the marker + ledger row.
    with pg_session_factory() as s:
        ref = _get_ref(s, wid)
        pc1 = _mk_citation_pc(
            s, wid, section_index=1, base_commit_sha=head0,
            base_section_sha=base_sha, reference_id=ref.id,
        )
    with pg_session_factory() as s:
        approver = s.get(User, oid)
        res1 = kernel_accept_citation(
            s, stamp=stamp_for(s, wid), room_doc=room_doc, proposed_change_id=pc1,
            workspace_id=wid, approver=approver, workspaces_dir=str(tmp_path),
        )
        s.commit()
        assert res1.pc.status == "accepted" and res1.committed is True
    head1 = _git_head(repo)
    assert head1 != head0

    # The target section changed (the marker is now in it), so key the SECOND pc to
    # the CURRENT live section 1 SHA — otherwise it would be stale, not idempotent.
    live_md = pm_to_markdown(ydoc_to_pm_json(room_doc))
    base_sha2 = split_sections(live_md)[1].sha
    with pg_session_factory() as s:
        ref = _get_ref(s, wid)
        pc2 = _mk_citation_pc(
            s, wid, section_index=1, base_commit_sha=head1,
            base_section_sha=base_sha2, reference_id=ref.id,
        )
    with pg_session_factory() as s:
        approver = s.get(User, oid)
        res2 = kernel_accept_citation(
            s, stamp=stamp_for(s, wid), room_doc=room_doc, proposed_change_id=pc2,
            workspace_id=wid, approver=approver, workspaces_dir=str(tmp_path),
        )
        s.commit()
        assert res2.pc.status == "accepted"
        assert res2.committed is False
        assert res2.update is None
        assert res2.evict is False
        assert res2.pc.resulting_commit_sha == head1  # == HEAD, no new commit

    # HEAD unchanged, still exactly one ArtifactVersion, one ledger row.
    assert _git_head(repo) == head1
    with pg_session_factory() as s:
        av = (
            s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid))
            .scalars()
            .all()
        )
        assert len(av) == 1
    ledger = load_ledger((repo / CITATIONS_FILENAME).read_text())
    assert len(ledger["evidence"]) == 1


def test_kernel_accept_citation_corrupt_ledger_is_stale(pg_session_factory, tmp_path):
    """Invalid JSON in citations.json on disk → the kernel maps the ValueError to
    terminal stale (git-first parity), does NOT evict the room, and leaves git
    unchanged."""
    wid, oid, repo, room_doc, base_sha = _kernel_setup(
        pg_session_factory, tmp_path, ref_citekey="smith2021"
    )
    # Write invalid JSON as citations.json (committed so it's part of the tree state).
    (repo / CITATIONS_FILENAME).write_text("{ this is not valid json ")
    subprocess.run(["git", "add", CITATIONS_FILENAME], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "corrupt ledger"], cwd=repo, check=True)
    head = _git_head(repo)
    with pg_session_factory() as s:
        ref = _get_ref(s, wid)
        pc_id = _mk_citation_pc(
            s, wid, section_index=1, base_commit_sha=head,
            base_section_sha=base_sha, reference_id=ref.id,
        )
    with pg_session_factory() as s:
        from call_manager.db.models import User
        approver = s.get(User, oid)
        res = kernel_accept_citation(
            s, stamp=stamp_for(s, wid), room_doc=room_doc, proposed_change_id=pc_id,
            workspace_id=wid, approver=approver, workspaces_dir=str(tmp_path),
        )
        s.commit()
        assert res.pc.status == "stale"
        assert res.update is None
        assert res.evict is False  # room NOT evicted
    # git unchanged (still the corrupt-ledger commit); no marker applied.
    assert _git_head(repo) == head
    assert (repo / CITATIONS_FILENAME).read_text() == "{ this is not valid json "
    assert find_markers((repo / PROPOSAL_FILENAME).read_text()) == []


def test_kernel_accept_citation_caches_citekey_in_one_transaction(pg_session_factory, tmp_path):
    """Codex High: after a successful kernel accept, ref.citekey equals the minted
    citekey (cache write) AND the whole accept is ONE transaction — the kernel never
    commits mid-way, so an uncommitted session shows the accept only after commit."""
    # Seed the Reference with citekey=None and CSL so a deterministic key is minted.
    wid, oid, repo, room_doc, base_sha = _kernel_setup(
        pg_session_factory, tmp_path, ref_citekey=None, ref_csl=_CSL_WITH_AUTHOR
    )
    head = _git_head(repo)
    with pg_session_factory() as s:
        ref = _get_ref(s, wid)
        assert ref.citekey is None
        pc_id = _mk_citation_pc(
            s, wid, section_index=1, base_commit_sha=head,
            base_section_sha=base_sha, reference_id=ref.id,
        )
    from call_manager.db.models import User

    with pg_session_factory() as s:
        approver = s.get(User, oid)
        res = kernel_accept_citation(
            s, stamp=stamp_for(s, wid), room_doc=room_doc, proposed_change_id=pc_id,
            workspace_id=wid, approver=approver, workspaces_dir=str(tmp_path),
        )
        # BEFORE the caller commits, a SEPARATE session must NOT see the accepted
        # state — proving the kernel did not commit mid-way (ONE transaction).
        with pg_session_factory() as other:
            assert other.get(ProposedChange, pc_id).status == "pending"
            assert _get_ref(other, wid).citekey is None
        assert res.pc.status == "accepted"
        s.commit()

    # After the single commit: ref.citekey == the minted citekey, and it matches
    # the ledger row's citekey.
    ledger = load_ledger((repo / CITATIONS_FILENAME).read_text())
    assert len(ledger["evidence"]) == 1
    minted_citekey = ledger["evidence"][0]["citekey"]
    assert minted_citekey  # a real key was minted
    with pg_session_factory() as s:
        ref = _get_ref(s, wid)
        assert ref.citekey == minted_citekey
        assert s.get(ProposedChange, pc_id).status == "accepted"
