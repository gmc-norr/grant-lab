"""Write pending proposed_change rows. Cheap (a DB insert) — no git write,
no commit mutex. The caller owns the transaction.

CRITICAL (anti-clobber): the base (commit + section sha) MUST be the snapshot
the proposer actually *read*, NOT recomputed live at propose time. With the
turn-freeze gone a human can edit the section between the proposer's read and
its propose call; recomputing live would capture that human edit as the base,
so accept would replay over it silently. Callers snapshot the base at READ time
via `capture_base` (the agent uses its `agent_view_head` + the section sha it
saw). A base that has since moved makes accept go `stale` instead of clobbering.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from call_manager.db.models import ProposedChange
from call_manager.proposal_assist.citations.gitio import current_head
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME


def capture_base(repo_dir: Path, section_index: int) -> tuple[str, str]:
    """Snapshot (head_commit_sha, section_sha) of the draft at READ time.
    Call this when the proposer *reads* the section, not when it proposes.

    Uses the house ``current_head`` helper (GIT_CEILING_DIRECTORIES-guarded) so a
    non-repo ``repo_dir`` can't silently capture an enclosing repo's HEAD as the base.
    """
    head = current_head(repo_dir)
    proposal = (repo_dir / PROPOSAL_FILENAME).read_text()
    return head, split_sections(proposal)[section_index].sha


def propose_prose_edit(
    *,
    session: Session,
    workspace_id: uuid.UUID,
    section_index: int,
    new_text: str,
    base_commit_sha: str,
    base_section_sha: str,
    origin_kind: str,
    origin_session_id: str | None = None,
    origin_job_id: uuid.UUID | None = None,
    triggering_turn_id: uuid.UUID | None = None,
    triggering_prompt: str | None = None,
    created_by: int | None = None,
) -> ProposedChange:
    pc = ProposedChange(
        workspace_id=workspace_id,
        target_artifact="draft",
        section_index=section_index,
        base_commit_sha=base_commit_sha,
        base_section_sha=base_section_sha,
        kind="prose_edit",
        payload={"new_text": new_text},
        origin_kind=origin_kind,
        origin_session_id=origin_session_id,
        origin_job_id=origin_job_id,
        triggering_turn_id=triggering_turn_id,
        triggering_prompt=triggering_prompt,
        created_by=created_by,
    )
    session.add(pc)
    session.flush()
    return pc
