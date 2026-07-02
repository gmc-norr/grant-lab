"""Artifact tools: read_proposal/read_spec, propose_edit/propose_spec_edit.

read_proposal/read_spec return the current artifact content and snapshot the
agent's view (HEAD + per-section shas) for anti-clobber checks. propose_edit /
propose_spec_edit record a PENDING proposed_change row (no git commit) for a
human to review and accept. The mutating edit_proposal / replace_proposal impls
were removed in SP2a-2 Task 7 — the agent is now structurally unable to commit
to the repo directly.

SP2b-2b generalized the read + propose paths into artifact-agnostic cores
(`_read_artifact_impl` / `_propose_artifact_edit_impl`) so the draft and the
spec share identical anti-clobber semantics; the public impls below are thin
wrappers that bind the per-artifact filename / ctx view fields / error string.
"""

from __future__ import annotations

import logging
from typing import Any

from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME, SPEC_FILENAME
from call_manager.proposal_assist.citations.gitio import GitCommitError, current_head
from call_manager.proposal_assist.collab import canonical
from call_manager.proposal_assist.collab.state import is_collab_enabled
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.tools.registry import ToolContext

logger = logging.getLogger(__name__)

# Spec 2026-06-10 §1 — shared error string for the pre-read guard. Per-artifact
# variants so the message names the right read tool (draft test asserts the
# "read_proposal" substring; the spec test asserts "read_spec").
_NOT_READ_DRAFT = "you haven't read the proposal yet — call read_proposal first"
_NOT_READ_SPEC = "you haven't read the spec yet — call read_spec first"
# Back-compat alias: kept in case anything still imports the pre-SP2b-2b name.
_NOT_READ_ERROR = _NOT_READ_DRAFT


async def _read_artifact_impl(
    ctx: ToolContext,
    *,
    filename: str,
    head_attr: str,
    sections_attr: str,
    proposal_attr: str,
) -> dict[str, Any]:
    """Read an artifact and snapshot the agent's view into the given ctx attrs.

    Records the agent's view of HEAD (spec 2026-06-10 §1) so a later propose
    can detect a concurrent user save that moved HEAD between the read and the
    write. A failure to resolve HEAD clears the view (None) rather than failing
    the read — the next mutation will then refuse with the "haven't read" error,
    which is the safe default.

    ORDERING INVARIANT: resolve HEAD *before* reading the file content. If these
    two operations were swapped a user commit that lands between read_text and
    current_head would leave the head view=H2 with content=H1. A subsequent
    propose would then pass its stale-HEAD check (view==H2==HEAD) and silently
    overwrite the user's edit. With HEAD resolved first, that same race yields
    view=H1/content=H2 — the next mutation sees HEAD!=view and correctly fails
    stale. Fail-safe.
    """
    artifact = "spec" if filename == SPEC_FILENAME else "draft"
    path = ctx.repo_dir / filename
    with ctx.session_factory() as _s:
        _collab = is_collab_enabled(_s, workspace_id=ctx.workspace_id, artifact=artifact)
        # Flag-off: disk is authoritative — keep the existence guard. Collab-on:
        # the durable log is canonical, so don't gate on the on-disk file.
        if not _collab and not path.exists():
            return {"error": f"{filename} does not exist in this workspace"}
        # HEAD snapshot stays (concurrent-save detection on the flag-off path; moot
        # on the collab path, which keys staleness on section SHA). Resolve HEAD
        # BEFORE reading content (fail-safe ordering invariant).
        try:
            setattr(ctx, head_attr, current_head(ctx.repo_dir))
        except GitCommitError:
            setattr(ctx, head_attr, None)
        if _collab:
            # Room-canonical (normalized) content so the captured base section shas
            # match the kernel's accept-time live.sha (Slice 3a C1).
            content = canonical.canonical_artifact_markdown(
                _s, workspace_id=ctx.workspace_id, artifact=artifact, repo_dir=ctx.repo_dir
            )
        else:
            content = path.read_text()   # raw HEAD, byte-for-byte (parity)
    # SP2a-2 §3: record the per-section shas the agent saw, so a later propose
    # takes its base_section_sha from this READ-time snapshot (anti-clobber)
    # rather than a live recompute. Built from the same `content` we return, so
    # the dict and the returned text agree exactly. Section.index is its
    # position (0 = preamble), so keying by s.index == keying by position.
    setattr(ctx, sections_attr, {s.index: s.sha for s in split_sections(content)})
    # SP2a-2 §3: also retain the full read-time text, so an anchor-offset ->
    # section_index map (propose_citation) is taken against the SAME snapshot the
    # section shas came from (consistent (section_index, base_section_sha) pair
    # even if a human added/removed a `##` section between read and propose).
    setattr(ctx, proposal_attr, content)
    return {"content": content}


