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
    has_commits,
    is_valid_branch_name,
    list_branches,
)
from call_manager.proposal_assist.citations.gitio import (
    create_branch as git_create_branch,
)
from call_manager.proposal_assist.citations.gitio import (
    delete_branch as git_delete_branch,
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


def list_workspace_branches(
    session: Session, *, workspace_id: uuid.UUID, workspaces_dir: Path | str
) -> tuple[list[str], str]:
    """(every local branch, the one this workspace is on). Read-only, no mutex.

    `current` comes from the DATABASE, not from `current_branch(repo)`. §4.1
    makes the column the source of truth and HEAD a cache, so after a crash
    between steps 9 and 10 the two disagree — and reporting HEAD would show the
    user the branch the app is about to reconcile AWAY from. The ref LIST is a
    git read because git is the only place refs live; that is not a
    source-of-truth question.

    No mutex on purpose: this is on the cockpit's read path and must not queue
    behind a switch. It does not reconcile either — reconciliation is a write
    (`git checkout`) and belongs to the operations that take the mutex.
    """
    repo = workspace_repo_dir(workspaces_dir, workspace_id)
    row = session.get(Workspace, workspace_id)
    if row is None:
        raise BranchNotFound(str(workspace_id))
    return list_branches(repo), row.current_branch


def create_workspace_branch(
    session: Session,
    *,
    stamp: FenceStamp,
    workspace_id: uuid.UUID,
    name: str,
    workspaces_dir: Path | str,
) -> None:
    """Create *name* at the current HEAD. Spec §3.1. Does NOT commit.

    Changes nothing anybody can see: HEAD does not move, the log is untouched,
    nobody is evicted. It is therefore the only branch operation that is safe
    while a drafting turn is running.

    It takes the mutex anyway, for one reason: HEAD must not move under it. A
    checkpoint or section save committing between the read and the `git branch`
    would silently found the branch on a commit the user did not choose.
    """
    if not is_valid_branch_name(name):
        raise BranchNameInvalid(name)
    repo = workspace_repo_dir(workspaces_dir, workspace_id)

    acquire_commit_mutex_at_generation(
        session, workspace_id, generation=stamp.generation, timeout_seconds=10
    )
    # Reconcile FIRST, like the switch does. `git branch <name> HEAD` founds the
    # branch on whatever HEAD is, so after a crashed switch left HEAD on the
    # wrong branch this would silently create the branch from a commit the user
    # did not choose — §4.1's rule is "before any git read or write", and this is
    # a git write.
    _reconcile_head(session, workspace_id=workspace_id, workspaces_dir=workspaces_dir)

    if not has_commits(repo):
        # A workspace still in setup. Detected explicitly rather than by catching
        # whatever `git branch` raises: measured, that call exits 128 here, but a
        # bare `except Exception` would relabel a permissions failure, a missing
        # git binary or a subprocess timeout as "no commits yet" — a wrong,
        # confident error message is worse than the real one.
        raise BranchNotFound(f"{name}: the workspace has no commits yet")
    if branch_exists(repo, name):
        raise BranchExists(name)
    git_create_branch(repo, name)
    logger.info(
        "branches.created",
        extra={"event": "branch_created", "workspace_id": str(workspace_id), "branch": name},
    )


def delete_workspace_branch(
    session: Session,
    *,
    stamp: FenceStamp,
    workspace_id: uuid.UUID,
    name: str,
    workspaces_dir: Path | str,
) -> None:
    """Delete *name*, force. Spec §3.2. Does NOT commit.

    DESTRUCTIVE and not recoverable through the UI: `-D` drops the ref and
    nothing else in the app names those commits (`list_commits` follows HEAD;
    ArtifactVersion rows persist but their shas become unreachable). The caller
    must have warned.

    Takes the mutex for the same reason a switch does: it must not run
    interleaved with a switch onto the branch it is deleting.
    """
    repo = workspace_repo_dir(workspaces_dir, workspace_id)

    acquire_commit_mutex_at_generation(
        session, workspace_id, generation=stamp.generation, timeout_seconds=10
    )
    # Same rule as create: reconcile before any git read or write, so a crashed
    # switch cannot make `git branch -D` operate against the wrong HEAD.
    _reconcile_head(session, workspace_id=workspace_id, workspaces_dir=workspaces_dir)

    if not branch_exists(repo, name):
        raise BranchNotFound(name)

    row = session.get(Workspace, workspace_id)
    if row is not None and row.current_branch == name:
        # The DATABASE is the source of truth (§4.1), so refuse against the
        # column, not against HEAD. git refuses too — "cannot delete branch
        # 'main' used by worktree" — but that message cannot be shown to a user
        # and would fire only if the two agreed.
        raise BranchInUse(name)
    if len(list_branches(repo)) <= 1:
        # A repo with no branch has a detached HEAD, and §4.1's reconciliation
        # then has nothing to reconcile to.
        raise BranchLast(name)

    git_delete_branch(repo, name)
    logger.info(
        "branches.deleted",
        extra={"event": "branch_deleted", "workspace_id": str(workspace_id), "branch": name},
    )
