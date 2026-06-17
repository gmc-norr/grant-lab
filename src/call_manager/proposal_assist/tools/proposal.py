"""Artifact tools: read_proposal, propose_edit.

read_proposal returns the current proposal.md content and snapshots the
agent's view (HEAD + per-section shas) for anti-clobber checks.

propose_edit records a PENDING proposed_change row (no git commit) for a
human to review and accept. The mutating edit_proposal / replace_proposal
impls were removed in SP2a-2 Task 7 — the agent is now structurally unable
to commit to the repo directly.
"""

from __future__ import annotations

import logging
from typing import Any

from call_manager.proposal_assist.citations.gitio import GitCommitError, current_head
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.tools.registry import ToolContext

logger = logging.getLogger(__name__)

PROPOSAL_FILENAME = "proposal.md"

# Spec 2026-06-10 §1 — shared error string for the pre-read guard.
_NOT_READ_ERROR = "you haven't read the proposal yet — call read_proposal first"


async def read_proposal_impl(ctx: ToolContext) -> dict[str, Any]:
    """Return the current proposal.md content.

    Records the agent's view of HEAD (spec 2026-06-10 §1) so a later
    edit/replace can detect a concurrent user save that moved HEAD between
    the read and the write. A failure to resolve HEAD clears the view (None)
    rather than failing the read — the next mutation will then refuse with
    the "haven't read" error, which is the safe default.
    """
    path = ctx.repo_dir / PROPOSAL_FILENAME
    if not path.exists():
        return {"error": f"{PROPOSAL_FILENAME} does not exist in this workspace"}
    # ORDERING INVARIANT: resolve HEAD *before* reading the file content.
    # If these two operations were swapped a user commit that lands between
    # read_text and current_head would leave agent_view_head=H2 with
    # content=H1.  A subsequent edit/replace would then pass its stale-HEAD
    # check (view==H2==HEAD) and silently overwrite the user's edit.
    # With HEAD resolved first, that same race yields view=H1/content=H2 —
    # the next mutation sees HEAD!=view and correctly fails stale.  Fail-safe.
    try:
        ctx.agent_view_head = current_head(ctx.repo_dir)
    except GitCommitError:
        ctx.agent_view_head = None
    content = path.read_text()
    # SP2a-2 §3: record the per-section shas the agent saw, so a later
    # propose_edit takes its base_section_sha from this READ-time snapshot
    # (anti-clobber) rather than a live recompute. Built from the same `content`
    # we return, so the dict and the returned text agree exactly.
    ctx.agent_view_sections = {s.index: s.sha for s in split_sections(content)}
    # SP2a-2 §3: also retain the full read-time proposal text, so propose_citation
    # (Task 5) maps an anchor offset -> section_index against the SAME snapshot the
    # section shas came from (consistent (section_index, base_section_sha) pair even
    # if a human added/removed a `##` section between read and propose).
    ctx.agent_view_proposal = content
    return {"content": content}


async def propose_edit_impl(
    ctx: ToolContext,
    *,
    section_index: int,
    new_text: str,
) -> dict[str, Any]:
    """Propose a full-text replacement of one section as a pending
    proposed_change (NOT a git commit). The base is the agent's READ-TIME
    snapshot (ctx.agent_view_head + ctx.agent_view_sections[section_index]) — we
    MUST NOT recompute it live (capture_base) here, or a human edit landed since
    the read would silently become the base and accept would clobber it.

    Returns {"proposed_change_id", "status": "pending"} or {"error": ...}.
    """
    # Lazy import avoids the proposing<->proposal module cycle.
    from call_manager.proposal_assist.proposals.proposing import propose_prose_edit

    base_commit_sha = ctx.agent_view_head
    if base_commit_sha is None:
        return {"error": _NOT_READ_ERROR}
    base_section_sha = ctx.agent_view_sections.get(section_index)
    if base_section_sha is None:
        return {"error": _NOT_READ_ERROR}

    with ctx.session_factory() as session:
        pc = propose_prose_edit(
            session=session,
            workspace_id=ctx.workspace_id,
            section_index=section_index,
            new_text=new_text,
            base_commit_sha=base_commit_sha,
            base_section_sha=base_section_sha,
            origin_kind="interactive_turn",
            origin_session_id=ctx.origin_session_id,
            triggering_turn_id=ctx.triggering_turn_id,
            triggering_prompt=ctx.triggering_prompt,
            created_by=ctx.created_by,
        )
        pc_id = str(pc.id)
        session.commit()
    return {"proposed_change_id": pc_id, "status": "pending"}
