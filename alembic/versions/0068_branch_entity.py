"""branches — a branch is a row with an immutable id, and the workspace names its root

Revision ID: 0068
Revises: 0067

FROZEN COPY. `_backfill_branch_entities` and its helpers below are
`grant_lab.proposal_assist.branch_entity_backfill.backfill_branch_entities` plus
what it called (`citations/gitio.py`: `head_branch`, `list_branches`,
`branch_exists`, `_git`, `_git_env`, `GitCommitError`; `paths.py`:
`workspace_repo_dir`) as they stood at 388494c8, copied in on 2026-11-03 when the
proposal workspace left master
(docs/superpowers/specs/2026-11-03-workspace-removal-design.md §3). The originals
are on the `workspace` branch and the `workspace-v1` tag. A migration must keep
running after the code it once imported is gone, so this file imports nothing
from `grant_lab`.

Changes of form only:
  * the ORM `Workspace` and `Branch` became `sa.table()`s naming the columns the
    schema had at 0068, and `session.add(Branch(...))` became a Core insert with
    the same `uuid4()` id;
  * `updated_at` is never written. The original named it with its own value to
    suppress the model's `onupdate`; a lightweight table has no `onupdate`;
  * the `workspace_id=` narrowing is gone: only Phase 0 passed it, never this
    migration.
Tests: tests/migrations/test_0068_backfill.py.
"""

from __future__ import annotations

import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import NamedTuple

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.engine import Connection

from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)

# Ceiling on any single git subprocess (gitio._GIT_TIMEOUT_SECONDS).
_GIT_TIMEOUT_SECONDS = 30.0
#: The prefix `%(refname)` puts on every local branch.
_HEADS_PREFIX = "refs/heads/"

_workspaces = sa.table(
    "workspaces",
    sa.column("id", UUID(as_uuid=True)),
    sa.column("current_branch", sa.Text()),
    sa.column("last_sdk_session_id", sa.Text()),
    sa.column("root_branch_id", UUID(as_uuid=True)),
)
_branches = sa.table(
    "branches",
    sa.column("id", UUID(as_uuid=True)),
    sa.column("workspace_id", UUID(as_uuid=True)),
    sa.column("name", sa.Text()),
    sa.column("last_sdk_session_id", sa.Text()),
)


class GitCommitError(RuntimeError):
    """A git read failed (frozen copy of gitio.GitCommitError)."""


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


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command in *repo_dir*. Never raises on a non-zero exit —
    callers decide what a failure means, because for some (`_branch_exists`) a
    non-zero exit IS the answer.

    The environment is gitio's `_git_env(repo_dir)`: no global or system config,
    no prompt, and repository DISCOVERY fenced at the repo's parent. Without that
    ceiling, a repo whose `.git` is corrupt answers with whatever repository
    encloses it, with exit 0 — and the reconciler would found rows named after
    the server's own branches and delete the workspace's real ones. The ceiling
    must be absolute: git ignores a relative entry. (gitio also defaulted
    GIT_AUTHOR_*/GIT_COMMITTER_*; no read below uses them.)
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


