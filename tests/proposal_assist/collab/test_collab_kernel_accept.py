# tests/proposal_assist/collab/test_collab_kernel_accept.py
#
# DEVIATION FROM BRIEF: The brief used section_index=0 throughout, but
# split_sections() ALWAYS emits an empty preamble at index 0, so "## Background"
# is section 1.  Using index 0 caused:
#   - test 1/2: SectionStructureError (preamble must not start with ##)
#   - test 3: newline-glue turned "" into "\n", defeating the no-op check
# Fix: changed section_index=0→1 and _section_sha(md0,0)→split_sections(live_md)[1].sha
# everywhere (base_sha must come from the Yjs-normalised live text because
# pm_to_markdown inserts a blank line between heading and paragraph).
import subprocess
import uuid as uuidlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from call_manager.db.models import ArtifactVersion, ProposedChange, User, Workspace
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME, Artifact, filename_for
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.hydrate import build_doc, load_or_hydrate
from call_manager.proposal_assist.collab.kernel import kernel_accept_prose
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import (
    apply_pm_json_to_fragment,
    pm_json_to_ydoc,
    ydoc_to_pm_json,
)
from call_manager.proposal_assist.editing.save import SpecLockedError
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.proposals.accepting import ProposedChangeNotFound
from call_manager.proposal_assist.proposals.kernel_accept import _CLEAN_ACCEPT_ERRORS
from tests.proposal_assist.conftest import make_user, make_workspace, stamp_for


def _init_repo(repo: Path, content: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


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
):
    pc = ProposedChange(
        id=uuidlib.uuid4(),
        workspace_id=wid,
        status="pending",
        target_artifact=artifact,
        section_index=section_index,
        base_commit_sha=base_commit_sha,
        base_section_sha=base_section_sha,
        kind=kind,
        payload={"new_text": new_text},
        origin_kind="human",
    )
    session.add(pc)
    session.commit()
    return pc.id


def test_kernel_accept_applies_edit_and_checkpoints(pg_session_factory, tmp_path):
    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="k-accept@umu.se")
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        room_doc = _live_room(s, wid, str(tmp_path))
    # Section 1 is "## Background…" — section 0 is always the empty preamble.
    # base_sha must come from the live room because pm_to_markdown normalises
    # "## Heading\nbody" into "## Heading\n\nbody" (blank line between blocks).
    live_md = pm_to_markdown(ydoc_to_pm_json(room_doc))
    base_sha = split_sections(live_md)[1].sha
    head = _git_head(repo)
    with pg_session_factory() as s:
        pc_id = _mk_pc(
            s,
            wid,
            section_index=1,
            new_text="## Background\nACCEPTED body.\n",
            base_commit_sha=head,
            base_section_sha=base_sha,
        )

    with pg_session_factory() as s:
        approver = s.get(type(o), oid)
        res = kernel_accept_prose(
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
        assert res.pc.resulting_commit_sha and res.committed is True
        assert res.update is not None

    # git HEAD now holds the accepted content; the room doc reflects it
    disk = (repo / PROPOSAL_FILENAME).read_text()
    assert "ACCEPTED body." in disk
    assert "".join(sec.text for sec in split_sections(disk)) == disk
    assert "ACCEPTED body." in pm_to_markdown(ydoc_to_pm_json(room_doc))
    with pg_session_factory() as s:
        assert res.pc.resulting_commit_sha == s.get(ProposedChange, pc_id).resulting_commit_sha
        av = (
            s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid))
            .scalars()
            .all()
        )
        assert len(av) == 1 and av[0].created_by == "user"
        # the update was tagged to the approver
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
        assert rows[-1][1] == oid


def test_kernel_accept_detects_staleness_against_LIVE_room_not_git(pg_session_factory, tmp_path):
    # THE regression guard: an edit that is live-but-not-checkpointed leaves git
    # HEAD == base_commit_sha, yet the accept must detect the live section changed.
    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="k-stale@umu.se")
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        room_doc = _live_room(s, wid, str(tmp_path))
    head = _git_head(repo)
    # base_sha = the ORIGINAL section 1 SHA (from the live room before the live edit)
    live_md = pm_to_markdown(ydoc_to_pm_json(room_doc))
    base_sha = split_sections(live_md)[1].sha  # proposal's base = the ORIGINAL section

    # a live edit lands in the room + store, but is NOT checkpointed (git HEAD unchanged)
    secs = split_sections(live_md)
    edited = "".join(
        "## Background\nlive edit, uncommitted.\n" if x.index == 1 else x.text
        for x in secs
    )
    live_update = apply_pm_json_to_fragment(room_doc, markdown_to_pm(edited))
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=oid, update=live_update
        )
    # git HEAD is still the original:
    assert _git_head(repo) == head

    with pg_session_factory() as s:
        pc_id = _mk_pc(
            s,
            wid,
            section_index=1,
            new_text="## Background\nproposed.\n",
            base_commit_sha=head,
            base_section_sha=base_sha,
        )
    with pg_session_factory() as s:
        approver = s.get(type(o), oid)
        res = kernel_accept_prose(
            s,
            stamp=stamp_for(s, wid),
            room_doc=room_doc,
            proposed_change_id=pc_id,
            workspace_id=wid,
            approver=approver,
            workspaces_dir=str(tmp_path),
        )
        s.commit()
        assert res.pc.status == "stale"  # live section SHA != base_section_sha
        assert res.update is None and res.evict is False
    # git untouched, nothing accepted
    assert "proposed." not in (repo / PROPOSAL_FILENAME).read_text()


