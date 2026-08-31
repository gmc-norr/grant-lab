"""Reconcile the branch entity against what git actually has.

A separate module, not migration-local, for the same reason `branch_backfill.py`
is: the migration is untestable in place, and this is the half that can be wrong.
The migration imports it.

Written as a RECONCILER rather than a one-shot backfill because rows and refs
cannot be kept atomic — `git branch` is not transactional, so a commit that fails
after the git call leaves a ref with no row or a row with no ref. Running this
repairs either. S2 runs it again before it attaches any worktree.
"""

from __future__ import annotations

import logging
import subprocess
import uuid
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from call_manager.db.models import Branch, Workspace
from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    branch_exists,
    head_branch,
    list_branches,
)
from call_manager.proposal_assist.paths import workspace_repo_dir

logger = logging.getLogger(__name__)


class BackfillResult(NamedTuple):
    """Counts, so the caller can say what happened rather than assume it did."""

    created: int
    deleted: int
    rooted: int
    skipped: int
    #: Workspaces with no repo directory at all. Correct for one still in setup —
    #: but if `WORKSPACES_DIR` is unset, resolved against the wrong cwd, or the
    #: volume is not mounted, EVERY workspace looks like that and the run reports
    #: a clean zero. Counted so "0 of 0" and "0 of 500" cannot print identically.
    absent: int


def _real_refs(repo: Path) -> list[str]:
    """The local branch names, with git's non-ref output filtered out.

    `git branch --format=...` emits a pseudo-entry on a detached HEAD, under every
    format. Measured 2026-08-31 (`list_branches` now asks for `%(refname)` and
    strips, because `:short` is ambiguity-dependent — see gitio.py):

        $ git branch --format='%(refname)'
        (HEAD detached at 7ee4363)
        refs/heads/feature
        refs/heads/main

    The pseudo-entry carries NO `refs/heads/` prefix, so stripping leaves it
    intact and the `branch_exists` filter below still rejects it.

    Founding a row from that first line would create a branch literally named
    `(HEAD detached at 777c52e)`, which every later stage would then try to
    resolve to a worktree. `branch_exists` uses `show-ref --verify
    refs/heads/<name>`, so it answers no for anything that is not a real ref.
    """
    return [name for name in list_branches(repo) if branch_exists(repo, name)]