async def read_proposal_impl(ctx: ToolContext) -> dict[str, Any]:
    """Return the current proposal.md content (snapshots the agent's draft view)."""
    return await _read_artifact_impl(
        ctx,
        filename=PROPOSAL_FILENAME,
        head_attr="agent_view_head",
        sections_attr="agent_view_sections",
        proposal_attr="agent_view_proposal",
    )


async def read_spec_impl(ctx: ToolContext) -> dict[str, Any]:
    """Return the current proposal-spec.md content (snapshots the agent's spec view)."""
    return await _read_artifact_impl(
        ctx,
        filename=SPEC_FILENAME,
        head_attr="agent_view_spec_head",
        sections_attr="agent_view_spec_sections",
        proposal_attr="agent_view_spec_proposal",
    )


async def _propose_artifact_edit_impl(
    ctx: ToolContext,
    *,
    target_artifact: str,
    head: str | None,
    sections: dict[int, str],
    section_index: int,
    new_text: str,
    not_read_err: str,
) -> dict[str, Any]:
    """Propose a full-text replacement of one section as a pending
    proposed_change (NOT a git commit). The base is the agent's READ-TIME
    snapshot (`head` + `sections[section_index]`) passed in by the caller — we
    MUST NOT recompute it live (capture_base) here, or a human edit landed since
    the read would silently become the base and accept would clobber it.

    Returns {"proposed_change_id", "status": "pending"} or {"error": ...}.
    """
    # Lazy import avoids the proposing<->proposal module cycle.
    from call_manager.proposal_assist.proposals.proposing import propose_prose_edit

    if head is None:
        return {"error": not_read_err}
    base_section_sha = sections.get(section_index)
    if base_section_sha is None:
        return {"error": not_read_err}

    with ctx.session_factory() as session:
        pc = propose_prose_edit(
            session=session,
            workspace_id=ctx.workspace_id,
            section_index=section_index,
            new_text=new_text,
            base_commit_sha=head,
            base_section_sha=base_section_sha,
            target_artifact=target_artifact,
            origin_kind=ctx.origin_kind,
            origin_job_id=ctx.origin_job_id,
            origin_session_id=ctx.origin_session_id,
            triggering_turn_id=ctx.triggering_turn_id,
            triggering_prompt=ctx.triggering_prompt,
            created_by=ctx.created_by,
        )
        if target_artifact == "draft" and ctx.turn_selection:
            sel = ctx.turn_selection
            pc.payload = {
                **pc.payload,
                "selection_anchor": sel.get("anchor"),
                "selection_from": sel.get("from_offset"),
                "selection_to": sel.get("to_offset"),
            }
        pc_id = str(pc.id)
        session.commit()
    return {"proposed_change_id": pc_id, "status": "pending"}


async def propose_edit_impl(
    ctx: ToolContext,
    *,
    section_index: int,
    new_text: str,
) -> dict[str, Any]:
    """Propose a pending prose_edit on proposal.md from the agent's draft view."""
    return await _propose_artifact_edit_impl(
        ctx,
        target_artifact="draft",
        head=ctx.agent_view_head,
        sections=ctx.agent_view_sections,
        section_index=section_index,
        new_text=new_text,
        not_read_err=_NOT_READ_DRAFT,
    )


async def propose_spec_edit_impl(
    ctx: ToolContext,
    *,
    section_index: int,
    new_text: str,
) -> dict[str, Any]:
    """Propose a pending prose_edit on proposal-spec.md from the agent's spec view."""
    return await _propose_artifact_edit_impl(
        ctx,
        target_artifact="spec",
        head=ctx.agent_view_spec_head,
        sections=ctx.agent_view_spec_sections,
        section_index=section_index,
        new_text=new_text,
        not_read_err=_NOT_READ_SPEC,
    )
