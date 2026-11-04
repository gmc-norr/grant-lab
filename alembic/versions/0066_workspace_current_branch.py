"""workspaces: current_branch, the source of truth for which branch a workspace is on

Revision ID: 0066
Revises: 0065

FROZEN COPY. `_backfill_current_branch` and its helpers below are
`grant_lab.proposal_assist.branch_backfill.backfill_current_branch` plus what it
called (`citations/gitio.py`: `current_branch`, `_git`, `_git_env`,
`GitCommitError`; `paths.py`: `workspace_repo_dir`) as they stood at 388494c8,
copied in on 2026-11-03 when the proposal workspace left master
(docs/superpowers/specs/2026-11-03-workspace-removal-design.md §3). The originals
are on the `workspace` branch and the `workspace-v1` tag. A migration must keep
running after the code it once imported is gone, so this file imports nothing
from `grant_lab`.

Changes of form only. The ORM `Workspace` became a lightweight `sa.table()`, so
no later model change can alter what this migration does. The ORM UPDATE also
set `updated_at = now()` (the model's `onupdate`); that is written out below, so
the rows come out as they did. Tests: tests/migrations/test_0066_backfill.py.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.engine import Connection

from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)

# Ceiling on any single git subprocess (gitio._GIT_TIMEOUT_SECONDS).
_GIT_TIMEOUT_SECONDS = 30.0

_workspaces = sa.table(
    "workspaces",
    sa.column("id", UUID(as_uuid=True)),
    sa.column("current_branch", sa.Text()),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)


class GitCommitError(RuntimeError):
    """A git read failed (frozen copy of gitio.GitCommitError)."""


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command in *repo_dir*. Never raises on a non-zero exit.

    The environment is gitio's `_git_env(repo_dir)`: no global or system config,
    no prompt, and repository DISCOVERY fenced at the repo's parent. Without that
    ceiling, a repo whose `.git` is corrupt answers with whatever repository
    encloses it, with exit 0. The ceiling must be absolute: git ignores a relative
    entry, and WORKSPACES_DIR defaults to the relative "var/workspaces". (gitio
    also defaulted GIT_AUTHOR_*/GIT_COMMITTER_*; no read below uses them.)
    """
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CEILING_DIRECTORIES"] = str(Path(repo_dir).resolve().parent)
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        env=env,
    )


def _current_branch(repo_dir: Path) -> str:
    """The checked-out branch name.

    Succeeds on an UNBORN HEAD (a repo with no commits): git still knows which
    branch the first commit will land on, and that name is the right answer.
    Raises GitCommitError on a DETACHED HEAD, where there is no branch name and
    no safe default.
    """
    proc = _git(repo_dir, "symbolic-ref", "--short", "HEAD")
    if proc.returncode != 0:
        raise GitCommitError(f"HEAD is not on a branch in {repo_dir}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _backfill_current_branch(conn: Connection, *, workspaces_dir: Path | str) -> int:
    """Set `current_branch` from `git symbolic-ref --short HEAD` per repo.

    Returns the number of rows changed. Does NOT commit.

    A workspace with no repo directory keeps the column default: there is no
    repo to read and nothing has happened in it yet, so 'main' is not a guess.
    A DETACHED HEAD raises, failing the migration: nothing in the app detached
    HEAD, so a detached repo means something outside the app did, and writing a
    plausible 'main' would discard that state.
    """
    changed = 0
    for (wid,) in conn.execute(sa.select(_workspaces.c.id)).all():
        # <WORKSPACES_DIR>/<workspace_id>/repo (paths.workspace_repo_dir)
        repo = Path(workspaces_dir) / str(wid) / "repo"
        if not (repo / ".git").exists():
            continue
        name = _current_branch(repo)  # raises GitCommitError on a detached HEAD
        conn.execute(
            sa.update(_workspaces)
            .where(_workspaces.c.id == wid)
            # updated_at: what the ORM UPDATE added from Workspace's onupdate.
            .values(current_branch=name, updated_at=sa.func.now())
        )
        changed += 1
        log.info(
            "branches.backfill",
            extra={"event": "branch_backfill", "workspace_id": str(wid), "branch": name},
        )
    return changed


def upgrade() -> None:
    # server_default so existing rows land somewhere valid before the backfill;
    # NOT NULL so no code path has to consider a null branch.
    op.add_column(
        "workspaces",
        sa.Column(
            "current_branch",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'main'"),
        ),
    )
    # Then correct it from each repo. The default is right for NEW rows only:
    # backfilling every existing row to 'main' is a guess, and a wrong guess
    # makes §4.1's reconciliation check out a branch that may not exist.
    workspaces_dir = os.environ.get("WORKSPACES_DIR", "var/workspaces")
    _backfill_current_branch(op.get_bind(), workspaces_dir=workspaces_dir)


def downgrade() -> None:
    op.drop_column("workspaces", "current_branch")
