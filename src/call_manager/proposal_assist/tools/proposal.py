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
src/call_manager/proposal_assist/phases/setup.py:200).

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

from call_manager.db.models import ArtifactVersion
from call_manager.proposal_assist.citations.gitio import GitCommitError, current_head
from call_manager.proposal_assist.drafting.locking import (
    BUSY_ERROR_MESSAGE,
    CommitMutexTimeout,
    acquire_commit_mutex,
)
from call_manager.proposal_assist.tools.registry import ToolContext

logger = logging.getLogger(__name__)

PROPOSAL_FILENAME = "proposal.md"

# Spec 2026-06-10 §1 — shared error strings so edit/replace and the cheap
# pre-check / authoritative mutex check return byte-identical messages.
_NOT_READ_ERROR = "you haven't read the proposal yet — call read_proposal first"
_STALE_ERROR = "proposal changed since you read it — call read_proposal and retry"
_BUSY_ERROR = BUSY_ERROR_MESSAGE  # canonical string lives in locking.py


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
    return {"content": content}


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

    Spec 2026-06-10 §1: the whole body runs under ctx.write_lock and carries
    an IMMUTABLE expected-HEAD (captured before reading proposal.md) that is
    verified inside the commit mutex, so a concurrent user save that moved
    HEAD becomes a tool error instead of a silent lost update.
    """
    async with ctx.write_lock:
        expected_head = ctx.agent_view_head
        if expected_head is None:
            return {"error": _NOT_READ_ERROR}

        # Cheap pre-check before doing any work — the authoritative check
        # happens again inside the commit mutex in _write_and_commit.
        try:
            if current_head(ctx.repo_dir) != expected_head:
                return {"error": _STALE_ERROR}
        except GitCommitError:
            return {"error": _STALE_ERROR}

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

        return _write_and_commit(
            ctx,
            content=content,
            commit_message=_commit_message(triggering_turn_id, edits=edits),
            triggering_turn_id=triggering_turn_id,
            expected_head=expected_head,
        )


async def replace_proposal_impl(
    ctx: ToolContext,
    *,
    content: str,
    triggering_turn_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Full overwrite of proposal.md. For large structural rewrites
    where edit_proposal would be unwieldy.

    Spec 2026-06-10 §1: runs under ctx.write_lock with an immutable
    expected-HEAD verified inside the commit mutex (see edit_proposal_impl).
    """
    async with ctx.write_lock:
        expected_head = ctx.agent_view_head
        if expected_head is None:
            return {"error": _NOT_READ_ERROR}

        try:
            if current_head(ctx.repo_dir) != expected_head:
                return {"error": _STALE_ERROR}
        except GitCommitError:
            return {"error": _STALE_ERROR}

        return _write_and_commit(
            ctx,
            content=content,
            commit_message=_commit_message(triggering_turn_id, edits=None),
            triggering_turn_id=triggering_turn_id,
            expected_head=expected_head,
        )


def _commit_message(triggering_turn_id: uuid.UUID | None, *, edits: list | None) -> str:
    kind = "edit_proposal" if edits else "replace_proposal"
    if triggering_turn_id is not None:
        return f"agent: {kind} [turn={triggering_turn_id}]"
    return f"agent: {kind}"


def _write_and_commit(
    ctx: ToolContext,
    *,
    content: str,
    commit_message: str,
    triggering_turn_id: uuid.UUID | None,
    expected_head: str,
) -> dict[str, Any]:
    """Write file → git commit → artifact_versions insert. Returns sha
    on success.

    Spec 2026-06-10 §1: the whole git-write + DB-row flow runs while the
    per-workspace COMMIT MUTEX is held on a single session. The mutex is a
    transaction-scoped Postgres advisory lock acquired here and released by
    that session's COMMIT/ROLLBACK — so it is held atomically across the git
    commit AND the artifact_versions insert. Inside the mutex we re-check
    HEAD against the immutable *expected_head* the caller captured before
    computing content: if HEAD moved (a user save committed concurrently),
    we refuse without writing. This closes the lost-update window that a
    bare compare-the-shared-view check would leave open (two tool calls both
    computed from the old HEAD; one commits + advances the shared view; the
    other's check would then spuriously pass).

    Spec line 253: "If step 1 fails: no state change anywhere." We therefore
    capture the original working-tree contents BEFORE writing and restore
    them on commit failure. We also unstage proposal.md (scoped — never
    `git reset --hard HEAD`) in case `git add` succeeded but `git commit` did
    not. This prevents wiping unrelated tracked files' dirty edits.

    Post-commit DB-failure (drift) case is preserved: if the git commit lands
    but the artifact_versions insert fails, we log and still return the sha —
    reconciliation backfills the orphan commit on next workspace open.

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

    with ctx.session_factory() as session:
        # Serialize against every other git mutation of this workspace. The
        # lock is transaction-scoped: released by this session's COMMIT or
        # ROLLBACK (whether explicit below or on context exit).
        try:
            acquire_commit_mutex(session, ctx.workspace_id)
        except CommitMutexTimeout:
            return {"error": _BUSY_ERROR}

        # AUTHORITATIVE expected-HEAD check, inside the mutex. A concurrent
        # user save that moved HEAD makes this refuse before any write.
        try:
            if current_head(ctx.repo_dir) != expected_head:
                return {"error": _STALE_ERROR}
        except GitCommitError:
            return {"error": _STALE_ERROR}

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
            # session ROLLBACK on context exit releases the mutex; no row added.
            return {"error": f"git commit failed: {stderr.strip()}"}

        # DB write: artifact_versions row on the SAME mutex session, so COMMIT
        # releases the lock atomically with the row landing.
        try:
            session.add(
                ArtifactVersion(
                    workspace_id=ctx.workspace_id,
                    commit_sha=sha,
                    triggering_turn_id=triggering_turn_id,
                    created_by="agent",
                    message=commit_message,
                )
            )
            session.commit()
        except Exception:
            # Spec-mandated drift case: git has commit, DB doesn't.
            # Reconciliation backfills on next workspace open. Roll back so the
            # mutex releases promptly; the git commit still stands.
            session.rollback()
            logger.exception(
                "edit/replace_proposal: artifact_versions insert failed for "
                "workspace %s sha %s — reconciliation will backfill",
                ctx.workspace_id,
                sha,
            )

    # Success: advance the agent's view so a following edit/replace in the
    # same turn doesn't spuriously fail the precondition.
    ctx.agent_view_head = sha
    return {"commit_sha": sha}
