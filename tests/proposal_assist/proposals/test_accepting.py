import subprocess
from pathlib import Path

import pytest

from call_manager.db.models import ProposedChange, Reference, ReferenceChunk
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.citations.markers import find_markers
from call_manager.proposal_assist.editing.save import save_section
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.proposals.accepting import (
    ProposedChangeNotFound,
    ProposedChangeNotPending,
    accept_proposed_change,
)
from call_manager.proposal_assist.proposals.proposing import (
    capture_base,
    propose_citation_insert,
    propose_prose_edit,
)
from call_manager.proposal_assist.tools.citations import CITATIONS_FILENAME
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from tests.proposal_assist.conftest import make_user, make_workspace


def _init_repo(repo_dir: Path, content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def _setup(s, tmp_path):
    ws = make_workspace(s)
    s.flush()
    repo = Path(tmp_path) / str(ws.id) / "repo"
    _init_repo(repo, "preamble\n\n## Methods\noriginal.\n")
    return ws, repo


EMBED_DIM = 1024


def _seed_reference(s, workspace_id, *, chunk_text, citekey=None):
    ref = Reference(
        workspace_id=workspace_id,
        kind="paper",
        source_type="uploaded",
        filename="seed.pdf",
        storage_path="seed/seed.pdf",
        mime_type="application/pdf",
        citekey=citekey,
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
    return ref.id


def test_accept_prose_applies_and_records_approver(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\nrewritten.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
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

        got = s.get(ProposedChange, pc.id)
        assert got.status == "accepted"
        assert got.resolved_by == approver.id
        assert got.resulting_commit_sha
        assert "rewritten." in (repo / PROPOSAL_FILENAME).read_text()


def test_accept_non_pending_is_rejected(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\nrewritten.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            origin_kind="interactive_turn",
        )
        pc.status = "rejected"
        s.flush()
        with pytest.raises(ProposedChangeNotPending):
            accept_proposed_change(
                session=s,
                proposed_change_id=pc.id,
                workspace_id=ws.id,
                approver=approver,
                workspaces_dir=str(tmp_path),
            )


def test_accept_does_not_clobber_human_edit_race(pg_session_factory, tmp_path):
    # Proposer READS at the original base; a human edits the SAME section before
    # the proposal is accepted. Accept must go stale, not overwrite (finding #1).
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()

        base_commit, base_section = capture_base(repo, 1)  # T0: proposer's read

        # T1: a human edits the section and commits — section sha moves.
        save_section(
            session=s,
            workspace=ws,
            user_id=ws.owner_id,
            section_index=1,
            new_text="## Methods\nhuman-edited.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            workspaces_dir=str(tmp_path),
        )
        s.flush()

        # T2: proposer proposes using the now-stale T0 snapshot.
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\nagent-version.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
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

        got = s.get(ProposedChange, pc.id)
        assert got.status == "stale"
        # stale-on-accept-attempt is stamped (distinguishes it from a passive sweep)
        assert got.resolved_by == approver.id
        assert got.resolved_at is not None
        text = (repo / PROPOSAL_FILENAME).read_text()
        assert "human-edited." in text  # human edit preserved
        assert "agent-version." not in text  # agent proposal NOT applied


def test_accept_rejects_mismatched_workspace(pg_session_factory, tmp_path):
    # A member of workspace B cannot accept a proposed_change from workspace A
    # by calling it under B's path (finding #2).
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        other_owner = make_user(s, email="other-owner@ki.se")
        other = make_workspace(s, owner=other_owner)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\nx.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            origin_kind="interactive_turn",
        )
        s.flush()
        with pytest.raises(ProposedChangeNotFound):
            accept_proposed_change(
                session=s,
                proposed_change_id=pc.id,
                workspace_id=other.id,
                approver=approver,
                workspaces_dir=str(tmp_path),
            )


def test_accept_noop_proposal_is_accepted_at_current_head(pg_session_factory, tmp_path):
    # A no-op proposal (new_text == live section text, base unmoved) makes
    # save_section short-circuit (changed=False, no new commit). The proposal is
    # still "accepted" and resulting_commit_sha is the unchanged HEAD (pins the
    # return contract for the changed=False path).
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        base_commit, base_section = capture_base(repo, 1)
        live_text = split_sections((repo / PROPOSAL_FILENAME).read_text())[1].text
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text=live_text,
            base_commit_sha=base_commit,
            base_section_sha=base_section,
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

        got = s.get(ProposedChange, pc.id)
        assert got.status == "accepted"
        assert got.resulting_commit_sha == base_commit  # HEAD did not move
        assert "original." in (repo / PROPOSAL_FILENAME).read_text()


def test_accept_rejects_spec_target(pg_session_factory, tmp_path):
    # SP2a accepts the draft path only; a spec-target row is deferred to SP2b.
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\nx.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            origin_kind="interactive_turn",
        )
        pc.target_artifact = "spec"
        s.flush()
        with pytest.raises(NotImplementedError):
            accept_proposed_change(
                session=s,
                proposed_change_id=pc.id,
                workspace_id=ws.id,
                approver=approver,
                workspaces_dir=str(tmp_path),
            )


CHUNK_TEXT = "Adjuvant therapy reduces recurrence by 30% in the treated cohort."


def test_accept_citation_inserts_marker_and_ledger_row(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        rid = _seed_reference(s, ws.id, chunk_text=CHUNK_TEXT, citekey="smith2021")
        base_commit, base_section = capture_base(repo, 1)
        # "original." is the anchor — it lives in section 1 (## Methods).
        pc = propose_citation_insert(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            reference_id=str(rid),
            anchor="original.",
            chunk_index=0,
            evidence_quote="reduces recurrence by 30%",
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

        got = s.get(ProposedChange, pc.id)
        assert got.status == "accepted"
        assert got.resolved_by == approver.id
        assert got.resulting_commit_sha
        # Marker landed in proposal.md right after the anchor.
        proposal_text = (repo / PROPOSAL_FILENAME).read_text()
        markers = find_markers(proposal_text)
        assert len(markers) == 1
        assert markers[0]["citekey"] == "smith2021"
        # Ledger row landed in citations.json.
        ledger = load_ledger((repo / CITATIONS_FILENAME).read_text())
        assert len(ledger["evidence"]) == 1
        assert ledger["evidence"][0]["evidence_quote"] == "reduces recurrence by 30%"


def test_accept_citation_verify_fail_goes_stale(pg_session_factory, tmp_path):
    # The anchor moved out of the document between propose and accept: the
    # accept-time re-verify fails, so the row flips to stale (parallels prose's
    # StaleSectionError->stale), stamped with the resolver, NO marker written.
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        rid = _seed_reference(s, ws.id, chunk_text=CHUNK_TEXT, citekey="smith2021")
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_citation_insert(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            reference_id=str(rid),
            anchor="this anchor no longer exists",
            chunk_index=0,
            evidence_quote="reduces recurrence by 30%",
            stance="supports",
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

        got = s.get(ProposedChange, pc.id)
        assert got.status == "stale"
        assert got.resolved_by == approver.id
        assert got.resolved_at is not None
        # No marker / ledger written.
        assert find_markers((repo / PROPOSAL_FILENAME).read_text()) == []


def test_accept_citation_atomic_when_citekey_cache_branch_runs(pg_session_factory, tmp_path):
    # BLOCKER-1 regression: when the Reference has NO citekey yet, the replay's
    # citekey-cache branch (ref.citekey != minted_citekey) RUNS. With owns_txn=False
    # it must FLUSH (not commit) so the FOR UPDATE pc-row lock + advisory mutex stay
    # held through the status flip — git/ledger write + status flip + citekey update
    # all land in ONE transaction. (Seeding citekey=None is what exercises this
    # branch; the citekey="smith2021" tests above skip it and would mask the bug.)
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        rid = _seed_reference(s, ws.id, chunk_text=CHUNK_TEXT, citekey=None)
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_citation_insert(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            reference_id=str(rid),
            anchor="original.",
            chunk_index=0,
            evidence_quote="reduces recurrence by 30%",
            stance="supports",
            confidence="high",
            origin_kind="interactive_turn",
        )
        s.flush()

        # BLOCKER-1 guard (must actually catch the regression): with owns_txn=False
        # the replay MUST NOT commit mid-way — it may only flush. A mid-replay
        # commit would release the FOR UPDATE pc-row lock + advisory mutex before
        # the status flip, splitting the accept across two transactions. Spy on
        # commit and prove the replay issues ZERO commits; only the test's own
        # explicit commit below persists everything in ONE transaction. (Without
        # this assertion the test passes even with an unconditional mid-replay
        # commit, because the final commit below still lands the status flip.)
        commits_during_replay = 0
        real_commit = s.commit

        def _spy_commit(*args, **kwargs):
            nonlocal commits_during_replay
            commits_during_replay += 1
            return real_commit(*args, **kwargs)

        s.commit = _spy_commit
        try:
            accept_proposed_change(
                session=s,
                proposed_change_id=pc.id,
                workspace_id=ws.id,
                approver=approver,
                workspaces_dir=str(tmp_path),
            )
        finally:
            s.commit = real_commit
        assert commits_during_replay == 0, (
            "citation accept replay committed mid-way (owns_txn guard regressed): "
            "the FOR UPDATE pc-row lock + advisory mutex would release before the "
            "status flip, splitting the accept across two transactions"
        )

        # Single commit at the END persists everything atomically (the replay only
        # flushed). Everything must be consistent post-commit.
        s.commit()

        got = s.get(ProposedChange, pc.id)
        assert got.status == "accepted"
        assert got.resolved_by == approver.id
        assert got.resulting_commit_sha
        # Marker landed in proposal.md.
        proposal_text = (repo / PROPOSAL_FILENAME).read_text()
        markers = find_markers(proposal_text)
        assert len(markers) == 1
        minted = markers[0]["citekey"]
        # Ledger row landed in citations.json.
        ledger = load_ledger((repo / CITATIONS_FILENAME).read_text())
        assert len(ledger["evidence"]) == 1
        # The citekey-cache branch ran and updated the Reference in the SAME txn.
        got_ref = s.get(Reference, rid)
        assert got_ref.citekey == minted


def test_resolve_takes_row_lock_blocking_concurrent_resolver(pg_session_factory, tmp_path):
    # The pending->terminal transition must be serialized. _load_scoped locks the
    # row FOR UPDATE, so while one session is resolving it, a second session cannot
    # grab the same row concurrently (which is what otherwise lets a double-click /
    # retry / two-member race corrupt the audit row). We prove the lock is held by
    # showing a NOWAIT acquisition from a second connection fails.
    from sqlalchemy import select
    from sqlalchemy.exc import OperationalError

    from call_manager.proposal_assist.proposals.accepting import _load_scoped

    with pg_session_factory() as setup:
        ws, repo = _setup(setup, tmp_path)
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_prose_edit(
            session=setup,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\nx.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            origin_kind="interactive_turn",
        )
        setup.commit()
        pc_id, ws_id = pc.id, ws.id

    holder = pg_session_factory()
    try:
        locked = _load_scoped(holder, pc_id, ws_id)  # SELECT ... FOR UPDATE
        assert locked.status == "pending"
        # A second connection cannot lock the same row while `holder` holds it.
        with pg_session_factory() as contender:
            with pytest.raises(OperationalError):
                contender.scalar(
                    select(ProposedChange)
                    .where(
                        ProposedChange.id == pc_id,
                        ProposedChange.workspace_id == ws_id,
                    )
                    .with_for_update(nowait=True)
                )
            contender.rollback()
    finally:
        holder.rollback()
        holder.close()


def test_reject_sets_status_and_approver(pg_session_factory, tmp_path):
    from call_manager.proposal_assist.proposals.accepting import reject_proposed_change

    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\nx.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            origin_kind="interactive_turn",
        )
        s.flush()
        reject_proposed_change(
            session=s, proposed_change_id=pc.id, workspace_id=ws.id, approver=approver
        )
        s.flush()
        got = s.get(ProposedChange, pc.id)
        assert got.status == "rejected"
        assert got.resolved_by == approver.id
        assert got.resolved_at is not None
