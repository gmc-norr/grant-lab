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
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from call_manager.db.models import Branch, Workspace
from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    branch_exists,
    current_branch,
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


def _real_refs(repo: Path) -> list[str]:
    """The local branch names, with git's non-ref output filtered out.

    `git branch --format=%(refname:short)` emits a pseudo-entry on a detached
    HEAD. Measured 2026-08-30:

        $ git branch --format='%(refname:short)'
        (HEAD detached at 777c52e)
        main

    Founding a row from that first line would create a branch literally named
    `(HEAD detached at 777c52e)`, which every later stage would then try to
    resolve to a worktree. `branch_exists` uses `show-ref --verify
    refs/heads/<name>`, so it answers no for anything that is not a real ref.
    """
    return [name for name in list_branches(repo) if branch_exists(repo, name)]


def backfill_branch_entities(session: Session, *, workspaces_dir: Path) -> BackfillResult:
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
    """
    created = deleted = rooted = skipped = 0
    rows = session.execute(select(Workspace.id, Workspace.last_sdk_session_id)).all()
    for wid, sdk_session_id in rows:
        repo = workspace_repo_dir(workspaces_dir, wid)
        if not (repo / ".git").exists():
            continue  # still in setup; nothing to found a branch on, and not a failure
        try:
            names = _real_refs(repo)
        except (GitCommitError, OSError):
            skipped += 1
            logger.warning(
                "branches.entity_reconcile_skipped",
                extra={"event": "branch_entity_reconcile_skipped", "workspace_id": str(wid)},
                exc_info=True,
            )
            continue
        if not names:
            continue  # `git init` with no commit: a branch name, but no ref

        existing = {
            n
            for (n,) in session.execute(select(Branch.name).where(Branch.workspace_id == wid)).all()
        }
        for name in names:
            if name in existing:
                continue
            session.add(Branch(workspace_id=wid, name=name))
            created += 1
        orphans = existing - set(names)
        if orphans:
            session.execute(
                delete(Branch).where(Branch.workspace_id == wid, Branch.name.in_(orphans))
            )
            deleted += len(orphans)
        session.flush()  # assign ids before anything reads one back

        # The root pointer: git HEAD. Detached leaves it alone rather than guessing.
        try:
            head = current_branch(repo)
        except (GitCommitError, OSError):
            head = None
        if head in names:
            root = session.execute(
                select(Branch).where(Branch.workspace_id == wid, Branch.name == head)
            ).scalar_one()
            session.execute(
                update(Workspace).where(Workspace.id == wid).values(root_branch_id=root.id)
            )
            rooted += 1

        # The SDK handle: workspaces.current_branch. A column naming a branch that
        # no longer exists has no row to carry it; leaving it on the workspace is
        # correct, because S4 is what finally drops that column.
        current = session.get(Workspace, wid).current_branch
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
    return BackfillResult(created=created, deleted=deleted, rooted=rooted, skipped=skipped)