def test_kernel_accept_noop_proposal_accepts_without_new_commit(pg_session_factory, tmp_path):
    md0 = "## Background\nsame.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="k-noop@umu.se")
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        room_doc = _live_room(s, wid, str(tmp_path))
    live_md = pm_to_markdown(ydoc_to_pm_json(room_doc))
    # section 1 = "## Background…"; section 0 is always the empty preamble
    live_sec1 = split_sections(live_md)[1]
    head = _git_head(repo)
    with pg_session_factory() as s:
        # propose EXACTLY the live text -> no-op
        pc_id = _mk_pc(
            s,
            wid,
            section_index=1,
            new_text=live_sec1.text,
            base_commit_sha=head,
            base_section_sha=live_sec1.sha,
        )
    with pg_session_factory() as s:
        approver = s.get(type(o), oid)
        res = kernel_accept_prose(
            s,
            stamp=stamp_for(s, wid),
            room_doc=room_doc,
            proposed_change_id=pc_id,
            workspace_id=wid,
            approver=approver,
            workspaces_dir=str(tmp_path),
        )
        s.commit()
        assert res.pc.status == "accepted"  # no-op still accepts
        assert res.committed is False and res.update is None
    with pg_session_factory() as s:
        av = (
            s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid))
            .scalars()
            .all()
        )
        assert av == []  # no new version


def test_proposed_change_not_found_in_clean_errors():
    """ProposedChangeNotFound must be in _CLEAN_ACCEPT_ERRORS so that a missing row
    on the kernel path does not trigger an unnecessary room eviction."""
    assert ProposedChangeNotFound in _CLEAN_ACCEPT_ERRORS


def test_kernel_accept_raises_proposed_change_not_found_for_missing_pc(
    pg_session_factory, tmp_path
):
    """kernel_accept_prose raises ProposedChangeNotFound (not any other error) when
    the proposed_changes row does not exist — and it does so BEFORE any doc mutation."""
    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="k-notfound@umu.se")
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        room_doc = _live_room(s, wid, str(tmp_path))

    nonexistent_pc_id = uuidlib.uuid4()
    with pg_session_factory() as s:
        approver = s.get(type(o), oid)
        try:
            kernel_accept_prose(
                s,
                stamp=stamp_for(s, wid),
                room_doc=room_doc,
                proposed_change_id=nonexistent_pc_id,
                workspace_id=wid,
                approver=approver,
                workspaces_dir=str(tmp_path),
            )
        except ProposedChangeNotFound:
            pass
        else:
            raise AssertionError("expected ProposedChangeNotFound")


def _init_spec_repo(repo: Path, content: str) -> None:
    """Init a repo whose committed HEAD holds the SPEC artifact file."""
    fname = filename_for(Artifact.SPEC)
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / fname).write_text(content)
    subprocess.run(["git", "add", fname], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def test_kernel_rejects_accept_into_locked_spec(pg_session_factory, tmp_path):
    """A prose accept against a LOCKED spec raises SpecLockedError (kernel spec-lock
    guard, kernel.py). The guard runs AFTER acquire_commit_mutex (Codex H3) and BEFORE
    any git I/O, so it needs no repo — the kernel takes the room_doc directly."""
    spec_md = "## Aims\noriginal aims.\n"
    room_doc = pm_json_to_ydoc(markdown_to_pm(spec_md))
    base_sha = split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))[1].sha
    with pg_session_factory() as s:
        o = make_user(s, email="k-speclock@umu.se")
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    with pg_session_factory() as s:  # lock the spec
        ws = s.get(Workspace, wid)
        ws.spec_locked_at = datetime.now(UTC)
        ws.spec_locked_by = oid
        s.commit()
    with pg_session_factory() as s:
        pc_id = _mk_pc(
            s, wid, section_index=1, new_text="## Aims\nedited aims.\n",
            base_commit_sha="0" * 40, base_section_sha=base_sha, artifact="spec",
        )
    with pg_session_factory() as s:
        approver = s.get(User, oid)
        with pytest.raises(SpecLockedError):
            kernel_accept_prose(
                s, stamp=stamp_for(s, wid), room_doc=room_doc, proposed_change_id=pc_id,
                workspace_id=wid, approver=approver, workspaces_dir=str(tmp_path),
            )
    # The rejected proposal was NOT resolved (still pending — the guard raised).
    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).status == "pending"


def test_kernel_accepts_unlocked_spec_and_checkpoints(pg_session_factory, tmp_path):
    """A prose accept against an UNLOCKED spec succeeds and checkpoints — exercises the
    H3 ordering (acquire_commit_mutex BEFORE the spec-lock guard) on a real spec
    artifact and confirms the spec file is updated in git."""
    spec_md = "## Aims\noriginal aims.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="k-specok@umu.se")
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_spec_repo(repo, spec_md)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="spec")
        s.commit()
    with pg_session_factory() as s:
        updates = load_or_hydrate(
            s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="spec",
            workspaces_dir=str(tmp_path),
        )
        s.commit()
        room_doc = build_doc(updates)
    base_sha = split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))[1].sha
    with pg_session_factory() as s:
        pc_id = _mk_pc(
            s, wid, section_index=1, new_text="## Aims\nACCEPTED spec body.\n",
            base_commit_sha=_git_head(repo), base_section_sha=base_sha, artifact="spec",
        )
    with pg_session_factory() as s:
        approver = s.get(User, oid)
        res = kernel_accept_prose(
            s, stamp=stamp_for(s, wid), room_doc=room_doc, proposed_change_id=pc_id,
            workspace_id=wid, approver=approver, workspaces_dir=str(tmp_path),
        )
        s.commit()
        assert res.pc.status == "accepted"
        assert res.committed is True and res.update is not None
    disk = (repo / filename_for(Artifact.SPEC)).read_text()
    assert "ACCEPTED spec body." in disk