def backfill_branch_entities(
    session: Session, *, workspaces_dir: Path | str, workspace_id: uuid.UUID | None = None
) -> BackfillResult:
    """Make `branches` agree with each workspace's local refs, and set the root
    pointer. Returns counts. Does NOT commit.

    **Every local ref gets a row, not just the current one.**
    `create_workspace_branch` has shipped since 880ea243 and does nothing but
    `git branch`, so a workspace can already hold several refs. A ref with no row
    is invisible to every later stage.

    **Rows whose ref is gone are deleted.** That is the other half of the repair,
    and it is why a delete whose commit failed after `git branch -D` does not
    leave a permanent orphan. A row is only ever removed for a workspace whose
    refs were read successfully — an unreadable repo skips the whole workspace, so
    a transient git failure can never be mistaken for "every branch was deleted".

    **The root pointer follows git HEAD.** §7.3 founds the root on what `repo/`
    actually is; a pointer founded on `current_branch` could name a tree nobody
    has. A detached HEAD leaves the pointer alone — the rows are still written,
    because the refs exist regardless of what HEAD points at.

    **`last_sdk_session_id` follows `workspaces.current_branch`,** not HEAD
    (spec §8 S1 step 1). It is one hosted conversation and it belongs to the
    branch the *user* was on; §4.1 makes the database the source of truth for
    that. Copying it to every branch would let two branches resume the same remote
    session (§3.1). The workspace column is left untouched —
    `turns/processor.py:389` is still its only reader, and it is dropped in S4.

    Idempotent: a second run over an unchanged repo returns `created=0,
    deleted=0`, and re-sets each root pointer to the value it already had. A
    migration can be re-run against a partially-migrated database, and the unique
    key would otherwise turn that into a hard failure.

    Skipped without raising: no repo directory, or a repo git cannot read. One bad
    workspace out of 500 must not leave the other 499 half-founded — but the count
    is RETURNED so the caller can report it rather than imply success.

    *workspace_id* narrows the pass to ONE workspace. Phase 0 uses it to found a
    freshly-initialised workspace's first branch row and root pointer, in the same
    transaction that creates the repo (spec §8 S1 step 4). Reusing this function
    rather than writing a second founding path is deliberate: ref filtering, the
    detached-HEAD case and the orphan sweep are all already tested here, and a
    parallel implementation would be the one that drifts.
    """
    created = deleted = rooted = skipped = absent = 0
    query = select(Workspace.id, Workspace.last_sdk_session_id)
    if workspace_id is not None:
        query = query.where(Workspace.id == workspace_id)
    rows = session.execute(query).all()
    for wid, sdk_session_id in rows:
        repo = workspace_repo_dir(workspaces_dir, wid)
        if not (repo / ".git").exists():
            # Still in setup: nothing to found a branch on, and not a failure.
            # Counted anyway — see BackfillResult.absent.
            absent += 1
            continue
        # BOTH git reads happen here, before anything is written for this
        # workspace, and a failure in EITHER skips the whole workspace. That is
        # what makes the promise three paragraphs up true: "a row is only ever
        # removed for a workspace whose refs were read successfully".
        #
        # `head_branch`, not `current_branch`: the latter raises the same
        # GitCommitError for a detached HEAD and for a broken repo, so catching it
        # made every operational failure look detached — and a detached-looking
        # workspace has its root pointer CLEARED. A timeout could destroy a
        # healthy pointer and still report success, because nothing counted it.
        try:
            names = _real_refs(repo)
            head = head_branch(repo)  # None ONLY for a genuinely detached HEAD
        except (GitCommitError, OSError, subprocess.TimeoutExpired):
            skipped += 1
            logger.warning(
                "branches.entity_reconcile_skipped",
                extra={"event": "branch_entity_reconcile_skipped", "workspace_id": str(wid)},
                exc_info=True,
            )
            continue
        # NO early return on an empty ref set. `git init` with no commit reaches
        # here with names == [], and so does a repo whose last ref was deleted
        # under a detached HEAD — and the second one still has rows and a root
        # pointer to clear. Skipping the sweep left them behind FOREVER, because
        # nothing else ever revisits them. Reproduced 2026-08-31: detach, delete
        # the only ref, re-run — result was (0,0,0,0,0) with the stale row and the
        # stale pointer both intact.
        #
        # An empty set is a no-op for a repo that never had rows, which is the
        # `git init` case, so falling through costs that case nothing.

        existing = {
            n
            for (n,) in session.execute(select(Branch.name).where(Branch.workspace_id == wid)).all()
        }
        for name in names:
            if name in existing:
                continue
            session.add(Branch(workspace_id=wid, name=name))
            created += 1
        session.flush()  # assign ids before anything reads one back
        orphans = existing - set(names)

        # The root pointer: git HEAD, read above. Detached (head is None) leaves
        # it alone rather than guessing.

        # THE POINTER MOVES BEFORE THE DELETE, and that order is load-bearing.
        # `fk_workspaces_root_branch_id` is ON DELETE RESTRICT, so deleting a row
        # the workspace still points at raises ForeignKeyViolation — and because
        # this function holds ONE transaction for every workspace, that aborts the
        # whole batch and discards every workspace already reconciled. Reproduced:
        # switch off `main`, `git branch -D main` (today's delete route allows it,
        # `branches.py:348` guards only `current_branch`), then reconcile.
        #
        # RESTRICT did its job — it refused to let a caller silently orphan the
        # root pointer. This function was that caller.
        # The root pointer is FOUNDED here, never MAINTAINED here. §7.3 gives
        # moving it to promote alone, and `switch_branch` deliberately leaves it
        # be — so a reconciler that re-derived it from HEAD would hand that power
        # to whoever switched last. Measured before this guard existed: switch to
        # `new_ideas`, re-run, and the pointer moved, which made the workspace's
        # real root deletable (`delete_workspace_branch` guards the root, and `-D`
        # is not recoverable through the UI).
        #
        # So assign only when the pointer is VACANT: never set, or naming a row
        # whose git ref has just vanished.
        rooted_at = session.get(Workspace, wid).root_branch_id
        held = session.get(Branch, rooted_at) if rooted_at is not None else None
        vacant = held is None or held.name in orphans

        if vacant and head in names:
            root = session.execute(
                select(Branch).where(Branch.workspace_id == wid, Branch.name == head)
            ).scalar_one()
            session.execute(
                update(Workspace).where(Workspace.id == wid).values(root_branch_id=root.id)
            )
            rooted += 1
        elif vacant and held is not None:
            # The old root's ref is gone and no new root is knowable (detached
            # HEAD). Clearing is the honest answer; promoting a survivor is not
            # this function's decision to make.
            #
            # This must run BEFORE the delete below: the FK is ON DELETE RESTRICT,
            # so deleting a row the workspace still points at raises
            # ForeignKeyViolation, and one transaction covers every workspace.
            session.execute(
                update(Workspace).where(Workspace.id == wid).values(root_branch_id=None)
            )

        if orphans:
            session.execute(
                delete(Branch).where(Branch.workspace_id == wid, Branch.name.in_(orphans))
            )
            deleted += len(orphans)
        session.flush()

        # The SDK handle: workspaces.current_branch. A column naming a branch that
        # no longer exists has no row to carry it; leaving it on the workspace is
        # correct, because S4 is what finally drops that column.
        # It is ONE handle, so at most one row may hold it. Clearing the others
        # first is what makes that true across runs: `switch_branch` moves
        # `current_branch` and deliberately does not touch
        # `workspaces.last_sdk_session_id`, so a reconcile, a switch, and a second
        # reconcile used to leave the SAME id on both branches — the exact thing
        # §3.1 forbids, because two branches would then resume one remote session.
        # Reproduced 2026-08-31: {'feature': 'sess-X', 'main': 'sess-X'}.
        #
        # `is_distinct_from`, not `!=`: with a NULL `current_branch`, `name != NULL`
        # is NULL and the UPDATE would match nothing, leaving every stale handle in
        # place in precisely the case where none of them can be right. The column
        # is NOT NULL today so that path is unreachable and deliberately untested
        # (`test_current_branch_is_not_nullable_so_the_null_path_is_unreachable`
        # pins the reason); S4 drops this column, and `!=` would be the wrong shape
        # to leave behind for whatever replaces it.
        current = session.get(Workspace, wid).current_branch
        session.execute(
            update(Branch)
            .where(Branch.workspace_id == wid, Branch.name.is_distinct_from(current))
            .values(last_sdk_session_id=None)
        )
        if current in names:
            session.execute(
                select(Branch).where(Branch.workspace_id == wid, Branch.name == current)
            ).scalar_one().last_sdk_session_id = sdk_session_id

        logger.info(
            "branches.entity_reconciled",
            extra={
                "event": "branch_entity_reconciled",
                "workspace_id": str(wid),
                "refs": len(names),
                "root": head,
            },
        )
    return BackfillResult(
        created=created, deleted=deleted, rooted=rooted, skipped=skipped, absent=absent
    )
