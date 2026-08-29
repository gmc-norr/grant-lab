"""Workspace branch lifecycle: create, switch, delete.

The design is 2026-08-29-workspace-branches-design.md. Two things in here are
load-bearing and non-obvious, both explained at their site: the ORDER of the
switch sequence (§4.2/§4.3) and the fact that a teardown reporting
`locked=False` aborts the switch rather than the teardown (§4.3).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.orm import Session

from call_manager.db.models import TurnJob, Workspace
from call_manager.proposal_assist.citations.gitio import (
    branch_exists,
    checkout_branch,
    current_branch,
)
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.checkpoint import checkpoint_artifact
from call_manager.proposal_assist.collab.room import BranchSwitchTeardown
from call_manager.proposal_assist.fencing import acquire_commit_mutex_at_generation
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.ports.stamp import FenceStamp

logger = logging.getLogger(__name__)

#: Both artifact rooms, in the order the switch handles them. Rooms are keyed
#: (workspace_id, artifact) with NO branch dimension — branches are a workspace
#: property, which is why a switch has to move the whole workspace at once.
_ARTIFACTS = ("draft", "spec")

TeardownFn = Callable[[uuid.UUID], BranchSwitchTeardown]


class BranchError(RuntimeError):
    """Base for every refusal in this module."""


class BranchNotFound(BranchError):
    """The named branch does not exist in the workspace repo."""


class BranchExists(BranchError):
    """A branch of that name is already there. Never silently reuse one."""


class BranchNameInvalid(BranchError):
    """`git check-ref-format --branch` rejected the name."""


class BranchBusy(BranchError):
    """The switch could not be made safely right now: a drafting turn is running,
    or the teardown could not prove no client write was in flight. Retryable."""


class BranchInUse(BranchError):
    """Refused because the branch is the one currently checked out."""


class BranchLast(BranchError):
    """Refused because it is the only branch left."""


def _has_running_turn(session: Session, *, workspace_id: uuid.UUID) -> bool:
    """Whether this workspace has a drafting turn in flight (§6.3).

    Backed by the partial unique index on turn_jobs(workspace_id) WHERE
    status='running' (models.py:887-892), so at most one row can ever match.
    Private: the switch is the only caller, and a public helper would invite use
    as a standalone gate — which it is NOT. It is only a gate while the caller
    holds the commit mutex and `claim_turn_job` holds the shared lease (task 6).
    """
    return (
        session.execute(
            select(TurnJob.id).where(
                TurnJob.workspace_id == workspace_id, TurnJob.status == "running"
            )
        ).first()
        is not None
    )


def _reconcile_head(
    session: Session, *, workspace_id: uuid.UUID, workspaces_dir: Path | str
) -> str:
    """Make git HEAD agree with `workspaces.current_branch`, and return that name.

    §4.1: the DATABASE is the source of truth and HEAD is a cache. Called at the
    top of a switch — before any other git read or write — and again by the route
    as a compensating action when a switch rolls back after step 9 has already
    moved HEAD. Idempotent: reconciling an already-agreeing pair does nothing.
    """
    repo = workspace_repo_dir(workspaces_dir, workspace_id)
    row = session.get(Workspace, workspace_id)
    if row is None:
        raise BranchNotFound(str(workspace_id))
    recorded = row.current_branch
    if current_branch(repo) != recorded:
        logger.warning(
            "branches.head_reconciled",
            extra={
                "event": "branch_head_reconciled",
                "workspace_id": str(workspace_id),
                "to": recorded,
            },
        )
        checkout_branch(repo, recorded)
    return recorded


def switch_branch(
    session: Session,
    *,
    stamp: FenceStamp,
    workspace_id: uuid.UUID,
    target: str,
    workspaces_dir: Path | str,
    teardown: TeardownFn,
) -> None:
    """Move a workspace to *target*. Spec §4.2. Does NOT commit — the caller does.

    *teardown* is a SYNC callable that performs the asyncio room teardown and
    returns its result. The room manager runs on the app's event loop while this
    runs in a request thread holding a database transaction; keeping the bridge
    at the caller means the one cross-thread wait lives where its timeout is
    visible, and lets every step below be tested without a loop.
    """
    repo = workspace_repo_dir(workspaces_dir, workspace_id)

    # ---- 1. Refuse while a drafting turn is running (§6.3) -------------------
    # Re-checked under the mutex below. This first check is the cheap one: it
    # rejects the common case before evicting anybody.
    if _has_running_turn(session, workspace_id=workspace_id):
        raise BranchBusy("a drafting turn is running")

    # ---- 2. Take the commit mutex at the caller's generation -----------------
    # pg_advisory_xact_lock is re-entrant within one transaction, so
    # checkpoint_artifact re-acquiring it below is free rather than a deadlock.
    acquire_commit_mutex_at_generation(
        session, workspace_id, generation=stamp.generation, timeout_seconds=10
    )

    # The authoritative re-check, now that nothing else can commit a claim
    # underneath us (task 6 gave claim_turn_job the shared lease).
    if _has_running_turn(session, workspace_id=workspace_id):
        raise BranchBusy("a drafting turn is running")

    # ---- 3. Reconcile HEAD to the column BEFORE any other git read or write --
    # §4.1 makes the DATABASE the source of truth. A previous switch that crashed
    # between steps 9 and 10 leaves HEAD on the target while the column still
    # names the source; step 5 below reads whatever HEAD is (checkpoint.py:110)
    # and would commit the source's live log onto the target branch. Reconciling
    # first makes that crash recoverable instead of silently wrong.
    recorded = _reconcile_head(session, workspace_id=workspace_id, workspaces_dir=workspaces_dir)

    if target == recorded:
        return  # switching to where you are throws nothing away
    if not branch_exists(repo, target):
        raise BranchNotFound(target)

    # ---- 4. Tear the rooms down FIRST ---------------------------------------
    # Before anything touches the log. A connected client persists inside
    # room.lock WITHOUT the commit mutex (room.py:731-738), so a truncate that
    # ran first would be undone by one keystroke, load_or_hydrate would return
    # those rows, and the workspace would serve the OLD branch's text under the
    # NEW branch's name.
    result = teardown(workspace_id)
    if not result.locked:
        # A persist may still be in flight and would ride into the target branch.
        # The teardown itself COMPLETED (it always does) — only the switch aborts.
        raise BranchBusy("could not quiesce the live document; try again")

    # ---- 5. Checkpoint the now-frozen log to the branch being LEFT -----------
    for artifact in _ARTIFACTS:
        checkpoint_artifact(
            session,
            stamp=stamp,
            workspace_id=workspace_id,
            artifact=artifact,
            workspaces_dir=workspaces_dir,
            record_version=False,
        )

    # ---- 6. Truncate the log and reset the watermark -------------------------
    # Same transaction as the checkpoint above — handover.py:60-62's rule: a
    # truncate is never visible without its checkpoint.
    for artifact in _ARTIFACTS:
        store.truncate_log(session, workspace_id=workspace_id, artifact=artifact)
        state.reset_checkpoint(session, workspace_id=workspace_id, artifact=artifact)

    # ---- 7. Bump the lifecycle generation ------------------------------------
    # Invalidates every in-flight edit form and fenced write stamped before now
    # (§6.1: the stale-write guard is an AND and passes on byte-identical
    # sections, which is the normal case right after branching).
    from call_manager.workspaces import bump_lifecycle_generation

    bump_lifecycle_generation(session, workspace_id=workspace_id)

    # ---- 8. Set the column ---------------------------------------------------
    session.execute(
        sql_update(Workspace).where(Workspace.id == workspace_id).values(current_branch=target)
    )

    # ---- 9. Check out the target ---------------------------------------------
    # NOT transactional. Step 10 is the caller's commit; on rollback the caller
    # compensates by checking `recorded` back out, and §4.1's reconciliation
    # repairs it even after a crash.
    try:
        checkout_branch(repo, target)
    except Exception:
        logger.warning(
            "branches.checkout_failed",
            extra={"event": "branch_checkout_failed", "workspace_id": str(workspace_id)},
            exc_info=True,
        )
        raise
    logger.info(
        "branches.switched",
        extra={
            "event": "branch_switched",
            "workspace_id": str(workspace_id),
            "from": recorded,
            "to": target,
            "closed_sockets": result.closed,
        },
    )
