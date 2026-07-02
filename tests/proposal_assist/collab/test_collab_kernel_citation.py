# tests/proposal_assist/collab/test_collab_kernel_citation.py  (Task 1 portion)
"""Characterization tests for compute_citation_insert (Task 1) and the git-first
citation-accept path.

Task 3 will add kernel_accept_citation tests to this same file.
"""
import subprocess
from pathlib import Path

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.citations.markers import find_markers
from call_manager.proposal_assist.proposals.accepting import (
    accept_proposed_change,
)
from call_manager.proposal_assist.proposals.proposing import (
    capture_base,
    propose_citation_insert,
)
from call_manager.proposal_assist.tools.citations import CITATIONS_FILENAME
from tests.proposal_assist.conftest import make_user, make_workspace

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
    from call_manager.proposal_assist.tools import citations as citations_mod
    from call_manager.proposal_assist.tools.citations import compute_citation_insert

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
        approver = make_user(s, email="approver-collab@ki.se")
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
