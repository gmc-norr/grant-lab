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

from sqlalchemy import delete, select
from sqlalchemy import update as sql_update
from sqlalchemy.orm import Session

from call_manager.db.models import Branch, TurnJob, Workspace
from call_manager.proposal_assist.citations.gitio import (
    branch_exists,
    checkout_branch,
    commit_exists,
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
from call_manager.proposal_assist.ports.shas import is_sha
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
    """Refused because the branch is load-bearing: it is the one currently
    checked out, or it is the workspace's ROOT.

    Two distinct reasons since the root guard landed, so every raise site passes
    a message saying WHICH — `_raise_http` puts `str(exc)` straight into the 409
    body, and "main" alone tells the user nothing about why they cannot delete
    it."""


class BranchRepoNotReady(BranchError):
    """The workspace exists but Phase 0 has not created its repo yet.

    Distinct from a repo git cannot READ, which stays a 500: that is a server
    fault and saying "still setting up" would hide corruption.
    """


class BranchStartUnknown(BranchError):
    """The requested start commit is not a commit in this repository.

    404, not 422: the request was well-formed, the thing it names does not
    exist -- the same distinction BranchNotFound draws for a branch name.
    """


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
    if not (repo / ".git").exists():
        # A workspace still in setup. Without this the cockpit's read path gets a
        # bare FileNotFoundError from `subprocess.run(cwd=...)` (measured), which
        # `_STATUS` cannot map, so main.py's generic handler answers 500 — for a
        # workspace that is simply young. Same probe the reconciler uses.
        #
        # Deliberately NOT a catch-all around `list_branches`: a repo that EXISTS
        # and git cannot read is corruption, and reporting that as "still setting
        # up" would hide it. That stays a 500.
        raise BranchRepoNotReady(str(workspace_id))
    return list_branches(repo), row.current_branch


def create_workspace_branch(
    session: Session,
    *,
    stamp: FenceStamp,
    workspace_id: uuid.UUID,
    name: str,
    workspaces_dir: Path | str,
    from_sha: str | None = None,
) -> None:
    """Create *name* at *from_sha*, or at the current HEAD when it is None.
    Spec §3.1. Does NOT commit.

    Changes nothing anybody can see: HEAD does not move, the log is untouched,
    nobody is evicted. It is therefore the only branch operation that is safe
    while a drafting turn is running.

    It takes the mutex for two reasons. The row work — clearing a root pointer,
    deleting a stale row — must be serialised against the other branch
    operations. And when `from_sha` is None the start point is HEAD, which must
    not move underneath the read: a checkpoint or section save committing
    between the read and the `git branch` would silently found the branch on a
    commit the user did not choose. With an explicit `from_sha` that second race
    does not apply — the caller pinned the commit.
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
    # De-orphan BEFORE the git call, not after. `branch_exists` has just answered
    # NO for this name, so any row carrying it is by definition an orphan — a
    # delete whose commit failed after `git branch -D`, or a restore whose
    # database snapshot is newer than the repo volume. Without this,
    # `uq_branches_ws_name` fires on the caller's commit, `_STATUS` has no mapping
    # for IntegrityError, and a create that USED to return 201 returns a 500.
    #
    # And if the orphan is the row `root_branch_id` names, the delete itself
    # raises: `fk_workspaces_root_branch_id` is ON DELETE RESTRICT. Clear the
    # pointer first. Clearing rather than re-pointing at the replacement is
    # deliberate — `git branch <name>` does not move HEAD, so the new ref is not
    # known to be the root; `backfill_branch_entities` re-roots it when HEAD
    # agrees. Reproduced 2026-08-31: ForeignKeyViolation on
    # fk_workspaces_root_branch_id, with the ref ALREADY created.
    #
    # Both run before `git_create_branch` so that a failure here leaves git
    # untouched — a database error is recoverable, a stray ref is not. The row is
    # still ADDED after the git call, so a ref git REFUSED leaves no row behind.
    ws = session.get(Workspace, workspace_id)
    if ws is not None and ws.root_branch_id is not None:
        held = session.get(Branch, ws.root_branch_id)
        if held is not None and held.name == name:
            session.execute(
                sql_update(Workspace)
                .where(Workspace.id == workspace_id)
                .values(root_branch_id=None)
            )
    session.execute(delete(Branch).where(Branch.workspace_id == workspace_id, Branch.name == name))
    session.flush()

    start = "HEAD"
    if from_sha is not None:
        # is_sha FIRST: git parses `--output=...` as an option before it reaches
        # any `--` separator, so an unvalidated value in argv is an
        # option-injection vector (ports/shas.py). commit_exists SECOND: a
        # well-formed sha naming a TREE, or naming nothing at all, would
        # otherwise fail inside git with a message no status code maps onto.
        if not is_sha(from_sha) or not commit_exists(repo, from_sha):
            raise BranchStartUnknown(f"{from_sha!r} is not a commit in this workspace")
        start = from_sha

    git_create_branch(repo, name, start)
    # The row is founded in the same call as the ref (spec §8 S1 step 4). A
    # one-time backfill goes stale the moment the next branch is created, and a
    # ref with no row is invisible to the branch-aware resolver S2 installs.
    #
    # These two are NOT atomic and no ordering makes them so: `git branch` is not
    # transactional, so a commit that fails after it leaves a ref with no row.
    # Placing the add after the git call at least means a ref git REFUSED leaves
    # no row (git_create_branch raises before this line). The residual window is
    # repaired by `backfill_branch_entities`, which is a two-way reconciler and is
    # safe to re-run; S2 runs it again before it attaches anything.
    #
    # `last_sdk_session_id` is deliberately left NULL: a new branch has held no
    # hosted conversation, and inheriting the parent's handle would let two
    # branches resume one remote session (§3.1).
    session.add(Branch(workspace_id=workspace_id, name=name))
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

    # `populate_existing=True` FORCES SQL. Without it this returns the instance
    # `require_owner` (web/_perms.py:18-24) already loaded into the identity map
    # before the route called us, so the guard below judges a snapshot taken
    # BEFORE the mutex was acquired. `acquire_commit_mutex_at_generation` is
    # `pg_advisory_xact_lock` — it takes no row lock and never refreshes the ORM —
    # so any writer committing in that window is invisible, and the wait itself
    # can be up to 10 seconds (Phase 0's `_m1` holds the mutex across `_init_repo`
    # and `backfill_branch_entities`, and founds this very pointer).
    #
    # Reproduced 2026-08-31: pointer vacant at load, founded by a second session
    # during the wait, guard skipped, `git branch -D main` destroyed the root ref,
    # and only THEN did RESTRICT raise — a rollback git cannot undo. The one-line
    # control was `session.expunge_all()` before the guard, which turned the same
    # interleaving into the correct BranchInUse refusal with the ref intact.
    row = session.get(Workspace, workspace_id, populate_existing=True)
    if row is not None and row.root_branch_id is not None:
        # WORKTREES §7.3 (2026-08-30-per-branch-worktrees-and-rooms-design.md):
        # the branch the root pointer names cannot be deleted — its worktree
        # IS `repo/`. The database enforces this too (fk_workspaces_root_branch_id
        # is ON DELETE RESTRICT), but an IntegrityError is not an error a user can
        # read. Raise the readable one first and leave RESTRICT as the backstop.
        root = session.get(Branch, row.root_branch_id)
        if root is not None and root.name == name:
            raise BranchInUse(f"{name}: this is the workspace's root branch")
    if row is not None and row.current_branch == name:
        # The DATABASE is the source of truth (§4.1), so refuse against the
        # column, not against HEAD. git refuses too — "cannot delete branch
        # 'main' used by worktree" — but that message cannot be shown to a user
        # and would fire only if the two agreed.
        raise BranchInUse(f"{name}: this is the branch the workspace is on")
    if len(list_branches(repo)) <= 1:
        # A repo with no branch has a detached HEAD, and §4.1's reconciliation
        # then has nothing to reconcile to.
        raise BranchLast(name)

    # THE ROW GOES FIRST, and that order is the whole point of calling RESTRICT a
    # "backstop" above. `git branch -D` is irreversible; a database error is not.
    # With the git call first, a root pointer this transaction did not see meant
    # the ref was already destroyed by the time RESTRICT fired — the backstop
    # fired after the thing it was supposed to protect was gone. Deleting the row
    # first makes the FK raise while git is still untouched, so the guard above
    # and the constraint below now protect the same thing in the same order.
    #
    # A ref created before this stage — or with `git branch` outside the app — has
    # no row; deleting nothing is correct, not an error, and raising here would
    # strand that ref forever.
    #
    # Still not atomic in the other direction: a commit that fails after
    # `git_delete_branch` leaves a row-less ref. `backfill_branch_entities`
    # founds such refs — that is why it is a reconciler and not a one-shot
    # backfill.
    session.execute(delete(Branch).where(Branch.workspace_id == workspace_id, Branch.name == name))
    session.flush()  # make the FK adjudicate NOW, while git is still untouched

    git_delete_branch(repo, name)
    logger.info(
        "branches.deleted",
        extra={"event": "branch_deleted", "workspace_id": str(workspace_id), "branch": name},
    )
