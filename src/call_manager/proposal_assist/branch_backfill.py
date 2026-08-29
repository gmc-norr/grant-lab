"""Backfill `workspaces.current_branch` from each repo's actual HEAD.

A separate module, not migration-local, for one reason: the migration is
untestable in place, and this is the half that can be wrong. The migration
imports it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from call_manager.db.models import Workspace
from call_manager.proposal_assist.citations.gitio import current_branch
from call_manager.proposal_assist.paths import workspace_repo_dir

logger = logging.getLogger(__name__)


def backfill_current_branch(session: Session, *, workspaces_dir: Path) -> int:
    """Set `current_branch` from `git symbolic-ref --short HEAD` per repo.

    Returns the number of rows changed. Does NOT commit.

    A workspace with no repo directory keeps the column default: there is no
    repo to read and nothing has happened in it yet, so 'main' is not a guess.
    A DETACHED HEAD raises — see the test for why guessing is worse than failing.
    """
    changed = 0
    for (wid,) in session.execute(select(Workspace.id)).all():
        repo = workspace_repo_dir(workspaces_dir, wid)
        if not (repo / ".git").exists():
            continue
        name = current_branch(repo)  # raises GitCommitError on a detached HEAD
        session.execute(update(Workspace).where(Workspace.id == wid).values(current_branch=name))
        changed += 1
        logger.info(
            "branches.backfill",
            extra={"event": "branch_backfill", "workspace_id": str(wid), "branch": name},
        )
    return changed
