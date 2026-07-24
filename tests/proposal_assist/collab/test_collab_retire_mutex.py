"""Mutex-participating collab writers refuse a deleted workspace (Slice B-mutex).

The load-bearing cases are the STALE-proposal ones. Both accept entry points
resolve a ProposedChange to "stale" on an early-exit branch that sits ABOVE any
mutex, and their wrapper commits — so merely swapping acquire_commit_mutex for
acquire_commit_mutex_live in place would still durably mutate a deleted
workspace. These tests distinguish "replaced the acquire" from "hoisted it".
"""

import subprocess
import uuid as uuidlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from call_manager.db.models import ProposedChange, User, Workspace
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.collab import state
from call_manager.proposal_assist.collab.hydrate import build_doc, load_or_hydrate
from call_manager.proposal_assist.collab.kernel import (
    kernel_accept_citation,
    kernel_accept_prose,
    kernel_init_spec,
)
from call_manager.proposal_assist.collab.markdown_bridge import pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json
from call_manager.proposal_assist.drafting.locking import WorkspaceDeleted
from call_manager.proposal_assist.editing.sectionizer import split_sections
from tests.proposal_assist.conftest import make_user, make_workspace


def _init_repo(repo: Path, content: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _git_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def _setup(pg_session_factory, tmp_path, email):
    """ws + git repo + enabled collab + a hydrated room doc. Returns the pieces
    every test below needs."""
    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        owner = make_user(s, email=email)
        ws = make_workspace(s, owner=owner, current_phase="active")
        s.commit()
        wid, oid = ws.id, owner.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        updates = load_or_hydrate(
            s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path)
        )
        s.commit()
        room_doc = build_doc(updates)
    live_md = pm_to_markdown(ydoc_to_pm_json(room_doc))
    base_sha = split_sections(live_md)[1].sha
    return wid, oid, repo, room_doc, base_sha


def _mk_pc(session, wid, *, base_section_sha, base_commit_sha, kind, payload):
    pc = ProposedChange(
        id=uuidlib.uuid4(),
        workspace_id=wid,
        status="pending",
        target_artifact="draft",
        section_index=1,
        base_commit_sha=base_commit_sha,
        base_section_sha=base_section_sha,
        kind=kind,
        payload=payload,
        origin_kind="human",
    )
    session.add(pc)
    session.commit()
    return pc.id


def _delete(pg_session_factory, wid):
    with pg_session_factory() as s:
        s.get(Workspace, wid).soft_deleted_at = datetime.now(UTC)
        s.commit()


# ---------------------------------------------------------------------------
# kernel_accept_prose
# ---------------------------------------------------------------------------


def test_kernel_accept_prose_refuses_a_deleted_workspace(pg_session_factory, tmp_path):
    wid, oid, repo, room_doc, base_sha = _setup(pg_session_factory, tmp_path, "kp-live@ki.se")
    head = _git_head(repo)
    with pg_session_factory() as s:
        pc_id = _mk_pc(
            s, wid, base_section_sha=base_sha, base_commit_sha=head,
            kind="prose_edit", payload={"new_text": "## Background\nnew.\n"},
        )
    _delete(pg_session_factory, wid)

    with pg_session_factory() as s:
        approver = s.get(User, oid)
        with pytest.raises(WorkspaceDeleted):
            kernel_accept_prose(
                s, room_doc=room_doc, proposed_change_id=pc_id, workspace_id=wid,
                approver=approver, workspaces_dir=str(tmp_path),
            )
        s.rollback()

    assert _git_head(repo) == head  # no commit
    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).status == "pending"


def test_STALE_prose_proposal_on_a_deleted_workspace_is_not_resolved(
    pg_session_factory, tmp_path
):
    """REGRESSION. kernel.py's staleness branch flushes status="stale" ABOVE the
    original acquire, and accept_via_kernel commits. With the acquire merely
    swapped in place, a deleted workspace still gets a durable row mutation."""
    wid, oid, repo, room_doc, _base_sha = _setup(pg_session_factory, tmp_path, "kp-stale@ki.se")
    head = _git_head(repo)
    with pg_session_factory() as s:
        pc_id = _mk_pc(
            s, wid,
            base_section_sha="0" * 64,  # deliberately STALE vs the live section
            base_commit_sha=head,
            kind="prose_edit", payload={"new_text": "## Background\nnew.\n"},
        )
    _delete(pg_session_factory, wid)

    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted):
            kernel_accept_prose(
                s, room_doc=room_doc, proposed_change_id=pc_id, workspace_id=wid,
                approver=s.get(User, oid), workspaces_dir=str(tmp_path),
            )
        s.rollback()

    with pg_session_factory() as s:
        pc = s.get(ProposedChange, pc_id)
        assert pc.status == "pending"  # NOT "stale"
        assert pc.resolved_by is None
        assert pc.resolved_at is None


# ---------------------------------------------------------------------------
# kernel_accept_citation
# ---------------------------------------------------------------------------


def test_STALE_citation_proposal_on_a_deleted_workspace_is_not_resolved(
    pg_session_factory, tmp_path
):
    """REGRESSION, second path. _stale() fires when apply_citation_to_room raises
    CiteStale — and that raise is at kernel.py:127, ABOVE that function's own
    acquire. Guarding only the inner acquire leaves this unguarded."""
    wid, oid, repo, room_doc, _base_sha = _setup(pg_session_factory, tmp_path, "kc-stale@ki.se")
    head = _git_head(repo)
    with pg_session_factory() as s:
        pc_id = _mk_pc(
            s, wid,
            base_section_sha="0" * 64,  # CiteStale fires before any anchor work
            base_commit_sha=head,
            kind="citation_insert",
            payload={
                "anchor": "original.",
                "reference_id": 1,
                "chunk_index": 0,
                "evidence_quote": "q",
            },
        )
    _delete(pg_session_factory, wid)

    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted):
            kernel_accept_citation(
                s, room_doc=room_doc, proposed_change_id=pc_id, workspace_id=wid,
                approver=s.get(User, oid), workspaces_dir=str(tmp_path),
            )
        s.rollback()

    with pg_session_factory() as s:
        pc = s.get(ProposedChange, pc_id)
        assert pc.status == "pending"  # NOT "stale"
        assert pc.resolved_by is None


# ---------------------------------------------------------------------------
# the remaining kernel mutex sites
# ---------------------------------------------------------------------------


def test_kernel_init_spec_refuses_a_deleted_workspace(pg_session_factory, tmp_path):
    wid, oid, _repo, room_doc, _sha = _setup(pg_session_factory, tmp_path, "ki-spec@ki.se")
    _delete(pg_session_factory, wid)
    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted):
            kernel_init_spec(
                s, room_doc=room_doc, workspace_id=wid, workspaces_dir=str(tmp_path),
                actor_id=oid,
            )
        s.rollback()


def test_workspace_deleted_is_a_clean_error_so_a_refusal_never_evicts_a_room():
    """Raised where CommitMutexTimeout is — before any doc mutation. If it were
    not in these tuples a refused write would evict a healthy room and disconnect
    everyone editing it."""
    from call_manager.proposal_assist.citations.collab_cite import _CLEAN_CITE_ERRORS
    from call_manager.proposal_assist.proposals.kernel_accept import _CLEAN_ACCEPT_ERRORS

    assert WorkspaceDeleted in _CLEAN_ACCEPT_ERRORS
    assert WorkspaceDeleted in _CLEAN_CITE_ERRORS
