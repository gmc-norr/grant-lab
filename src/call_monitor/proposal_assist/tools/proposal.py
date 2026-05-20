"""Artifact-editing tools: read_proposal, edit_proposal, replace_proposal.

Each editing tool atomically:
  1. Validates inputs (no-op edits, non-unique anchors, missing anchors → reject).
  2. Writes the new content to repo_dir/proposal.md.
  3. Commits via `git commit` (subprocess).
  4. Inserts an artifact_versions row in the DB with the new sha.

Write order is git-first, DB-second per spec line 248. If the DB write
fails after the commit, the next workspace open's reconciliation pass
backfills the orphan commit (see reconcile.py).

ROLLBACK (Codex M4): on commit failure, restore the file from captured
content AND `git reset HEAD -- proposal.md` (unstage scoped to that
path). NEVER `git reset --hard HEAD` — that would wipe unrelated
tracked-file dirty edits.

GIT ENV (Codex C2): GIT_AUTHOR_* / GIT_COMMITTER_* set so production
without global gitconfig works (matches Phase 0's pattern at
src/call_monitor/proposal_assist/phases/setup.py:200).

The handlers are async (the SDK calls them via the MCP protocol). They
return a JSON-able dict (the registry's @tool wrappers will translate
to the MCP envelope). Errors are returned as {"error": "..."} dicts
rather than raising, which gives the model a chance to react.
"""

from __future__ import annotations

import logging
import os
import subprocess
import uuid
from typing import Any

from call_monitor.db.models import ArtifactVersion
from call_monitor.proposal_assist.tools.registry import ToolContext

logger = logging.getLogger(__name__)

PROPOSAL_FILENAME = "proposal.md"


async def read_proposal_impl(ctx: ToolContext) -> dict[str, Any]:
    """Return the current proposal.md content."""
    path = ctx.repo_dir / PROPOSAL_FILENAME
    if not path.exists():
        return {"error": f"{PROPOSAL_FILENAME} does not exist in this workspace"}
    return {"content": path.read_text()}


async def edit_proposal_impl(
    ctx: ToolContext,
    *,
    edits: list[dict[str, str]],
    triggering_turn_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Apply surgical {old_string → new_string} edits. All-or-nothing:
    if any anchor is missing or ambiguous, the batch is rejected and
    nothing is written or committed.

    Returns {"commit_sha": "..."} on success; {"error": "..."} on
    validation failure.
    """
    if not edits:
        return {"error": "edits list cannot be empty"}

    path = ctx.repo_dir / PROPOSAL_FILENAME
    if not path.exists():
        return {"error": f"{PROPOSAL_FILENAME} does not exist in this workspace"}

    content = path.read_text()
    for idx, edit in enumerate(edits):
        old = edit.get("old_string", "")
        new = edit.get("new_string", "")
        if not old:
            return {"error": f"edit #{idx}: old_string is required"}
        count = content.count(old)
        if count == 0:
            return {"error": f"edit #{idx}: old_string not found in proposal"}
        if count > 1:
            return {
                "error": (
                    f"edit #{idx}: old_string is ambiguous "
                    f"(matches {count} times); pick a unique anchor"
                )
            }
        content = content.replace(old, new, 1)

    return await _write_and_commit(
        ctx,
        content=content,
        commit_message=_commit_message(triggering_turn_id, edits=edits),
        triggering_turn_id=triggering_turn_id,
    )


async def replace_proposal_impl(
    ctx: ToolContext,
    *,
    content: str,
    triggering_turn_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Full overwrite of proposal.md. For large structural rewrites
    where edit_proposal would be unwieldy."""
    return await _write_and_commit(
        ctx,
        content=content,
        commit_message=_commit_message(triggering_turn_id, edits=None),
        triggering_turn_id=triggering_turn_id,
    )


def _commit_message(triggering_turn_id: uuid.UUID | None, *, edits: list | None) -> str:
    kind = "edit_proposal" if edits else "replace_proposal"
    if triggering_turn_id is not None:
        return f"agent: {kind} [turn={triggering_turn_id}]"
    return f"agent: {kind}"


async def _write_and_commit(
    ctx: ToolContext,
    *,
    content: str,
    commit_message: str,
    triggering_turn_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Write file → git commit → artifact_versions insert. Returns sha
    on success.

    Spec line 253: "If step 1 fails: no state change anywhere." We
    therefore capture the original working-tree contents BEFORE writing
    and restore them on commit failure. We also unstage proposal.md
    (scoped — never `git reset --hard HEAD`) in case `git add` succeeded
    but `git commit` did not. This prevents wiping unrelated tracked
    files' dirty edits.

    GIT_AUTHOR_* / GIT_COMMITTER_* env vars match Phase 0's pattern
    so production without a global gitconfig still works.
    """
    path = ctx.repo_dir / PROPOSAL_FILENAME
    original_existed = path.exists()
    original_content = path.read_text() if original_existed else None

    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "call-manager")
    env.setdefault("GIT_AUTHOR_EMAIL", "call-manager@local")
    env.setdefault("GIT_COMMITTER_NAME", "call-manager")
    env.setdefault("GIT_COMMITTER_EMAIL", "call-manager@local")

    path.write_text(content)
    try:
        subprocess.run(
            ["git", "add", PROPOSAL_FILENAME],
            cwd=ctx.repo_dir,
            check=True,
            capture_output=True,
            env=env,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", commit_message],
            cwd=ctx.repo_dir,
            check=True,
            capture_output=True,
            env=env,
        )
        sha_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ctx.repo_dir,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        sha = sha_proc.stdout.strip()
    except subprocess.CalledProcessError as e:
        # ROLLBACK (rev3 / Codex M4): scoped to proposal.md only.
        if original_content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(original_content)
        subprocess.run(
            ["git", "reset", "HEAD", "--", PROPOSAL_FILENAME],
            cwd=ctx.repo_dir,
            check=False,
            capture_output=True,
            env=env,
        )
        stderr = (
            e.stderr.decode("utf-8", errors="replace")
            if isinstance(e.stderr, bytes)
            else (e.stderr or "")
        )
        return {"error": f"git commit failed: {stderr.strip()}"}

    # DB write: artifact_versions row.
    try:
        with ctx.session_factory() as s:
            s.add(
                ArtifactVersion(
                    workspace_id=ctx.workspace_id,
                    commit_sha=sha,
                    triggering_turn_id=triggering_turn_id,
                    created_by="agent",
                    message=commit_message,
                )
            )
            s.commit()
    except Exception:
        # Spec-mandated drift case: git has commit, DB doesn't.
        # Reconciliation backfills on next workspace open.
        logger.exception(
            "edit/replace_proposal: artifact_versions insert failed for "
            "workspace %s sha %s — reconciliation will backfill",
            ctx.workspace_id,
            sha,
        )

    return {"commit_sha": sha}