def _head_branch(repo_dir: Path) -> str | None:
    """The checked-out branch name, or None when HEAD is DETACHED.

    `-q` separates the cases (measured 2026-08-31): attached -> 0 and the name;
    detached -> 1; not a repository -> 128; unborn HEAD -> 0 and the name the
    first commit will land on. Only exit 1 means detached — collapsing 128 into
    it would report a broken repo as a normal state and clear its root pointer.
    """
    proc = _git(repo_dir, "symbolic-ref", "--short", "-q", "HEAD")
    if proc.returncode == 1:
        return None
    if proc.returncode != 0:
        raise GitCommitError(f"cannot read HEAD in {repo_dir}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _list_branches(repo_dir: Path) -> list[str]:
    """Every local branch, in git's own order. Empty only for a repo with no refs.

    `%(refname)` and strip, NOT `%(refname:short)`: the short form depends on what
    other refs exist (a tag named like a branch makes it `heads/feature`).

    A corrupt ref file makes `git branch` exit ZERO, warn "ignoring broken ref" on
    stderr and OMIT that branch. Treating that short list as complete would delete
    the omitted branch's row as an orphan, so it raises instead, and the caller
    skips the whole workspace.
    """
    proc = _git(repo_dir, "branch", "--format=%(refname)")
    if proc.returncode != 0:
        raise GitCommitError(f"cannot list branches in {repo_dir}: {proc.stderr.strip()}")
    if "broken ref" in proc.stderr:
        raise GitCommitError(f"broken ref in {repo_dir}: {proc.stderr.strip()}")
    return [ln.strip().removeprefix(_HEADS_PREFIX) for ln in proc.stdout.splitlines() if ln.strip()]


def _branch_exists(repo_dir: Path, name: str) -> bool:
    """Whether a LOCAL branch of this name exists (full refs/heads/ path, so a tag
    sharing the name cannot answer yes). Only exit 1 means "no such branch"; 128
    (not a repository, corruption, permissions) raises."""
    proc = _git(repo_dir, "show-ref", "--verify", "--quiet", f"refs/heads/{name}")
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    raise GitCommitError(
        f"cannot read refs in {repo_dir} (git exit {proc.returncode}): {proc.stderr.strip()}"
    )


def _real_refs(repo: Path) -> list[str]:
    """The local branch names, with git's non-ref output filtered out.

    On a detached HEAD `git branch` emits a pseudo-entry such as
    `(HEAD detached at 7ee4363)` with no `refs/heads/` prefix. `_branch_exists`
    answers no for it, so no row is founded from it.
    """
    return [name for name in _list_branches(repo) if _branch_exists(repo, name)]


def _backfill_branch_entities(conn: Connection, *, workspaces_dir: Path | str) -> BackfillResult:
    """Make `branches` agree with each workspace's local refs, and set the root
    pointer. Returns counts. Does NOT commit.

    * Every local ref gets a row, not just the current one.
    * Rows whose ref is gone are deleted — but only for a workspace whose refs were
      read successfully; an unreadable repo skips the whole workspace.
    * The root pointer is FOUNDED from git HEAD, never MOVED: it is assigned only
      when VACANT (never set, or naming a row whose ref just vanished). A detached
      HEAD leaves it alone.
    * `last_sdk_session_id` follows `workspaces.current_branch`, not HEAD, and
      lands on at most one branch. The workspace's own column is left untouched.
    * Idempotent. Skipped without raising: no repo directory (`absent`), or a repo
      git cannot read (`skipped`).
    """
    created = deleted = rooted = skipped = absent = 0
    rows = conn.execute(sa.select(_workspaces.c.id, _workspaces.c.last_sdk_session_id)).all()
    for wid, sdk_session_id in rows:
        # <WORKSPACES_DIR>/<workspace_id>/repo (paths.workspace_repo_dir)
        repo = Path(workspaces_dir) / str(wid) / "repo"
        try:
            has_git = (repo / ".git").exists()
        except OSError:
            # `Path.exists()` RAISES on EACCES. Unreadable is `skipped`, not
            # `absent`: the repo may well be there.
            skipped += 1
            log.warning(
                "branches.entity_reconcile_skipped",
                extra={"event": "branch_entity_reconcile_skipped", "workspace_id": str(wid)},
                exc_info=True,
            )
            continue
        if not has_git:
            absent += 1
            continue
        # BOTH git reads happen before anything is written for this workspace,
        # and a failure in either skips it. `_head_branch`, not a current-branch
        # read that raises for detached and broken alike: a broken repo must not
        # look detached, because detached CLEARS the root pointer.
        try:
            names = _real_refs(repo)
            head = _head_branch(repo)  # None ONLY for a genuinely detached HEAD
        except (GitCommitError, OSError, subprocess.TimeoutExpired):
            skipped += 1
            log.warning(
                "branches.entity_reconcile_skipped",
                extra={"event": "branch_entity_reconcile_skipped", "workspace_id": str(wid)},
                exc_info=True,
            )
            continue
        # No early return on an empty ref set: a repo whose last ref was deleted
        # under a detached HEAD still has rows and a pointer to clear.

        existing = {
            n
            for (n,) in conn.execute(
                sa.select(_branches.c.name).where(_branches.c.workspace_id == wid)
            ).all()
        }
        for name in names:
            if name in existing:
                continue
            conn.execute(sa.insert(_branches).values(id=uuid.uuid4(), workspace_id=wid, name=name))
            created += 1
        orphans = existing - set(names)

        # THE POINTER MOVES BEFORE THE DELETE, and that order is load-bearing:
        # fk_workspaces_root_branch_id is ON DELETE RESTRICT, so deleting the row
        # the workspace still points at would abort the whole batch.
        rooted_at = conn.execute(
            sa.select(_workspaces.c.root_branch_id).where(_workspaces.c.id == wid)
        ).scalar_one()
        held_name = (
            conn.execute(
                sa.select(_branches.c.name).where(_branches.c.id == rooted_at)
            ).scalar_one_or_none()
            if rooted_at is not None
            else None
        )
        vacant = held_name is None or held_name in orphans

        if vacant and head in names:
            root_id = conn.execute(
                sa.select(_branches.c.id).where(
                    _branches.c.workspace_id == wid, _branches.c.name == head
                )
            ).scalar_one()
            conn.execute(
                sa.update(_workspaces).where(_workspaces.c.id == wid).values(root_branch_id=root_id)
            )
            rooted += 1
        elif vacant and rooted_at is not None:
            # The old root's ref is gone and no new root is knowable (detached
            # HEAD). Clearing is the honest answer; promoting a survivor is not
            # this function's decision to make.
            conn.execute(
                sa.update(_workspaces).where(_workspaces.c.id == wid).values(root_branch_id=None)
            )

        if orphans:
            conn.execute(
                sa.delete(_branches).where(
                    _branches.c.workspace_id == wid, _branches.c.name.in_(sorted(orphans))
                )
            )
            deleted += len(orphans)

        # The SDK handle: ONE hosted conversation, so at most one row may hold it.
        # Clear the others first. `is_distinct_from`, not `!=`: with a NULL
        # current_branch, `name != NULL` is NULL and would clear nothing.
        current = conn.execute(
            sa.select(_workspaces.c.current_branch).where(_workspaces.c.id == wid)
        ).scalar_one()
        conn.execute(
            sa.update(_branches)
            .where(_branches.c.workspace_id == wid, _branches.c.name.is_distinct_from(current))
            .values(last_sdk_session_id=None)
        )
        if current in names:
            conn.execute(
                sa.update(_branches)
                .where(_branches.c.workspace_id == wid, _branches.c.name == current)
                .values(last_sdk_session_id=sdk_session_id)
            )

        log.info(
            "branches.entity_reconciled",
            extra={
                "event": "branch_entity_reconciled",
                "workspace_id": str(wid),
                "refs": len(names),
                "head": head,
            },
        )
    return BackfillResult(
        created=created, deleted=deleted, rooted=rooted, skipped=skipped, absent=absent
    )


def upgrade() -> None:
    op.create_table(
        "branches",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_sdk_session_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_branches_workspace_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workspace_id", "name", name="uq_branches_ws_name"),
    )
    op.create_index("ix_branches_workspace_id", "branches", ["workspace_id"])

    # The other half of the cycle, added as a separate ALTER — the same shape the
    # model declares with use_alter=True.
    op.add_column("workspaces", sa.Column("root_branch_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_workspaces_root_branch_id",
        "workspaces",
        "branches",
        ["root_branch_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # Then found the branches from each repo. `branches` and `root_branch_id`
    # both exist by this point.
    workspaces_dir = os.environ.get("WORKSPACES_DIR", "var/workspaces")
    result = _backfill_branch_entities(op.get_bind(), workspaces_dir=workspaces_dir)
    # Say what happened. A workspace whose repo could not be read is left with no
    # rows and no root pointer, and the migration still succeeds — that is the
    # right trade (one bad repo must not block a deploy) but it must not be
    # silent, or "the migration succeeded" reads as "every workspace was founded".
    print(
        f"0068: founded {result.created} branch rows, removed {result.deleted} orphans, "
        f"set {result.rooted} root pointers, SKIPPED {result.skipped} unreadable repos, "
        f"{result.absent} workspaces with no repo directory"
    )
    # `absent` is the one to read on a deploy. A workspace still in setup has no
    # repo and is correctly skipped — but if WORKSPACES_DIR is unset, resolved
    # against the wrong cwd, or the volume is not mounted here, EVERY workspace
    # looks that way and every other counter prints a clean zero. `absent` equal
    # to the workspace count means this migration founded nothing and nobody was
    # told.


def downgrade() -> None:
    op.drop_constraint("fk_workspaces_root_branch_id", "workspaces", type_="foreignkey")
    op.drop_column("workspaces", "root_branch_id")
    op.drop_index("ix_branches_workspace_id", table_name="branches")
    op.drop_table("branches")
