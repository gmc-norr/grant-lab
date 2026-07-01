# src/call_manager/proposal_assist/collab/kernel.py
"""Room-aware write kernel (Slice 2b-1): accept a prose proposal against the
LIVE Yjs room, not git. Validates staleness against the live section SHA,
applies the accepted edit as an authenticated incremental Yjs update, stages it,
and checkpoints — all on the caller's single transaction (the caller commits)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pycrdt import Doc
from sqlalchemy.orm import Session

from call_manager.db.models import ProposedChange, User, Workspace
from call_manager.proposal_assist.collab import store
from call_manager.proposal_assist.collab.checkpoint import _head_or_empty, checkpoint_artifact
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import (
    apply_pm_json_to_fragment,
    ydoc_to_pm_json,
)
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex
from call_manager.proposal_assist.editing.save import SectionStructureError, SpecLockedError
from call_manager.proposal_assist.editing.sectionizer import is_h2_heading_line, split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.proposals.accepting import _load_scoped


class CitationKernelDeferred(Exception):
    """A citation_insert accept on a collab-enabled artifact. Routing this through
    the kernel is Slice 2b-2; until then it must NOT fall through to the git-first
    path (which would write git while Yjs is canonical). Endpoint maps to 409."""


@dataclass
class KernelAcceptResult:
    pc: ProposedChange
    update: bytes | None
    committed: bool
    evict: bool


def _live_sections(room_doc: Doc):
    return split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))


def kernel_accept_prose(
    session: Session,
    *,
    room_doc: Doc,
    proposed_change_id: uuid.UUID,
    workspace_id: uuid.UUID,
    approver: User,
    workspaces_dir,
) -> KernelAcceptResult:
    pc = _load_scoped(session, proposed_change_id, workspace_id)  # FOR UPDATE
    if pc.status != "pending":
        from call_manager.proposal_assist.proposals.accepting import ProposedChangeNotPending

        raise ProposedChangeNotPending(pc.status)
    if pc.kind != "prose_edit":
        # citation_insert (and any future kind) is 2b-2 on the collab path.
        raise CitationKernelDeferred(pc.kind)

    artifact = pc.target_artifact
    sections = _live_sections(room_doc)
    if not 0 <= pc.section_index < len(sections):
        raise IndexError(f"section_index {pc.section_index} out of range")
    live = sections[pc.section_index]

    # THE regression guard: staleness vs the LIVE section SHA (never git HEAD).
    if live.sha != pc.base_section_sha:
        pc.status = "stale"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        session.flush()
        return KernelAcceptResult(pc=pc, update=None, committed=False, evict=False)

    # Acquire the commit mutex NOW — before reading spec_locked_at (Codex H3).
    # post_lock_spec (workspaces.py:487) sets the lock UNDER this same mutex; checking
    # spec_locked_at before taking the mutex would let a concurrent lock commit between
    # our guard and our checkpoint, accepting into a spec that just got locked. Taking
    # it here freezes the lock state for the rest of our transaction. checkpoint_artifact
    # re-acquires the same xact lock later — Postgres advisory xact locks are re-entrant
    # within a transaction, so that is an instant no-op (no deadlock, no second timeout).
    # Raised BEFORE any doc mutation, so a CommitMutexTimeout needs no room eviction
    # (it is in _CLEAN_ACCEPT_ERRORS).
    acquire_commit_mutex(session, workspace_id)

    # Spec-lock guard (the lock OPERATION is 2b-2; here we only honor an existing lock).
    if artifact == "spec":
        ws = session.get(Workspace, workspace_id)
        session.refresh(ws, ["spec_locked_at"])
        if ws.spec_locked_at is not None:
            raise SpecLockedError(str(workspace_id))

    new_text = pc.payload["new_text"]

    # Structure guard (mirror save.py): non-preamble edits start with an H2 heading;
    # the preamble (index 0) must not.
    first_line = new_text.split("\n", 1)[0] if new_text else ""
    if pc.section_index > 0:
        if not is_h2_heading_line(first_line):
            raise SectionStructureError(
                f"section {pc.section_index} edit must start with an H2 heading (## …)"
            )
    elif new_text and is_h2_heading_line(first_line):
        raise SectionStructureError("preamble (section 0) edit must not start with an H2 heading")

    # Newline glue for a non-final section (mirror save.py).
    if pc.section_index < len(sections) - 1 and not new_text.endswith("\n"):
        new_text += "\n"

    # No-op: proposing exactly the live text — accept at the current checkpoint,
    # no new commit (mirrors save_section's changed=False short-circuit).
    if new_text == live.text:
        repo = workspace_repo_dir(workspaces_dir, workspace_id)
        pc.status = "accepted"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        pc.resulting_commit_sha = _head_or_empty(repo)
        session.flush()
        return KernelAcceptResult(pc=pc, update=None, committed=False, evict=False)

    # Apply: reassemble the whole doc with the section replaced, mutate the live
    # room fragment (capturing the incremental update), stage it, checkpoint.
    new_doc_md = "".join(new_text if s.index == pc.section_index else s.text for s in sections)
    update = apply_pm_json_to_fragment(room_doc, markdown_to_pm(new_doc_md))
    # From here on room_doc is mutated: any failure must evict the room.
    store.append_update(
        session, workspace_id=workspace_id, artifact=artifact,
        actor_id=approver.id, update=update, commit=False,
    )
    result = checkpoint_artifact(
        session, workspace_id=workspace_id, artifact=artifact, workspaces_dir=workspaces_dir
    )
    pc.status = "accepted"
    pc.resolved_by = approver.id
    pc.resolved_at = datetime.now(UTC)
    pc.resulting_commit_sha = result.commit_sha
    session.flush()
    return KernelAcceptResult(pc=pc, update=update, committed=result.committed, evict=True)
