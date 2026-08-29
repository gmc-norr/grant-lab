# Workspace Branches Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a workspace owner create, switch between and delete real git branches in the workspace repo, without a switch ever serving one branch's text under another branch's name.

**Architecture:** `workspaces.current_branch` is the source of truth and git HEAD is reconciled to it. A switch runs as one mutex-held transaction that tears the collaborative rooms down *first*, checkpoints the frozen log to the branch being left, truncates it, bumps the lifecycle generation, moves the branch, and checks out — with a compensating checkout on rollback. Three existing behaviours must change first, because the switch is unsafe without them.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, PostgreSQL 16, pytest (+ testcontainers), `anyio` for the asyncio room manager. React 18 + Vite + Mantine + TanStack Query on the front end.

**Source spec:** `docs/superpowers/specs/2026-08-29-workspace-branches-design.md` (revision 5, `423e808d`). Merge is deliberately **not** in scope — see `2026-08-29-workspace-branch-merge-design.md`.

## Global Constraints

- **Owner only** for create, switch and delete. Use `require_owner` from `call_manager.proposal_assist.web._perms`.
- **Every git or log write in a switch happens under `acquire_commit_mutex_at_generation`** at the caller's stamped generation. `pg_advisory_xact_lock` is re-entrant within one transaction, so calling `checkpoint_artifact` (which re-acquires it) from inside the mutex-held switch transaction is safe and intended.
- **The teardown always completes.** Aborting a switch must never leave a room popped from the registry, un-tombstoned, with open sockets. Abort the *switch*, never the *teardown*.
- **A switch closes editor sockets with 1013, never 1008.** `frontend/src/collab/collabProvider.ts:234` treats 1008 and 1009 as terminal and does not reconnect.
- **Never adjust an expected number to make it match.** If a count differs from this plan, name the term that changed and record it as a plan defect.
- Run the suite with `uv run pytest -n 16 --dist loadfile`. Baseline before this work: **3361 passed, 1 skipped**. The `1 skipped` is the signal — it is the real-SDK test gated on `ANTHROPIC_API_KEY`.
- Frontend commands run from `frontend/`: `pnpm exec tsc --noEmit`, `pnpm vitest run`, `pnpm build`. **`pnpm add` is blocked** by a 7-day `minimumReleaseAge` — add no dependencies.
- Lint: `uv run ruff check .` and `uv run ruff format --check .`.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/call_manager/proposal_assist/citations/gitio.py` | **Modify.** Gains the branch primitives: read, list, create, delete, checkout, validate. Pure subprocess wrappers, no app state. |
| `alembic/versions/0066_workspace_current_branch.py` | **Create.** The column plus a real per-repo backfill. |
| `src/call_manager/db/models.py` | **Modify.** `Workspace.current_branch`. |
| `src/call_manager/proposal_assist/collab/hydrate.py` | **Modify.** Move the mutex acquisition before the log read. |
| `src/call_manager/proposal_assist/collab/room.py` | **Modify.** Adds `RoomManager.prepare_branch_switch` and a close-code parameter on the per-room teardown. |
| `src/call_manager/proposal_assist/web/collab_ws.py` | **Modify.** A stale generation on connect closes 1013, not 1008. |
| `src/call_manager/proposal_assist/turns/processor.py` | **Modify.** `claim_turn_job` takes the shared lease. |
| `src/call_manager/proposal_assist/branches.py` | **Create.** The three operations — `create_branch`, `switch_branch`, `delete_branch` — and the errors they raise. This is the only file that knows the switch sequence. |
| `src/call_manager/proposal_assist/web/branches.py` | **Create.** Four owner-only routes over `branches.py`. |
| `frontend/src/api/branches.ts` | **Create.** The four hooks. |
| `frontend/src/cockpit/panes/BranchPicker.tsx` | **Create.** The branch control, rendered above the version graph. |
| `frontend/src/shell/StatusBar.tsx` | **Modify.** Shows the current branch beside the commit. |

Ten tasks. Tasks 1–6 are prerequisites and each is independently mergeable; task 7 is the switch itself and depends on all of them.

---

### Task 1: git branch primitives

**Files:**
- Modify: `src/call_manager/proposal_assist/citations/gitio.py` (append after `current_head`, which ends at line 322)
- Test: `tests/proposal_assist/test_gitio_branches.py` (create)

**Interfaces:**
- Consumes: `_git_env()`, `_GIT_TIMEOUT_SECONDS`, `GitCommitError` — all already in `gitio.py`.
- Produces:
  - `current_branch(repo_dir: Path) -> str`
  - `list_branches(repo_dir: Path) -> list[str]`
  - `branch_exists(repo_dir: Path, name: str) -> bool`
  - `is_valid_branch_name(name: str) -> bool`
  - `create_branch(repo_dir: Path, name: str) -> None`
  - `delete_branch(repo_dir: Path, name: str) -> None`
  - `checkout_branch(repo_dir: Path, name: str) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_gitio_branches.py`:

```python
"""git branch primitives. Every expectation here was measured against real git
on 2026-08-29 before being written down — including the one that is
counter-intuitive (an unborn HEAD still has a branch NAME)."""

import subprocess

import pytest

from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    branch_exists,
    checkout_branch,
    create_branch,
    current_branch,
    delete_branch,
    is_valid_branch_name,
    list_branches,
)


def _repo(tmp_path, *, commit=True):
    d = tmp_path / "repo"
    d.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=d, check=True)
    if commit:
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "first"],
            cwd=d, check=True,
            env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                 "PATH": "/usr/bin:/bin"},
        )
    return d


def test_current_branch_reads_the_checked_out_branch(tmp_path):
    assert current_branch(_repo(tmp_path)) == "main"


def test_current_branch_works_on_an_UNBORN_head(tmp_path):
    # Measured: `git symbolic-ref --short HEAD` exits 0 and prints "main" on a
    # repo with no commits. The migration depends on this — a workspace still in
    # setup must not fail the backfill.
    assert current_branch(_repo(tmp_path, commit=False)) == "main"


def test_current_branch_raises_on_a_DETACHED_head(tmp_path):
    d = _repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=d, check=True)
    with pytest.raises(GitCommitError):
        current_branch(d)


def test_create_then_list_and_exists(tmp_path):
    d = _repo(tmp_path)
    create_branch(d, "aims-ambitious")
    assert sorted(list_branches(d)) == ["aims-ambitious", "main"]
    assert branch_exists(d, "aims-ambitious") is True
    assert branch_exists(d, "nope") is False
    # Creating must NOT move HEAD.
    assert current_branch(d) == "main"


def test_create_on_an_unborn_repo_raises(tmp_path):
    # Measured: `git branch x HEAD` exits 128 with
    # "fatal: not a valid object name: 'HEAD'".
    with pytest.raises(GitCommitError):
        create_branch(_repo(tmp_path, commit=False), "x")


def test_checkout_moves_head(tmp_path):
    d = _repo(tmp_path)
    create_branch(d, "short-version")
    checkout_branch(d, "short-version")
    assert current_branch(d) == "short-version"


def test_delete_removes_the_ref(tmp_path):
    d = _repo(tmp_path)
    create_branch(d, "dead-end")
    delete_branch(d, "dead-end")
    assert branch_exists(d, "dead-end") is False


def test_delete_is_force_and_removes_an_UNMERGED_branch(tmp_path):
    # -D not -d: the branch a user most wants to throw away is the one whose
    # commits went nowhere, and `-d` refuses exactly that.
    d = _repo(tmp_path)
    create_branch(d, "abandoned")
    checkout_branch(d, "abandoned")
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "work"], cwd=d, check=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
             "PATH": "/usr/bin:/bin"},
    )
    checkout_branch(d, "main")
    delete_branch(d, "abandoned")
    assert branch_exists(d, "abandoned") is False


def test_delete_of_the_current_branch_raises(tmp_path):
    # git refuses this itself (exit 1, "cannot delete branch ... used by
    # worktree"). The app refuses first, but this proves the floor exists.
    d = _repo(tmp_path)
    with pytest.raises(GitCommitError):
        delete_branch(d, "main")


@pytest.mark.parametrize("name", ["a..b", "foo.lock", "-lead", "with space", ""])
def test_invalid_names_are_rejected(name):
    # Delegated to `git check-ref-format --branch`. Measured 2026-08-29: git
    # rejects each of these. Do NOT hand-roll the rules.
    assert is_valid_branch_name(name) is False


@pytest.mark.parametrize("name", ["good-name", "aims/ambitious", "v2"])
def test_valid_names_are_accepted(name):
    assert is_valid_branch_name(name) is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/proposal_assist/test_gitio_branches.py -q`
Expected: a **collection error** — `ImportError: cannot import name 'branch_exists'`. That is the expected failure for a not-yet-written module surface; it becomes real assertion failures once the functions exist.

- [ ] **Step 3: Implement the primitives**

Append to `src/call_manager/proposal_assist/citations/gitio.py`, after `current_head`:

```python
def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command in *repo_dir*, capturing text output. Never raises on
    a non-zero exit — callers decide what a failure means, because for some of
    these (`branch_exists`) a non-zero exit IS the answer."""
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        env=_git_env(),
    )


def current_branch(repo_dir: Path) -> str:
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


def list_branches(repo_dir: Path) -> list[str]:
    """Every local branch, in git's own order. Empty only for a repo with no refs."""
    proc = _git(repo_dir, "branch", "--format=%(refname:short)")
    if proc.returncode != 0:
        raise GitCommitError(f"cannot list branches in {repo_dir}: {proc.stderr.strip()}")
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def branch_exists(repo_dir: Path, name: str) -> bool:
    """Whether a LOCAL branch of this name exists. Uses the full refs/heads/ path
    so a tag or a commit-ish that happens to share the name cannot answer yes."""
    return _git(repo_dir, "show-ref", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0


def is_valid_branch_name(name: str) -> bool:
    """Delegated to `git check-ref-format --branch`, which owns these rules.

    A hand-rolled check that accepts a name git rejects produces a half-created
    branch; one that rejects a name git accepts is an unexplainable refusal.
    This runs without a repository — check-ref-format reads none.
    """
    if not name or name != name.strip():
        return False
    proc = subprocess.run(
        ["git", "check-ref-format", "--branch", name],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        env=_git_env(),
    )
    return proc.returncode == 0


def create_branch(repo_dir: Path, name: str) -> None:
    """Create *name* at the current HEAD. Does NOT move HEAD.

    Fails on a repo with no commits — `git branch <name> HEAD` exits 128 with
    "not a valid object name: 'HEAD'" — which is the correct refusal for a
    workspace still in setup.
    """
    proc = _git(repo_dir, "branch", name, "HEAD")
    if proc.returncode != 0:
        raise GitCommitError(f"cannot create branch {name!r}: {proc.stderr.strip()}")


def delete_branch(repo_dir: Path, name: str) -> None:
    """Delete *name*, FORCE (`-D`).

    `-d` refuses to delete a branch whose commits are unmerged — which is
    precisely the branch a user throws away, and a refusal that could not be
    explained in the UI. git still refuses to delete the CHECKED-OUT branch, and
    that refusal is kept as the floor beneath the app's own check.
    """
    proc = _git(repo_dir, "branch", "-D", name)
    if proc.returncode != 0:
        raise GitCommitError(f"cannot delete branch {name!r}: {proc.stderr.strip()}")


def checkout_branch(repo_dir: Path, name: str) -> None:
    """Move HEAD to *name*. The working tree follows."""
    proc = _git(repo_dir, "checkout", "-q", name)
    if proc.returncode != 0:
        raise GitCommitError(f"cannot check out branch {name!r}: {proc.stderr.strip()}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/proposal_assist/test_gitio_branches.py -q`
Expected: `12 passed`.

- [ ] **Step 5: Prove `is_valid_branch_name` is not a rubber stamp**

Temporarily replace its body with `return True`, then run the file.
Expected: `test_invalid_names_are_rejected` FAILS on 5 parametrised cases.
Restore the real body and re-run — `12 passed`.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src/call_manager/proposal_assist/citations/gitio.py tests/proposal_assist/test_gitio_branches.py
uv run ruff format --check src/call_manager/proposal_assist/citations/gitio.py tests/proposal_assist/test_gitio_branches.py
git add src/call_manager/proposal_assist/citations/gitio.py tests/proposal_assist/test_gitio_branches.py
git commit -m "feat(gitio): branch primitives — read, list, create, delete, checkout, validate"
```

---

### Task 2: `workspaces.current_branch` column, backfilled from the repo

**Files:**
- Modify: `src/call_manager/db/models.py` (the `Workspace` class)
- Create: `alembic/versions/0066_workspace_current_branch.py`
- Test: `tests/proposal_assist/test_branch_column_migration.py` (create)

**Interfaces:**
- Consumes: `current_branch` from Task 1; `workspace_repo_dir` from `call_manager.proposal_assist.paths`.
- Produces: `Workspace.current_branch: Mapped[str]` — not null, server default `'main'`.

**Why the backfill is not optional:** `'main'` is right for *new* rows. Backfilling every existing row to `'main'` is a guess, and a wrong guess makes §4.1's reconciliation check out a branch that may not exist — turning a working workspace into a broken one on deploy. Measured 2026-08-29: all 16 workspace repos on this instance are on `main`, so the backfill is expected to change nothing here. It is required anyway; the failure it prevents is silent and total.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_branch_column_migration.py`:

```python
"""The current_branch backfill reads each repo, and refuses to guess.

Uses the house fixtures: `make_workspace` (tests/proposal_assist/conftest.py:200)
and `pg_session_factory` (tests/conftest.py:69). Do not construct a Workspace by
hand — it needs an owner and, for some paths, a call.
"""

import subprocess
import uuid

import pytest
from sqlalchemy import select

from call_manager.db.models import Workspace
from call_manager.proposal_assist.branch_backfill import backfill_current_branch
from call_manager.proposal_assist.citations.gitio import GitCommitError
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import make_workspace

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": "/usr/bin:/bin",
}


def _repo_on(workspaces_dir, workspace_id, branch, *, commit=True, detach=False):
    d = workspace_repo_dir(workspaces_dir, workspace_id)
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", branch, "."], cwd=d, check=True)
    if commit:
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "x"],
                       cwd=d, check=True, env=_GIT_ENV)
    if detach:
        subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=d, check=True)
    return d


def _branch_of(session_factory, wid):
    with session_factory() as s:
        return s.execute(
            select(Workspace.current_branch).where(Workspace.id == wid)
        ).scalar_one()


def test_backfill_reads_the_repos_actual_branch(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo_on(tmp_path, wid, "aims-ambitious")

    with pg_session_factory() as s:
        assert backfill_current_branch(s, workspaces_dir=tmp_path) == 1
        s.commit()

    assert _branch_of(pg_session_factory, wid) == "aims-ambitious"


def test_backfill_records_an_unborn_repos_branch_rather_than_failing(
    pg_session_factory, tmp_path
):
    # A workspace still in setup has a repo with no commits. Its branch NAME is
    # known and correct; failing here would block the whole migration on it.
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo_on(tmp_path, wid, "main", commit=False)

    with pg_session_factory() as s:
        backfill_current_branch(s, workspaces_dir=tmp_path)
        s.commit()

    assert _branch_of(pg_session_factory, wid) == "main"


def test_backfill_leaves_a_workspace_with_no_repo_at_the_default(
    pg_session_factory, tmp_path
):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    # No repo directory created at all.
    with pg_session_factory() as s:
        assert backfill_current_branch(s, workspaces_dir=tmp_path) == 0
        s.commit()

    assert _branch_of(pg_session_factory, wid) == "main"


def test_backfill_RAISES_on_a_detached_head(pg_session_factory, tmp_path):
    # Nothing in the app detaches HEAD, so a detached repo means something
    # outside the app did. Writing a plausible 'main' would discard that state.
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo_on(tmp_path, wid, "main", detach=True)

    with pg_session_factory() as s, pytest.raises(GitCommitError):
        backfill_current_branch(s, workspaces_dir=tmp_path)
```

**Note on isolation:** these tests write into `tmp_path` and each creates its own
workspace, but `backfill_current_branch` walks EVERY workspace row. Other rows
from the same test transaction have no repo under this `tmp_path`, so they are
skipped by the `.git` existence check — which is why the count assertions above
are exact.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_branch_column_migration.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'call_manager.proposal_assist.branch_backfill'`.

- [ ] **Step 3: Add the model field**

In `src/call_manager/db/models.py`, in the `Workspace` class, beside `lifecycle_generation`:

```python
    #: The branch this workspace is on. The DATABASE is the source of truth;
    #: git HEAD is reconciled to this value before any git read or write
    #: (2026-08-29-workspace-branches-design.md §4.1). Never inferred from HEAD.
    current_branch: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'main'")
    )
```

If `text` is not already imported in `models.py`, add it to the existing `from sqlalchemy import ...` line.

- [ ] **Step 4: Write the backfill as an importable function**

Create `src/call_manager/proposal_assist/branch_backfill.py`:

```python
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
        name = current_branch(repo)   # raises GitCommitError on a detached HEAD
        session.execute(
            update(Workspace).where(Workspace.id == wid).values(current_branch=name)
        )
        changed += 1
        logger.info(
            "branches.backfill",
            extra={"event": "branch_backfill", "workspace_id": str(wid), "branch": name},
        )
    return changed
```

- [ ] **Step 5: Write the migration**

Create `alembic/versions/0066_workspace_current_branch.py`:

```python
"""workspaces: current_branch, the source of truth for which branch a workspace is on

Revision ID: 0066
Revises: 0065
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


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
    from sqlalchemy.orm import Session

    from call_manager.config import get_settings
    from call_manager.proposal_assist.branch_backfill import backfill_current_branch

    with Session(bind=op.get_bind()) as s:
        backfill_current_branch(s, workspaces_dir=get_settings().workspaces_dir)
        s.commit()


def downgrade() -> None:
    op.drop_column("workspaces", "current_branch")
```

`get_settings().workspaces_dir` is a **`str`** (`config.py:178`), not a `Path`. That is fine: `workspace_repo_dir` accepts `Path | str` (`paths.py:9`). Do not wrap it.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/proposal_assist/test_branch_column_migration.py -q`
Expected: `4 passed`.

- [ ] **Step 7: Prove the detached-HEAD refusal is real**

Change `backfill_current_branch` to swallow the error (`except GitCommitError: continue`) and re-run.
Expected: `test_backfill_RAISES_on_a_detached_head` FAILS with `DID NOT RAISE`. Restore and re-run — `4 passed`.

- [ ] **Step 8: Verify the migration round-trips**

Run: `uv run pytest tests/test_alembic_v2_round_trip.py -q`
Expected: passes. This is the existing gate that catches a broken `upgrade`/`downgrade` pair.

- [ ] **Step 9: Lint and commit**

```bash
uv run ruff check src/call_manager/db/models.py src/call_manager/proposal_assist/branch_backfill.py alembic/versions/0066_workspace_current_branch.py tests/proposal_assist/test_branch_column_migration.py
uv run ruff format --check src/call_manager/db/models.py src/call_manager/proposal_assist/branch_backfill.py alembic/versions/0066_workspace_current_branch.py tests/proposal_assist/test_branch_column_migration.py
git add -A
git commit -m "feat(db): workspaces.current_branch, backfilled from each repo's real HEAD"
```

---

### Task 3: `load_or_hydrate` takes the commit mutex BEFORE reading the log

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/hydrate.py:27-71`
- Test: `tests/proposal_assist/collab/test_collab_hydrate_mutex_order.py` (create)

**Interfaces:**
- Consumes: nothing new.
- Produces: no signature change. `load_or_hydrate(session, *, stamp, workspace_id, artifact, workspaces_dir) -> list[bytes]` keeps its shape; only *when* it takes the mutex changes.

**Why (spec §4.4):** today the mutex is acquired at `hydrate.py:62-67`, but the function returns at `hydrate.py:30-32` whenever the log has rows. The mutex is therefore on the **cold seed path only**. A client reconnecting mid-switch — after the rooms are torn down, before the log is truncated — reads the *old* branch's rows without blocking on anything, and the room manager publishes a fresh room from them that the switch has already torn down and will not tear down again. The switch then truncates underneath it.

**Two consequences to accept deliberately, not discover:**
- Every room open now pays one per-workspace mutex acquisition, not just the first open ever. Room open is a connect-time path, not a per-keystroke one.
- `CommitMutexTimeout` can now fire on an ordinary reconnect. The handling already exists (`hydrate.py:52-61`, `collab_ws.py:172-187`); what changes is how often it can fire, not what happens.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/collab/test_collab_hydrate_mutex_order.py`:

```python
"""load_or_hydrate must take the commit mutex before it READS the log, not only
before it writes a seed. Spec §4.4 — this is what makes a mid-switch reconnect
block instead of republishing the old branch's room."""

import uuid

import pytest

from call_manager.proposal_assist.collab import hydrate, store
from call_manager.proposal_assist.collab.hydrate import load_or_hydrate
from tests.proposal_assist.conftest import make_workspace, stamp_for


def test_mutex_is_taken_even_when_the_log_ALREADY_has_rows(pg_session_factory, tmp_path, monkeypatch):
    # The warm path. Before this change it took no lock at all, which is the
    # whole defect: a reconnect during a branch switch sailed past the mutex.
    with pg_session_factory() as s:
        ws = make_workspace(s, title="w")
        wid = ws.id
        s.commit()

    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None, update=b"\x00\x00")
        s.commit()

    calls: list[tuple] = []
    real = hydrate.acquire_commit_mutex_at_generation

    def _spy(session, workspace_id, *, generation, timeout_seconds):
        calls.append((workspace_id, generation))
        return real(session, workspace_id, generation=generation, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(hydrate, "acquire_commit_mutex_at_generation", _spy)

    with pg_session_factory() as s:
        stamp = stamp_for(s, wid)
        updates = load_or_hydrate(
            s, stamp=stamp, workspace_id=wid, artifact="draft", workspaces_dir=tmp_path
        )

    assert updates == [b"\x00\x00"]      # still returns the rows
    assert calls == [(wid, 0)]           # and it LOCKED first


def test_mutex_is_taken_before_load_updates_is_called(pg_session_factory, tmp_path, monkeypatch):
    # Order, not merely presence: a lock taken after the read would satisfy the
    # test above and still lose the race this exists to close.
    with pg_session_factory() as s:
        ws = make_workspace(s, title="w")
        wid = ws.id
        s.commit()
    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None, update=b"\x00\x00")
        s.commit()

    order: list[str] = []
    real_mutex = hydrate.acquire_commit_mutex_at_generation
    real_load = hydrate.store.load_updates

    def _mutex(session, workspace_id, *, generation, timeout_seconds):
        order.append("mutex")
        return real_mutex(session, workspace_id, generation=generation, timeout_seconds=timeout_seconds)

    def _load(session, *, workspace_id, artifact):
        order.append("load")
        return real_load(session, workspace_id=workspace_id, artifact=artifact)

    monkeypatch.setattr(hydrate, "acquire_commit_mutex_at_generation", _mutex)
    monkeypatch.setattr(hydrate.store, "load_updates", _load)

    with pg_session_factory() as s:
        load_or_hydrate(
            s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft",
            workspaces_dir=tmp_path,
        )

    assert order == ["mutex", "load"]


def test_a_stale_generation_is_refused_on_the_WARM_path_too(pg_session_factory, tmp_path):
    # Consequence of moving the lock, and the behaviour §4.4 relies on: a connect
    # stamped before a switch wakes holding an old generation and is refused,
    # rather than being handed the pre-switch log.
    from call_manager.proposal_assist.fencing import WorkspaceGenerationStale
    from call_manager.workspaces import bump_lifecycle_generation

    with pg_session_factory() as s:
        ws = make_workspace(s, title="w")
        wid = ws.id
        s.commit()
    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None, update=b"\x00\x00")
        s.commit()

    with pg_session_factory() as s:
        stale = stamp_for(s, wid)          # belief formed BEFORE the bump

    with pg_session_factory() as s:
        bump_lifecycle_generation(s, workspace_id=wid)
        s.commit()

    with pg_session_factory() as s, pytest.raises(WorkspaceGenerationStale):
        load_or_hydrate(
            s, stamp=stale, workspace_id=wid, artifact="draft", workspaces_dir=tmp_path
        )
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/proposal_assist/collab/test_collab_hydrate_mutex_order.py -q`
Expected: all three FAIL on assertions, not on collection —
`test_mutex_is_taken_even_when_the_log_ALREADY_has_rows` with `assert [] == [(UUID(...), 0)]`,
`test_mutex_is_taken_before_load_updates_is_called` with `assert ['load'] == ['mutex', 'load']`,
`test_a_stale_generation_is_refused_on_the_WARM_path_too` with `DID NOT RAISE`.

If any of them fails at import instead, stop: the test is wrong, not the code.

- [ ] **Step 3: Move the acquisition**

In `src/call_manager/proposal_assist/collab/hydrate.py`, replace the body of `load_or_hydrate` from its first statement through the existing `acquire_commit_mutex_at_generation(...)` call with:

```python
    # SPEC §4.4 — the mutex comes BEFORE the read, not before the seed write.
    #
    # This used to guard only the cold seed path, below. That left the WARM path
    # (the log already has rows) taking no lock at all, and a branch switch has a
    # window — rooms torn down, log not yet truncated — in which a reconnect on
    # that path reads the OLD branch's rows and the manager publishes a room from
    # them that the switch has already torn down and will not tear down again.
    # The switch then truncates underneath it and the workspace serves the old
    # branch's text under the new branch's name.
    #
    # Cost, accepted knowingly: every room open now pays one per-workspace mutex
    # acquisition rather than only the first open ever, and CommitMutexTimeout
    # becomes reachable on an ordinary reconnect. Room open is a connect-time
    # path, not a per-keystroke one. The handling is unchanged and is described
    # in the comment below.
    #
    # Do NOT catch WorkspaceDeleted / WorkspaceGenerationStale / CommitMutexTimeout
    # here. Returning [] would be indistinguishable from "this artifact has no
    # content yet", and accept_via_kernel would then build an EMPTY room and apply
    # a section write to it. All three are already house contracts: on the HTTP
    # paths WorkspaceDeleted and WorkspaceGenerationStale are a 404 (app-level
    # handlers) and CommitMutexTimeout the standard busy -> 429. On the WEBSOCKET
    # path those handlers still RUN (Starlette dispatches them in the websocket
    # scope too), but all they can emit on an already-accepted socket is a denial
    # response the client cannot see or act on — so collab_ws maps them to a close
    # frame instead; see its except around MANAGER.acquire.
    acquire_commit_mutex_at_generation(
        session,
        stamp.workspace_id,
        generation=stamp.generation,
        timeout_seconds=_HYDRATE_MUTEX_TIMEOUT_SECONDS,
    )
    updates = store.load_updates(session, workspace_id=workspace_id, artifact=artifact)
    if updates:
        return updates
    if not state.is_collab_enabled(session, workspace_id=workspace_id, artifact=artifact):
        return []
    md = read_head_markdown(workspace_repo_dir(workspaces_dir, workspace_id), artifact)
    if md is None:
        # Collab enabled but the artifact file has never been committed. Leave the
        # room empty (Slice-1 behavior) rather than persisting a synthetic seed.
        return []
    seed = pm_json_to_ydoc(markdown_to_pm_or_empty(md)).get_update()
```

Then keep the existing `store.append_update(...)` / `return [seed]` tail unchanged, and update `_HYDRATE_MUTEX_TIMEOUT_SECONDS`'s comment (`hydrate.py:21-24`), which currently says the seed "runs once per room, ever":

```python
# Bounds the wait on EVERY room open, not only the first one ever: since spec
# §4.4 the mutex is taken before the log read so a mid-switch reconnect blocks
# rather than republishing the pre-switch room. On contention the caller's
# operation fails with the house busy semantics (429 on HTTP, a 1013 close frame
# on the websocket) and the client retries.
_HYDRATE_MUTEX_TIMEOUT_SECONDS = 5
```

- [ ] **Step 4: Run the new tests**

Run: `uv run pytest tests/proposal_assist/collab/test_collab_hydrate_mutex_order.py -q`
Expected: `3 passed`.

- [ ] **Step 5: Run every existing hydrate and collab test**

Run: `uv run pytest tests/proposal_assist/collab -q -n 8 --dist loadfile`
Expected: all pass. This step exists because moving a lock earlier is exactly the change that surfaces a lock-order inversion. **If anything hangs rather than fails, that is the inversion** — record it as a plan defect naming the call path, do not raise the timeout.

- [ ] **Step 6: Prove there is no inversion — nothing holds `room.lock` across this call**

Run: `grep -rn "load_or_hydrate\|load=_load\|load=" src/call_manager/proposal_assist/ --include=*.py | grep -v test`
Expected: the only production caller is `collab_ws.py`'s `_load` closure passed to `MANAGER.acquire`, and `RoomManager.acquire` awaits `load()` **outside** the manager lock and holds no `room.lock` (`room.py:484-487, 506-521`). Record what you actually find. If a caller does hold `room.lock`, stop and escalate — this is spec §4.4's named risk.

- [ ] **Step 7: Full suite**

Run: `uv run pytest -n 16 --dist loadfile`
Expected: `3364 passed, 1 skipped` (the baseline 3361 plus this task's 3). If the count differs, name the term that changed.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check src/call_manager/proposal_assist/collab/hydrate.py tests/proposal_assist/collab/test_collab_hydrate_mutex_order.py
uv run ruff format --check src/call_manager/proposal_assist/collab/hydrate.py tests/proposal_assist/collab/test_collab_hydrate_mutex_order.py
git add src/call_manager/proposal_assist/collab/hydrate.py tests/proposal_assist/collab/test_collab_hydrate_mutex_order.py
git commit -m "fix(collab): hydrate takes the commit mutex before reading the log, not only before seeding"
```

---

### Task 4: a stale generation on connect closes 1013, not 1008

**Files:**
- Modify: `src/call_manager/proposal_assist/web/collab_ws.py:145-155`
- Test: `tests/proposal_assist/collab/test_collab_ws_close_codes.py` (create)

**Interfaces:**
- Consumes: `_WS_TRY_AGAIN_LATER = 1013` (`collab_ws.py:54`) — already defined.
- Produces: no signature change.

**Why (spec §5.1), measured:** `frontend/src/collab/collabProvider.ts:226-237` reads

```ts
if (ev.code === 1008 || ev.code === 1009) { this.setStatus("closed-policy"); return; }
this.setStatus("offline");
this.scheduleReconnect();
```

**1008 is terminal** — it returns before `scheduleReconnect`, and nothing retries until the component remounts. Today `WorkspaceGenerationStale` on connect closes 1008 (`collab_ws.py:154`), which is right for a *deleted* workspace and wrong for a *switched* one: after a switch, the artifact still exists and the client should come straight back. 1013 is what the codebase already uses for exactly that shape (`collab_ws.py:172-187`).

`WorkspaceRetired` and `WorkspaceDeleted` keep 1008. Only the generation-stale arm changes.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/collab/test_collab_ws_close_codes.py`:

```python
"""A close code is a protocol decision, not a detail: the client retries every
code EXCEPT 1008/1009 (frontend/src/collab/collabProvider.ts:234). A workspace
that merely moved generation must not be closed with the code that means stop."""

import inspect

from call_manager.proposal_assist.web import collab_ws


def test_generation_stale_is_not_grouped_with_retired_and_deleted():
    # The three exceptions had ONE handler; a switch needs the stale arm to close
    # 1013 while the other two keep 1008.
    src = inspect.getsource(collab_ws)
    assert "except WorkspaceGenerationStale:" in src, (
        "WorkspaceGenerationStale must have its own arm so it can close 1013"
    )


def test_the_stale_arm_closes_with_try_again_later():
    src = inspect.getsource(collab_ws)
    arm = src.split("except WorkspaceGenerationStale:")[1].split("except ")[0]
    assert "_WS_TRY_AGAIN_LATER" in arm, "the stale arm must close 1013"
    assert "_WS_POLICY_VIOLATION" not in arm, "1008 is terminal on the client"


def test_retired_and_deleted_still_close_with_policy_violation():
    src = inspect.getsource(collab_ws)
    arm = src.split("except (WorkspaceRetired, WorkspaceDeleted):")[1].split("except ")[0]
    assert "_WS_POLICY_VIOLATION" in arm


def test_the_codes_are_the_rfc_values():
    assert collab_ws._WS_TRY_AGAIN_LATER == 1013
    assert collab_ws._WS_POLICY_VIOLATION == 1008
```

**On source-inspection tests:** these read the module text because driving a real
websocket upgrade to the stale-generation branch needs the full app plus a race.
They are a deliberate floor, not the whole gate — task 7 asserts the observable
behaviour end to end through `prepare_branch_switch`. Keep both.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/proposal_assist/collab/test_collab_ws_close_codes.py -q`
Expected: `2 failed, 2 passed` — the first two fail because today the three exceptions share one `except (WorkspaceRetired, WorkspaceDeleted, WorkspaceGenerationStale):` arm.

- [ ] **Step 3: Split the arm**

In `src/call_manager/proposal_assist/web/collab_ws.py`, replace the combined arm at lines 145-155 with:

```python
    except (WorkspaceRetired, WorkspaceDeleted):
        # The workspace was retired (soft-deleted) between the membership check and
        # room acquisition — or during cold-start hydration, where load_or_hydrate's
        # commit-mutex-at-generation guard raises WorkspaceDeleted. accept() already
        # happened, so a raise would surface as 1011/1006; close with 1008 (policy)
        # and return WITHOUT entering the try below (no client registered, nothing
        # to release). 1008 is TERMINAL on the client
        # (frontend/src/collab/collabProvider.ts:234) and that is correct here:
        # the artifact is gone, so coming back is pointless.
        with contextlib.suppress(Exception):
            await websocket.close(code=_WS_POLICY_VIOLATION)
        return
    except WorkspaceGenerationStale:
        # The workspace MOVED generation — a branch switch, or a delete-and-restore
        # — between this connect's authorization stamp and the mutex. Since spec
        # §4.4 this is reachable on every reconnect, not only a cold seed, because
        # load_or_hydrate now takes the mutex before reading the log.
        #
        # This must NOT be 1008. The artifact still exists and the client SHOULD
        # come straight back: its next connect is stamped at the new generation,
        # blocks on nothing, and hydrates the branch that is now current. 1008
        # would leave every editor dead until a page reload, which is what a
        # branch switch would look like to everyone except the person switching.
        with contextlib.suppress(Exception):
            await websocket.close(code=_WS_TRY_AGAIN_LATER)
        return
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/proposal_assist/collab/test_collab_ws_close_codes.py -q`
Expected: `4 passed`.

- [ ] **Step 5: Prove the gate bites**

Change the stale arm's code back to `_WS_POLICY_VIOLATION` and re-run.
Expected: `test_the_stale_arm_closes_with_try_again_later` FAILS on `"1008 is terminal on the client"`. Restore and re-run — `4 passed`.

- [ ] **Step 6: Run the collab suite and commit**

```bash
uv run pytest tests/proposal_assist/collab -q -n 8 --dist loadfile
uv run ruff check src/call_manager/proposal_assist/web/collab_ws.py tests/proposal_assist/collab/test_collab_ws_close_codes.py
uv run ruff format --check src/call_manager/proposal_assist/web/collab_ws.py tests/proposal_assist/collab/test_collab_ws_close_codes.py
git add src/call_manager/proposal_assist/web/collab_ws.py tests/proposal_assist/collab/test_collab_ws_close_codes.py
git commit -m "fix(collab): a stale generation closes 1013, not the terminal 1008"
```

Expected from the collab suite: all pass. If an existing test asserts 1008 on the stale path, that test encodes the old contract — update it and say so in the commit body.

---

### Task 5: the switch-specific room teardown

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/room.py` (`_retire_room` gains a parameter; a new `prepare_branch_switch` beside `retire_workspace` at line 670)
- Test: `tests/proposal_assist/collab/test_collab_branch_teardown.py` (create)

**Interfaces:**
- Consumes: `_set_evicted_under_lock` (`room.py:781`), `_RETIRE_LOCK_WAIT_SECONDS`, `_RETIRE_OPS_DRAIN_SECONDS`.
- Produces:
  - `RoomManager.prepare_branch_switch(workspace_id: uuid.UUID) -> BranchSwitchTeardown`
  - `@dataclass(frozen=True) class BranchSwitchTeardown: closed: int; locked: bool` — `locked` is False if **either** room's `room.lock` could not be obtained inside the budget.

**Why this is not `evict` and not `_retire_room` (spec §4.3):**
- `RoomManager.evict` (`room.py:656-668`) sets `room.evicted = True` holding only the *manager* lock. It never takes `room.lock`, so a persist already inside that lock commits after evict returns.
- `_retire_room` does take `room.lock` (via `_set_evicted_under_lock`, `room.py:781-786`) — but on timeout it sets the flag unsynchronized and **proceeds** (`room.py:744-753`), and its own comment names the cost: *"the straggler's own commit remains the documented residual."* Harmless when the workspace is being deleted. Corrupting for a switch, where that straggler rides into the target branch.

**The rule the switch needs: report the timeout, but always finish the teardown.** The teardown pops the room first (`room.py:726-730`), so a teardown that returns early on timeout leaves a room that is out of the registry, un-tombstoned, and still writable — its clients hold the object directly (`collab_ws.py:144`), a retry cannot find it, and its next keystroke lands after the retry's truncate. So `prepare_branch_switch` closes, tombstones and drains **unconditionally**, and returns `locked=False`. The *caller* aborts the switch.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/collab/test_collab_branch_teardown.py`:

```python
"""prepare_branch_switch: the teardown a branch switch needs.

Two properties, and they pull in opposite directions — which is why both are
tested. It must REPORT that it could not linearize (so the switch aborts), and
it must FINISH tearing down anyway (so no writable orphan survives the abort)."""

import asyncio
import uuid

import pytest

from call_manager.proposal_assist.collab.room import MANAGER, ClientState
from tests.proposal_assist.collab.test_collab_room import FakeClient


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _open(key):
    async def _load():
        return []
    client = FakeClient()
    room = await MANAGER.acquire(
        key, load=_load, client=client, state=ClientState(user_id=1, can_write=True)
    )
    return room, client


@pytest.mark.anyio
async def test_tears_down_both_artifact_rooms():
    wid = uuid.uuid4()
    draft, dc = await _open((wid, "draft"))
    spec, sc = await _open((wid, "spec"))

    res = await MANAGER.prepare_branch_switch(wid)

    assert res.locked is True
    assert res.closed == 2
    assert draft.evicted is True and spec.evicted is True
    assert MANAGER._rooms.get((wid, "draft")) is None
    assert MANAGER._rooms.get((wid, "spec")) is None


@pytest.mark.anyio
async def test_closes_with_1013_so_the_client_reconnects():
    # 1008 is TERMINAL on the client (collabProvider.ts:234). A switch that used
    # it would leave every other editor dead until a page reload.
    wid = uuid.uuid4()
    _, client = await _open((wid, "draft"))
    await MANAGER.prepare_branch_switch(wid)
    assert client.closed == 1013


@pytest.mark.anyio
async def test_reports_locked_False_when_room_lock_is_held():
    wid = uuid.uuid4()
    room, _ = await _open((wid, "draft"))

    async with room.lock:                      # stand in for a persist in flight
        res = await asyncio.wait_for(MANAGER.prepare_branch_switch(wid), timeout=10)

    assert res.locked is False                 # the switch must abort on this


@pytest.mark.anyio
async def test_still_FINISHES_the_teardown_when_the_lock_times_out():
    # THE ORPHAN TEST. The teardown pops the room before it reaches for the lock,
    # so a teardown that gives up on timeout leaves a room that is out of the
    # registry, un-tombstoned and still writable — held directly by its clients
    # (collab_ws.py:144). A retry tears down a DIFFERENT room, truncates, and the
    # orphan's next keystroke lands afterwards.
    wid = uuid.uuid4()
    room, client = await _open((wid, "draft"))

    async with room.lock:
        res = await asyncio.wait_for(MANAGER.prepare_branch_switch(wid), timeout=10)

    assert res.locked is False
    assert MANAGER._rooms.get((wid, "draft")) is None   # popped
    assert room.evicted is True                          # AND tombstoned
    assert client.closed == 1013                         # AND socket closed


@pytest.mark.anyio
async def test_is_idempotent_on_a_workspace_with_no_rooms():
    res = await MANAGER.prepare_branch_switch(uuid.uuid4())
    assert res.locked is True and res.closed == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/proposal_assist/collab/test_collab_branch_teardown.py -q`
Expected: collection succeeds, all five FAIL with `AttributeError: 'RoomManager' object has no attribute 'prepare_branch_switch'`.

- [ ] **Step 3: Give `_retire_room` a close code and a lock-result**

In `src/call_manager/proposal_assist/collab/room.py`, change `_retire_room`'s signature and its two behavioural points. Replace the `try/except TimeoutError` block at lines 744-753 with:

```python
        locked = True
        try:
            await asyncio.wait_for(
                _set_evicted_under_lock(room), timeout=_RETIRE_LOCK_WAIT_SECONDS
            )
        except TimeoutError:
            locked = False
            room.evicted = True   # unsynchronized fallback; still stops LATER frames
            logger.warning(
                "collab.retire_lock_wait_timeout",
                extra={"event": "collab_retire_lock_wait_timeout", "room": str(key)},
            )
```

and change the signature and the close code to:

```python
    async def _retire_room(
        self, workspace_id: uuid.UUID, artifact: str, *, close_code: int = _WS_POLICY_VIOLATION
    ) -> tuple[int, bool]:
        """Tear down ONE artifact room. Returns (sockets closed, got_room_lock).

        `close_code` is a protocol decision the CALLER owns, because the same
        teardown serves two situations the client must distinguish:
        retirement closes 1008 (terminal — the workspace is gone, retrying is
        pointless) and a branch switch closes 1013 (come straight back — the
        artifact still exists on another branch). 1008 is terminal on the client
        (frontend/src/collab/collabProvider.ts:234).

        `got_room_lock` is False when the tombstone had to be set without
        `room.lock`. Retirement ignores it — the straggler's commit lands in a
        log nobody will read. A branch switch MUST NOT ignore it: that commit
        would ride into the target branch. Either way this function finishes the
        teardown; it never returns leaving a popped room live.
        """
```

Then, inside, use `close_code` at the close call (replacing `_WS_POLICY_VIOLATION` at line 763) and return `(closed, locked)`. Update `retire_workspace`'s accumulation (`room.py:706-719`) to unpack the tuple and keep summing only `closed` — retirement's contract is unchanged.

- [ ] **Step 4: Add `prepare_branch_switch`**

Beside `retire_workspace`, add:

```python
@dataclass(frozen=True)
class BranchSwitchTeardown:
    """What a branch-switch teardown achieved. `locked` False means at least one
    room was tombstoned WITHOUT room.lock, so a client persist may still be in
    flight — the switch must abort. The teardown itself always completed."""

    closed: int
    locked: bool
```

(put the dataclass at module level, near `RoomKey`), and on `RoomManager`:

```python
    async def prepare_branch_switch(self, workspace_id: uuid.UUID) -> BranchSwitchTeardown:
        """Tear both artifact rooms down for a branch switch (spec §4.2 step 4).

        Differs from `retire_workspace` in exactly two ways, both deliberate:
        it closes 1013 rather than 1008, and it REPORTS whether it linearized
        against in-flight client persists instead of swallowing the timeout.

        It does NOT abort on a lock timeout. The teardown pops each room before
        it reaches for `room.lock`, so returning early would leave a room out of
        the registry, un-tombstoned, and still writable through the reference its
        clients already hold — a retry could not find it, and its next keystroke
        would land after that retry truncated the log. Abort the SWITCH, never
        the teardown: this finishes, and the caller acts on `locked`.

        Never checkpoints. The switch checkpoints separately, from the frozen
        LOG, after this returns.
        """
        results = await asyncio.gather(
            *(
                self._retire_room(workspace_id, artifact, close_code=_WS_TRY_AGAIN_LATER)
                for artifact in ("draft", "spec")
            ),
            return_exceptions=True,
        )
        closed = 0
        locked = True
        for artifact, result in zip(("draft", "spec"), results, strict=True):
            if isinstance(result, BaseException):
                # A teardown that RAISED tore down an unknown amount. Treat it as
                # not linearized so the switch aborts — the conservative reading.
                locked = False
                logger.warning(
                    "collab.branch_switch_teardown_failed",
                    extra={
                        "event": "collab_branch_switch_teardown_failed",
                        "room": str((workspace_id, artifact)),
                    },
                    exc_info=result,
                )
                continue
            n, got_lock = result
            closed += n
            locked = locked and got_lock
        return BranchSwitchTeardown(closed=closed, locked=locked)
```

Add `_WS_TRY_AGAIN_LATER = 1013` beside the other codes at `room.py:40-44`, and `from dataclasses import dataclass` if it is not already imported.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/proposal_assist/collab/test_collab_branch_teardown.py -q`
Expected: `5 passed`.

- [ ] **Step 6: Prove the orphan test bites**

Make the timeout path return early — insert `return closed, False` immediately after the `locked = False` assignment in `_retire_room`, before the close loop.
Expected: `test_still_FINISHES_the_teardown_when_the_lock_times_out` FAILS on `assert room.evicted is True` or `assert client.closed == 1013`.
Restore and re-run — `5 passed`. **A green suite is not evidence for this task; this step is.**

- [ ] **Step 7: Prove retirement still closes 1008**

Run: `uv run pytest tests/proposal_assist/test_drafting_retire_tombstone.py tests/proposal_assist/test_workspaces_delete_route.py -q`
Expected: all pass, unchanged. If one asserts a close code, confirm it still reads 1008.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check src/call_manager/proposal_assist/collab/room.py tests/proposal_assist/collab/test_collab_branch_teardown.py
uv run ruff format --check src/call_manager/proposal_assist/collab/room.py tests/proposal_assist/collab/test_collab_branch_teardown.py
git add src/call_manager/proposal_assist/collab/room.py tests/proposal_assist/collab/test_collab_branch_teardown.py
git commit -m "feat(collab): prepare_branch_switch — teardown that reports the lock and never orphans a room"
```

---

### Task 6: `claim_turn_job` takes the shared lease

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/processor.py:140-174`
- Test: `tests/proposal_assist/test_turn_claim_lease.py` (create)

**Interfaces:**
- Consumes: `acquire_shared_lease` (`fencing.py:174`).
- Produces: no signature change to `claim_turn_job`.

**Why (spec §6.3):** the switch refuses while a drafting turn is `running`, but `claim_turn_job` flips `queued → running` in a single UPDATE that takes **no mutex**, so a turn can start immediately after the check. Correctness is already saved by the fence — `claimed_at_generation` is captured in the same statement, so a turn that claimed before the switch's bump is stale for life and every fenced write it attempts raises. But it dies **silently, minutes into model work**, which is the exact loss the refusal exists to prevent.

The lease is what turns "I checked" into "I waited, then checked": shared locks do not conflict with each other, so concurrent claims stay concurrent, while the switch's exclusive mutex either waits for an in-flight claim or wins and leaves the claim's own generation predicate to fail.

**Do not upgrade.** `acquire_shared_lease`'s docstring is explicit: an operation holding the lease must never go on to call `acquire_commit_mutex*` for the same workspace. `claim_turn_job` does not, and must not start.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_turn_claim_lease.py`:

```python
"""claim_turn_job must hold the shared lease, so a branch switch's exclusive
mutex linearizes against it rather than racing it (spec §6.3)."""

import inspect

from call_manager.proposal_assist.turns import processor


def test_claim_turn_job_takes_the_shared_lease():
    src = inspect.getsource(processor.claim_turn_job)
    assert "acquire_shared_lease" in src, (
        "without the lease the switch's running-turn refusal is probabilistic"
    )


def test_the_lease_is_taken_BEFORE_the_claim_update():
    src = inspect.getsource(processor.claim_turn_job)
    assert src.index("acquire_shared_lease") < src.index("update(TurnJob)"), (
        "a lease taken after the write guards nothing"
    )


def test_claim_turn_job_never_upgrades_to_the_exclusive_mutex():
    # The one deadlock shape this protocol has: two holders of shared, each
    # waiting to upgrade. fencing.acquire_shared_lease's docstring forbids it.
    src = inspect.getsource(processor.claim_turn_job)
    assert "acquire_commit_mutex" not in src
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/proposal_assist/test_turn_claim_lease.py -q`
Expected: `2 failed, 1 passed`.

- [ ] **Step 3: Take the lease**

In `claim_turn_job`, inside the `with session_factory() as s:` block, before the `s.execute(update(TurnJob)...)`:

```python
    with session_factory() as s:
        # Spec §6.3. A branch switch refuses while a turn is running, but this
        # UPDATE used to take no lock at all — so a turn could start inside the
        # switch's own window and then be killed silently by the generation
        # fence, minutes into model work. The SHARED lease makes the switch's
        # exclusive mutex either wait for this claim or win and leave the
        # correlated generation subquery below to capture the post-switch value.
        #
        # NEVER upgrade this to acquire_commit_mutex* for the same workspace:
        # two holders of shared each waiting to upgrade is the one deadlock shape
        # this protocol has (fencing.acquire_shared_lease's docstring).
        acquire_shared_lease(s, _workspace_id_of(s, turn_job_id))
        row = s.execute(...)
```

`acquire_shared_lease` needs the workspace id, which `claim_turn_job` currently only learns from `RETURNING`. Add the lookup above it:

```python
def _workspace_id_of(session: Session, turn_job_id: uuid.UUID) -> uuid.UUID:
    """The workspace a turn belongs to, read before the claim so the shared lease
    can be taken on it. A separate SELECT is correct here: the lease must be held
    BEFORE the claim, and the claim's own RETURNING is too late."""
    return session.execute(
        select(TurnJob.workspace_id).where(TurnJob.id == turn_job_id)
    ).scalar_one()
```

Import `acquire_shared_lease` from `call_manager.proposal_assist.fencing` and `select` from `sqlalchemy` if not already imported.

**If the row is gone** (`scalar_one` raises `NoResultFound`), let it propagate: `claim_turn_job` already returns `None` only for a job that was not `queued`, and a job that does not exist at all is a different, louder condition.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_turn_claim_lease.py -q`
Expected: `3 passed`.

- [ ] **Step 5: Run the turn pipeline suite — this is the risky part of the task**

Run: `uv run pytest tests/proposal_assist -q -n 8 --dist loadfile -k "turn or drain or processor or job"`
Expected: all pass. A new lock in the claim path is exactly the change that can deadlock or slow a drain. **If anything hangs rather than fails, record the call path as a plan defect and escalate — do not raise a timeout to make it pass.**

- [ ] **Step 6: Full suite, lint, commit**

```bash
uv run pytest -n 16 --dist loadfile
uv run ruff check src/call_manager/proposal_assist/turns/processor.py tests/proposal_assist/test_turn_claim_lease.py
uv run ruff format --check src/call_manager/proposal_assist/turns/processor.py tests/proposal_assist/test_turn_claim_lease.py
git add src/call_manager/proposal_assist/turns/processor.py tests/proposal_assist/test_turn_claim_lease.py
git commit -m "fix(turns): claim_turn_job takes the shared lease so a branch switch can linearize against it"
```

---

### Task 7: the switch sequence

**Files:**
- Create: `src/call_manager/proposal_assist/branches.py`
- Modify: `src/call_manager/proposal_assist/collab/store.py:146-149` and `src/call_manager/proposal_assist/collab/state.py:99-103` (docstrings only — both say "Kill-switch handover ONLY" and now have a second legitimate caller)
- Test: `tests/proposal_assist/test_branch_switch.py` (create)

**Interfaces:**
- Consumes: everything from tasks 1–6, plus `checkpoint_artifact`, `store.truncate_log`, `state.reset_checkpoint`, `bump_lifecycle_generation`, `acquire_commit_mutex_at_generation`, `workspace_repo_dir`.
- Produces:
  - `switch_branch(session, *, stamp, workspace_id, target, workspaces_dir, teardown) -> None`
  - `class BranchError(RuntimeError)`, `class BranchNotFound(BranchError)`, `class BranchBusy(BranchError)`, `class BranchInUse(BranchError)`, `class BranchNameInvalid(BranchError)`
  - `TeardownFn = Callable[[uuid.UUID], BranchSwitchTeardown]`

**The `teardown` parameter is the design decision of this task.** The room manager is asyncio; a switch runs in a sync request thread holding a database transaction. Rather than reach for the event loop from inside the transaction, `switch_branch` takes a **sync callable** and the route supplies the bridge (task 9). That keeps the whole sequence unit-testable with a fake teardown, and keeps the one cross-thread wait at the edge where its timeout is visible.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_branch_switch.py`:

```python
"""The switch sequence, spec §4.2. Ordinary behaviour first, then the races and
failures — which are the criteria that decide whether this is safe. Several of
these assert by BREAKING the mechanism first; those steps are in the plan."""

import subprocess
import uuid

import pytest
from sqlalchemy import select

from call_manager.db.models import Workspace
from call_manager.proposal_assist.branches import BranchBusy, BranchNotFound, switch_branch
from call_manager.proposal_assist.citations.gitio import (
    create_branch,
    current_branch,
    current_head,
)
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.room import BranchSwitchTeardown
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import init_workspace_repo, make_workspace, stamp_for

OK = BranchSwitchTeardown(closed=0, locked=True)
NOT_LOCKED = BranchSwitchTeardown(closed=1, locked=False)


def _ws_with_repo(session_factory, tmp_path, *, text="# Aims\n\nold\n"):
    with session_factory() as s:
        ws = make_workspace(s, title="w")
        wid = ws.id
        s.commit()
    repo = workspace_repo_dir(tmp_path, wid)
    init_workspace_repo(repo, text)
    with session_factory() as s:
        s.execute(
            Workspace.__table__.update().where(Workspace.id == wid).values(current_branch="main")
        )
        s.commit()
    return wid, repo


def _rows(session_factory, wid, artifact="draft"):
    with session_factory() as s:
        return len(store.load_updates(s, workspace_id=wid, artifact=artifact))


def test_switch_moves_the_column_and_HEAD(pg_session_factory, tmp_path):
    wid, repo = _ws_with_repo(pg_session_factory, tmp_path)
    create_branch(repo, "short-version")

    with pg_session_factory() as s:
        switch_branch(
            s, stamp=stamp_for(s, wid), workspace_id=wid, target="short-version",
            workspaces_dir=tmp_path, teardown=lambda _wid: OK,
        )
        s.commit()

    with pg_session_factory() as s:
        assert s.execute(
            select(Workspace.current_branch).where(Workspace.id == wid)
        ).scalar_one() == "short-version"
    assert current_branch(repo) == "short-version"


def test_switch_checkpoints_live_edits_to_the_branch_being_LEFT(pg_session_factory, tmp_path):
    wid, repo = _ws_with_repo(pg_session_factory, tmp_path)
    create_branch(repo, "short-version")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None,
                            update=_update_bytes("# Aims\n\nNEW LIVE TEXT\n"))
        s.commit()

    with pg_session_factory() as s:
        switch_branch(
            s, stamp=stamp_for(s, wid), workspace_id=wid, target="short-version",
            workspaces_dir=tmp_path, teardown=lambda _wid: OK,
        )
        s.commit()

    # The live text is on main, not on short-version.
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    assert "NEW LIVE TEXT" in (repo / "proposal.md").read_text()


def test_switch_truncates_the_log(pg_session_factory, tmp_path):
    wid, repo = _ws_with_repo(pg_session_factory, tmp_path)
    create_branch(repo, "short-version")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None,
                            update=_update_bytes("# Aims\n\nlive\n"))
        s.commit()
    assert _rows(pg_session_factory, wid) == 1

    with pg_session_factory() as s:
        switch_branch(s, stamp=stamp_for(s, wid), workspace_id=wid, target="short-version",
                      workspaces_dir=tmp_path, teardown=lambda _wid: OK)
        s.commit()

    assert _rows(pg_session_factory, wid) == 0


def test_switch_bumps_the_generation(pg_session_factory, tmp_path):
    wid, repo = _ws_with_repo(pg_session_factory, tmp_path)
    create_branch(repo, "short-version")
    with pg_session_factory() as s:
        before = s.get(Workspace, wid).lifecycle_generation
        switch_branch(s, stamp=stamp_for(s, wid), workspace_id=wid, target="short-version",
                      workspaces_dir=tmp_path, teardown=lambda _wid: OK)
        s.commit()
    with pg_session_factory() as s:
        assert s.get(Workspace, wid).lifecycle_generation == before + 1


def test_switch_to_a_branch_that_does_not_exist_raises(pg_session_factory, tmp_path):
    wid, _ = _ws_with_repo(pg_session_factory, tmp_path)
    with pg_session_factory() as s, pytest.raises(BranchNotFound):
        switch_branch(s, stamp=stamp_for(s, wid), workspace_id=wid, target="ghost",
                      workspaces_dir=tmp_path, teardown=lambda _wid: OK)


def test_switch_to_the_CURRENT_branch_is_a_no_op(pg_session_factory, tmp_path):
    wid, repo = _ws_with_repo(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None,
                            update=_update_bytes("# Aims\n\nlive\n"))
        s.commit()
    called = []

    with pg_session_factory() as s:
        switch_branch(s, stamp=stamp_for(s, wid), workspace_id=wid, target="main",
                      workspaces_dir=tmp_path, teardown=lambda w: called.append(w) or OK)
        s.commit()

    # Nobody is evicted and no work is thrown away for a switch to where you are.
    assert called == []
    assert _rows(pg_session_factory, wid) == 1


def test_a_teardown_that_could_not_LOCK_aborts_the_switch(pg_session_factory, tmp_path):
    # Spec §4.3: locked=False means a client persist may be in flight, and that
    # persist would ride into the target branch. Refusing is free — the user retries.
    wid, repo = _ws_with_repo(pg_session_factory, tmp_path)
    create_branch(repo, "short-version")

    with pg_session_factory() as s, pytest.raises(BranchBusy):
        switch_branch(s, stamp=stamp_for(s, wid), workspace_id=wid, target="short-version",
                      workspaces_dir=tmp_path, teardown=lambda _wid: NOT_LOCKED)
        s.commit()

    with pg_session_factory() as s:
        assert s.execute(
            select(Workspace.current_branch).where(Workspace.id == wid)
        ).scalar_one() == "main"
    assert current_branch(repo) == "main"


def test_HEAD_is_reconciled_before_the_checkpoint_reads_it(pg_session_factory, tmp_path):
    # Spec §4.2 step 3. The state a crash between steps 9 and 10 leaves: HEAD on
    # the target, the column still naming the source. Without the reconcile, step
    # 5 commits the SOURCE branch's live log onto the TARGET branch.
    wid, repo = _ws_with_repo(pg_session_factory, tmp_path)
    create_branch(repo, "short-version")
    create_branch(repo, "third")
    subprocess.run(["git", "checkout", "-q", "short-version"], cwd=repo, check=True)
    # The column still says main — the crash state.
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None,
                            update=_update_bytes("# Aims\n\nMAIN LIVE TEXT\n"))
        s.commit()

    with pg_session_factory() as s:
        switch_branch(s, stamp=stamp_for(s, wid), workspace_id=wid, target="third",
                      workspaces_dir=tmp_path, teardown=lambda _wid: OK)
        s.commit()

    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    assert "MAIN LIVE TEXT" in (repo / "proposal.md").read_text()
    subprocess.run(["git", "checkout", "-q", "short-version"], cwd=repo, check=True)
    assert "MAIN LIVE TEXT" not in (repo / "proposal.md").read_text()


def test_a_failure_after_truncate_rolls_the_DATABASE_back(pg_session_factory, tmp_path, monkeypatch):
    # And leaves step 5's git commit in place, which is CORRECT: git is not in
    # the transaction, and that commit holds the source branch's own content on
    # the source branch. Spec §4.5.
    from call_manager.proposal_assist import branches as B

    wid, repo = _ws_with_repo(pg_session_factory, tmp_path)
    create_branch(repo, "short-version")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None,
                            update=_update_bytes("# Aims\n\nlive\n"))
        s.commit()
    head_before = current_head(repo)

    def _boom(*a, **k):
        raise RuntimeError("checkout failed")

    monkeypatch.setattr(B, "checkout_branch", _boom)

    with pg_session_factory() as s:
        with pytest.raises(RuntimeError):
            switch_branch(s, stamp=stamp_for(s, wid), workspace_id=wid,
                          target="short-version", workspaces_dir=tmp_path,
                          teardown=lambda _wid: OK)
        s.rollback()

    assert _rows(pg_session_factory, wid) == 1          # log intact
    with pg_session_factory() as s:
        assert s.execute(
            select(Workspace.current_branch).where(Workspace.id == wid)
        ).scalar_one() == "main"                        # column intact
    assert current_head(repo) != head_before            # checkpoint commit SURVIVED
    assert current_branch(repo) == "main"               # on the source branch
```

`_update_bytes` is a helper this file needs — add it near the top:

```python
def _update_bytes(markdown: str) -> bytes:
    """A Yjs update that makes a room's document equal *markdown*, the way the
    collab tests build one (see tests/proposal_assist/collab/test_collab_room.py)."""
    from call_manager.proposal_assist.collab.canonical import markdown_to_pm_or_empty
    from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc

    return pm_json_to_ydoc(markdown_to_pm_or_empty(markdown)).get_update()
```

`state.enable_collab(session, *, workspace_id, artifact)` is real and is at `state.py:65`. The collab log is only read back by hydration when collab is enabled for that artifact, which is why these fixtures enable it.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/proposal_assist/test_branch_switch.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'call_manager.proposal_assist.branches'`.

- [ ] **Step 3: Write the module**

Create `src/call_manager/proposal_assist/branches.py`:

```python
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

from sqlalchemy import update as sql_update
from sqlalchemy.orm import Session

from call_manager.db.models import Workspace
from call_manager.proposal_assist.citations.gitio import (
    branch_exists,
    checkout_branch,
    create_branch as git_create_branch,
    current_branch,
    delete_branch as git_delete_branch,
    is_valid_branch_name,
    list_branches,
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
    if has_running_turn(session, workspace_id=workspace_id):
        raise BranchBusy("a drafting turn is running")

    # ---- 2. Take the commit mutex at the caller's generation -----------------
    # pg_advisory_xact_lock is re-entrant within one transaction, so
    # checkpoint_artifact re-acquiring it below is free rather than a deadlock.
    acquire_commit_mutex_at_generation(
        session, workspace_id, generation=stamp.generation, timeout_seconds=10
    )

    # The authoritative re-check, now that nothing else can commit a claim
    # underneath us (task 6 gave claim_turn_job the shared lease).
    if has_running_turn(session, workspace_id=workspace_id):
        raise BranchBusy("a drafting turn is running")

    # ---- 3. Reconcile HEAD to the column BEFORE any other git read or write --
    # §4.1 makes the DATABASE the source of truth. A previous switch that crashed
    # between steps 9 and 10 leaves HEAD on the target while the column still
    # names the source; step 5 below reads whatever HEAD is (checkpoint.py:110)
    # and would commit the source's live log onto the target branch. Reconciling
    # first makes that crash recoverable instead of silently wrong.
    recorded = _reconcile_head(
        session, workspace_id=workspace_id, workspaces_dir=workspaces_dir
    )

    if target == recorded:
        return                      # switching to where you are throws nothing away
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
```

**There is no existing single-workspace "is a turn running" helper** — measured
2026-08-29: nothing in `src/call_manager/proposal_assist/` defines one.
`drain.py:62` selects running turns across all workspaces, which is a different
question. So define it privately in `branches.py`, which is its only caller, and
drop `from call_manager.proposal_assist.turns.drain import has_running_turn` from
the imports above:

```python
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
```

Import `TurnJob` from `call_manager.db.models` and `select` from `sqlalchemy`, and
call it as `_has_running_turn(...)` at both check sites.

- [ ] **Step 4: Update the two "handover ONLY" docstrings**

`store.truncate_log` (`store.py:146-149`) and `state.reset_checkpoint` (`state.py:99-103`) both say their caller is the kill-switch handover. They now have a second one. Replace "Kill-switch handover ONLY" in each with:

```
Two callers, both of which MUST have checkpointed the full log to git in the
SAME transaction: the kill-switch handover (handover.py) and a branch switch
(branches.py §4.2 step 6).
```

Leaving these stale is how the next reader concludes a branch switch is doing something forbidden.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_branch_switch.py -q`
Expected: `9 passed`.

- [ ] **Step 6: Prove the reconcile is what saves the crash case**

Delete the `if current_branch(repo) != recorded: checkout_branch(...)` block and re-run.
Expected: `test_HEAD_is_reconciled_before_the_checkpoint_reads_it` FAILS — `"MAIN LIVE TEXT" not in` main's `proposal.md`, because the checkpoint landed on `short-version`. Restore and re-run — `9 passed`.

- [ ] **Step 7: Prove the abort is what saves the in-flight persist**

Change `if not result.locked: raise BranchBusy(...)` to `pass` and re-run.
Expected: `test_a_teardown_that_could_not_LOCK_aborts_the_switch` FAILS on `DID NOT RAISE`. Restore and re-run — `9 passed`.

- [ ] **Step 8: Prove the teardown really comes before the truncate**

Move the `teardown(workspace_id)` call to after the truncate loop and re-run the file plus `tests/proposal_assist/collab/test_collab_branch_teardown.py`.
Expected: this is the ordering the whole design turns on, and **no existing test may catch it** — if the suite stays green, that is a finding, not a pass. Record it, restore the order, and add this to the branch's review notes for task 10's end-to-end criterion.

- [ ] **Step 9: Full suite, lint, commit**

```bash
uv run pytest -n 16 --dist loadfile
uv run ruff check src/call_manager/proposal_assist/branches.py src/call_manager/proposal_assist/collab/store.py src/call_manager/proposal_assist/collab/state.py tests/proposal_assist/test_branch_switch.py
uv run ruff format --check src/call_manager/proposal_assist/branches.py tests/proposal_assist/test_branch_switch.py
git add -A
git commit -m "feat(branches): the switch sequence — teardown first, reconcile before checkpoint, abort on an unproven lock"
```

---

### Task 8: create and delete

**Files:**
- Modify: `src/call_manager/proposal_assist/branches.py` (append)
- Test: `tests/proposal_assist/test_branch_create_delete.py` (create)

**Interfaces:**
- Consumes: the errors and imports from task 7.
- Produces:
  - `create_workspace_branch(session, *, stamp, workspace_id, name, workspaces_dir) -> None`
  - `delete_workspace_branch(session, *, stamp, workspace_id, name, workspaces_dir) -> None`
  - `list_workspace_branches(session, *, workspace_id, workspaces_dir) -> tuple[list[str], str]` — `(names, current)`

**Why create still takes the mutex (spec §3.1):** it changes nothing anybody can see and needs no checkpoint or teardown. But **HEAD must not move under it** — a checkpoint or section save committing between reading HEAD and running `git branch` would silently found the branch on a commit the user did not choose.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_branch_create_delete.py`:

```python
"""Create and delete. Both take the mutex; both refuse loudly rather than guess."""

import subprocess

import pytest
from sqlalchemy import select

from call_manager.db.models import Workspace
from call_manager.proposal_assist.branches import (
    BranchExists,
    BranchInUse,
    BranchLast,
    BranchNameInvalid,
    BranchNotFound,
    create_workspace_branch,
    delete_workspace_branch,
    list_workspace_branches,
)
from call_manager.proposal_assist.citations.gitio import (
    branch_exists,
    current_branch,
    current_head,
)
from call_manager.proposal_assist.collab import store
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import init_workspace_repo, make_workspace, stamp_for


def _ws(session_factory, tmp_path, *, commit=True):
    with session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    repo = workspace_repo_dir(tmp_path, wid)
    if commit:
        init_workspace_repo(repo, "# Aims\n\nbody\n")
    else:
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=repo, check=True)
    return wid, repo


def test_create_makes_the_branch_without_moving_head(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    head_before = current_head(repo)
    with pg_session_factory() as s:
        create_workspace_branch(
            s, stamp=stamp_for(s, wid), workspace_id=wid, name="aims-ambitious",
            workspaces_dir=tmp_path,
        )
        s.commit()
    assert branch_exists(repo, "aims-ambitious")
    assert current_branch(repo) == "main"
    assert current_head(repo) == head_before


def test_create_does_not_disturb_the_log(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None, update=b"\x00\x00")
        s.commit()
    with pg_session_factory() as s:
        create_workspace_branch(s, stamp=stamp_for(s, wid), workspace_id=wid,
                                name="x", workspaces_dir=tmp_path)
        s.commit()
    with pg_session_factory() as s:
        assert len(store.load_updates(s, workspace_id=wid, artifact="draft")) == 1


@pytest.mark.parametrize("bad", ["a..b", "foo.lock", "-lead", "with space", ""])
def test_create_rejects_a_name_git_rejects(pg_session_factory, tmp_path, bad):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s, pytest.raises(BranchNameInvalid):
        create_workspace_branch(s, stamp=stamp_for(s, wid), workspace_id=wid,
                                name=bad, workspaces_dir=tmp_path)
    # and leaves no partially created ref
    assert sorted(list_workspace_branches(None, workspace_id=wid, workspaces_dir=tmp_path)[0]) == ["main"]


def test_create_refuses_a_duplicate(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        create_workspace_branch(s, stamp=stamp_for(s, wid), workspace_id=wid,
                                name="dup", workspaces_dir=tmp_path)
        s.commit()
    with pg_session_factory() as s, pytest.raises(BranchExists):
        create_workspace_branch(s, stamp=stamp_for(s, wid), workspace_id=wid,
                                name="dup", workspaces_dir=tmp_path)


def test_create_refuses_on_a_repo_with_no_commits(pg_session_factory, tmp_path):
    # A workspace still in setup. Measured: `git branch x HEAD` exits 128.
    wid, repo = _ws(pg_session_factory, tmp_path, commit=False)
    with pg_session_factory() as s, pytest.raises(BranchNotFound):
        create_workspace_branch(s, stamp=stamp_for(s, wid), workspace_id=wid,
                                name="x", workspaces_dir=tmp_path)


def test_delete_removes_a_non_current_branch(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        create_workspace_branch(s, stamp=stamp_for(s, wid), workspace_id=wid,
                                name="dead", workspaces_dir=tmp_path)
        s.commit()
    with pg_session_factory() as s:
        delete_workspace_branch(s, stamp=stamp_for(s, wid), workspace_id=wid,
                                name="dead", workspaces_dir=tmp_path)
        s.commit()
    assert branch_exists(repo, "dead") is False


def test_delete_refuses_the_CURRENT_branch(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        create_workspace_branch(s, stamp=stamp_for(s, wid), workspace_id=wid,
                                name="other", workspaces_dir=tmp_path)
        s.commit()
    with pg_session_factory() as s, pytest.raises(BranchInUse):
        delete_workspace_branch(s, stamp=stamp_for(s, wid), workspace_id=wid,
                                name="main", workspaces_dir=tmp_path)
    assert branch_exists(repo, "main") is True


def test_delete_refuses_the_LAST_branch(pg_session_factory, tmp_path):
    # A repo with no branch has a detached HEAD, and §4.1's reconciliation then
    # has nothing to reconcile to.
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s, pytest.raises((BranchInUse, BranchLast)):
        delete_workspace_branch(s, stamp=stamp_for(s, wid), workspace_id=wid,
                                name="main", workspaces_dir=tmp_path)
    assert branch_exists(repo, "main") is True


def test_delete_of_a_missing_branch_raises(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s, pytest.raises(BranchNotFound):
        delete_workspace_branch(s, stamp=stamp_for(s, wid), workspace_id=wid,
                                name="ghost", workspaces_dir=tmp_path)


def test_list_returns_the_names_and_the_current_one(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        create_workspace_branch(s, stamp=stamp_for(s, wid), workspace_id=wid,
                                name="b", workspaces_dir=tmp_path)
        s.commit()
    names, current = list_workspace_branches(None, workspace_id=wid, workspaces_dir=tmp_path)
    assert sorted(names) == ["b", "main"]
    assert current == "main"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/proposal_assist/test_branch_create_delete.py -q`
Expected: collection error — `ImportError: cannot import name 'create_workspace_branch'`.

- [ ] **Step 3: Implement**

Append to `src/call_manager/proposal_assist/branches.py`:

```python
def list_workspace_branches(
    session: Session | None, *, workspace_id: uuid.UUID, workspaces_dir: Path | str
) -> tuple[list[str], str]:
    """(every local branch, the checked-out one). Read-only, no mutex.

    `session` is accepted and unused so the three operations share a call shape;
    reading refs takes no database work and must not take a lock that a switch
    would then queue behind.
    """
    repo = workspace_repo_dir(workspaces_dir, workspace_id)
    return list_branches(repo), current_branch(repo)


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
    if branch_exists(repo, name):
        raise BranchExists(name)
    try:
        git_create_branch(repo, name)
    except Exception as exc:
        # The repo has no commits yet — a workspace still in setup. Measured:
        # `git branch <name> HEAD` exits 128, "not a valid object name: 'HEAD'".
        raise BranchNotFound(f"{name}: the workspace has no commits yet") from exc
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
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_branch_create_delete.py -q`
Expected: `14 passed` (5 of them from the parametrised name test).

- [ ] **Step 5: Prove the current-branch refusal is not git's**

Change `raise BranchInUse(name)` to `pass` and re-run.
Expected: `test_delete_refuses_the_CURRENT_branch` still FAILS, but now with `GitCommitError` rather than `BranchInUse` — proving git is the floor and the app's check is the message. Restore and re-run — `14 passed`.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src/call_manager/proposal_assist/branches.py tests/proposal_assist/test_branch_create_delete.py
uv run ruff format --check src/call_manager/proposal_assist/branches.py tests/proposal_assist/test_branch_create_delete.py
git add src/call_manager/proposal_assist/branches.py tests/proposal_assist/test_branch_create_delete.py
git commit -m "feat(branches): create and delete, with the refusals stated rather than left to git"
```

---

### Task 9: the API

**Files:**
- Create: `src/call_manager/proposal_assist/web/branches.py`
- Modify: `src/call_manager/main.py` (import + `include_router`, beside `versions_router` at lines 64-65 and 801)
- Test: `tests/proposal_assist/test_api_branches.py` (create)

**Interfaces:**
- Consumes: `branches.py` from tasks 7–8; `require_owner`, `require_member` (`web/_perms.py:18,29`); `_session_dep` (`web/_deps.py`); `get_current_user`.
- Produces: `GET /api/workspaces/{id}/branches`, `POST /api/workspaces/{id}/branches`, `POST /api/workspaces/{id}/branches/switch`, `DELETE /api/workspaces/{id}/branches/{name}`.

**This task owns the sync→async bridge.** `switch_branch` takes a sync `teardown` callable; the route builds it with `asyncio.run_coroutine_threadsafe(coro, request.app.state.app_loop)` and `fut.result(timeout=...)`, mirroring `lifecycle.py:145-182`. Bound it: this wait happens while holding the commit mutex, so an unbounded wait would pin a worker and the workspace together.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_api_branches.py`:

```python
"""Branch routes. Owner-only for mutation, and every refusal maps to a status the
UI can act on rather than a 500.

Fixture shapes, measured from tests/proposal_assist/conftest.py:
  * `client_authed_owner` yields the TestClient ALONE (not a tuple).
  * `current_user` is the logged-in user.
  * `_app` is the app; `_app.state.settings.workspaces_dir` is where repos live.
  * A non-owner 403 is built by giving the workspace to a DIFFERENT user — the
    pattern in test_api_annotations_dismiss.py:82-92.
"""

import pytest

from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import init_workspace_repo, make_user, make_workspace


def _seed(session, app, owner):
    """A workspace owned by *owner*, with a real repo on `main`."""
    ws = make_workspace(session, title="w", owner=owner)
    session.flush()
    init_workspace_repo(
        workspace_repo_dir(app.state.settings.workspaces_dir, ws.id), "# Aims\n\nbody\n"
    )
    return ws.id


def test_list_returns_branches_and_current(
    client_authed_owner, current_user, pg_session_factory, _app
):
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    r = client_authed_owner.get(f"/api/workspaces/{wid}/branches")
    assert r.status_code == 200
    assert r.json() == {"branches": ["main"], "current": "main"}


def test_create_then_list(client_authed_owner, current_user, pg_session_factory, _app):
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    assert client_authed_owner.post(
        f"/api/workspaces/{wid}/branches", json={"name": "v2"}
    ).status_code == 201
    assert sorted(
        client_authed_owner.get(f"/api/workspaces/{wid}/branches").json()["branches"]
    ) == ["main", "v2"]


def test_create_with_an_invalid_name_is_422_not_500(
    client_authed_owner, current_user, pg_session_factory, _app
):
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    assert client_authed_owner.post(
        f"/api/workspaces/{wid}/branches", json={"name": "a..b"}
    ).status_code == 422


def test_create_a_duplicate_is_409(client_authed_owner, current_user, pg_session_factory, _app):
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    client_authed_owner.post(f"/api/workspaces/{wid}/branches", json={"name": "v2"})
    assert client_authed_owner.post(
        f"/api/workspaces/{wid}/branches", json={"name": "v2"}
    ).status_code == 409


def test_switch_moves_the_workspace(client_authed_owner, current_user, pg_session_factory, _app):
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    client_authed_owner.post(f"/api/workspaces/{wid}/branches", json={"name": "v2"})
    r = client_authed_owner.post(f"/api/workspaces/{wid}/branches/switch", json={"name": "v2"})
    assert r.status_code == 200
    assert r.json()["current"] == "v2"


def test_switch_to_a_missing_branch_is_404(
    client_authed_owner, current_user, pg_session_factory, _app
):
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    assert client_authed_owner.post(
        f"/api/workspaces/{wid}/branches/switch", json={"name": "ghost"}
    ).status_code == 404


def test_delete_the_current_branch_is_409(
    client_authed_owner, current_user, pg_session_factory, _app
):
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    client_authed_owner.post(f"/api/workspaces/{wid}/branches", json={"name": "v2"})
    assert client_authed_owner.delete(
        f"/api/workspaces/{wid}/branches/main"
    ).status_code == 409


def test_delete_removes_it(client_authed_owner, current_user, pg_session_factory, _app):
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    client_authed_owner.post(f"/api/workspaces/{wid}/branches", json={"name": "v2"})
    assert client_authed_owner.delete(f"/api/workspaces/{wid}/branches/v2").status_code == 204
    assert client_authed_owner.get(
        f"/api/workspaces/{wid}/branches"
    ).json()["branches"] == ["main"]


@pytest.mark.parametrize(
    "verb,suffix,body",
    [
        ("post", "/branches", {"name": "x"}),
        ("post", "/branches/switch", {"name": "main"}),
        ("delete", "/branches/main", None),
    ],
)
def test_a_non_owner_cannot_mutate_branches(
    client_authed_owner, pg_session_factory, _app, verb, suffix, body
):
    # Spec §7: owner only for create, switch and delete. The workspace belongs to
    # somebody else; the logged-in user is not its owner.
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger-branch@ki.se")
        wid = _seed(s, _app, stranger)
        s.commit()
    call = getattr(client_authed_owner, verb)
    r = call(f"/api/workspaces/{wid}{suffix}", **({"json": body} if body is not None else {}))
    assert r.status_code == 403
```

**A member's read access is not asserted here.** `require_member` on the list route
is the same helper `versions.py:123` already uses, and building a genuine
non-owner *member* needs the membership row those tests seed. If the reviewer
wants that covered, model it on whichever `test_api_*.py` file already seeds a
member and say so — do not invent a second way to make one.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/proposal_assist/test_api_branches.py -q`
Expected: every test fails on the status code (no router is mounted, so FastAPI answers 404) — assertion failures, not collection errors. The three parametrised permission cases fail with `assert 404 == 403`.

- [ ] **Step 3: Write the router**

Create `src/call_manager/proposal_assist/web/branches.py`:

```python
"""Branch routes. Owner-only for anything that changes state; members may list.

This module owns the ONE cross-thread wait in the feature: the room teardown is
asyncio and a switch is a sync request holding the commit mutex, so the bridge
lives here — at the edge, where its timeout is visible — rather than inside
branches.switch_branch.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.proposal_assist.branches import (
    BranchBusy,
    BranchExists,
    BranchInUse,
    BranchLast,
    BranchNameInvalid,
    BranchNotFound,
    create_workspace_branch,
    delete_workspace_branch,
    list_workspace_branches,
    switch_branch,
)
from call_manager.proposal_assist.collab.room import MANAGER, BranchSwitchTeardown
from call_manager.proposal_assist.ports.stamp import stamp_for_workspace
from call_manager.proposal_assist.web._deps import _session_dep
from call_manager.proposal_assist.web._perms import require_member, require_owner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workspaces", tags=["branches"])

#: Bound on the cross-thread wait for the room teardown. This wait happens while
#: the switch transaction holds the commit mutex, so an unbounded one would pin a
#: worker and the workspace together. Comfortably above the room-lock and ops
#: drain budgets the teardown itself uses (room.py:46-53).
_TEARDOWN_BARRIER_SECONDS = 15.0


class BranchListOut(BaseModel):
    branches: list[str]
    current: str


class BranchIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


def _teardown_via_loop(request: Request):
    """Build the sync callable switch_branch expects.

    Mirrors lifecycle.signal_workspace_restore (lifecycle.py:145-182): schedule
    onto the app loop, wait bounded, and treat "could not schedule" and "timed
    out" as NOT LOCKED — the conservative reading, which makes the switch abort
    rather than proceed on an unproven teardown.
    """

    def _run(workspace_id: uuid.UUID) -> BranchSwitchTeardown:
        loop = getattr(request.app.state, "app_loop", None)
        if loop is None:
            logger.warning(
                "branches.bridge_unavailable",
                extra={"event": "branch_bridge_unavailable", "workspace_id": str(workspace_id)},
            )
            return BranchSwitchTeardown(closed=0, locked=False)
        coro = MANAGER.prepare_branch_switch(workspace_id)
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            coro.close()   # never-awaited coroutine -> close it before reporting
            logger.warning(
                "branches.teardown_schedule_failed",
                extra={"event": "branch_teardown_schedule_failed",
                       "workspace_id": str(workspace_id)},
                exc_info=True,
            )
            return BranchSwitchTeardown(closed=0, locked=False)
        try:
            return fut.result(timeout=_TEARDOWN_BARRIER_SECONDS)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "branches.teardown_barrier_timeout",
                extra={"event": "branch_teardown_barrier_timeout",
                       "workspace_id": str(workspace_id)},
            )
            return BranchSwitchTeardown(closed=0, locked=False)

    return _run


#: Every refusal this module can produce, mapped to a status the UI can act on.
#: Without this they reach main.py's generic handler as 500s, and "try again" is
#: indistinguishable from "you did something wrong".
_STATUS = {
    BranchNameInvalid: 422,
    BranchNotFound: 404,
    BranchExists: 409,
    BranchInUse: 409,
    BranchLast: 409,
    BranchBusy: 409,
}


def _raise_http(exc: Exception):
    for kind, code in _STATUS.items():
        if isinstance(exc, kind):
            raise HTTPException(status_code=code, detail=str(exc)) from exc
    raise exc


@router.get("/{workspace_id}/branches", response_model=BranchListOut)
def list_branches_route(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
):
    require_member(workspace_id=workspace_id, user=user, session=session)
    names, current = list_workspace_branches(
        None, workspace_id=workspace_id, workspaces_dir=request.app.state.settings.workspaces_dir
    )
    return BranchListOut(branches=names, current=current)


@router.post("/{workspace_id}/branches", status_code=201)
def create_branch_route(
    workspace_id: uuid.UUID,
    body: BranchIn,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
):
    ws = require_owner(workspace_id=workspace_id, user=user, session=session)
    try:
        create_workspace_branch(
            session,
            stamp=stamp_for_workspace(ws),
            workspace_id=workspace_id,
            name=body.name,
            workspaces_dir=request.app.state.settings.workspaces_dir,
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        _raise_http(exc)
    return {"name": body.name}


@router.post("/{workspace_id}/branches/switch", response_model=BranchListOut)
def switch_branch_route(
    workspace_id: uuid.UUID,
    body: BranchIn,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
):
    ws = require_owner(workspace_id=workspace_id, user=user, session=session)
    workspaces_dir = request.app.state.settings.workspaces_dir
    try:
        switch_branch(
            session,
            stamp=stamp_for_workspace(ws),
            workspace_id=workspace_id,
            target=body.name,
            workspaces_dir=workspaces_dir,
            teardown=_teardown_via_loop(request),
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        # Compensating checkout: step 9 is not transactional, so a rollback after
        # it leaves HEAD on the target while the column names the source. §4.1's
        # reconciliation would repair this on the next access anyway; doing it
        # here means the very next git read is already correct.
        try:
            from call_manager.proposal_assist.branches import _reconcile_head

            _reconcile_head(session, workspace_id=workspace_id, workspaces_dir=workspaces_dir)
        except Exception:
            logger.warning(
                "branches.compensating_checkout_failed",
                extra={"event": "branch_compensating_checkout_failed",
                       "workspace_id": str(workspace_id)},
                exc_info=True,
            )
        _raise_http(exc)
    names, current = list_workspace_branches(
        None, workspace_id=workspace_id, workspaces_dir=workspaces_dir
    )
    return BranchListOut(branches=names, current=current)


@router.delete("/{workspace_id}/branches/{name}", status_code=204)
def delete_branch_route(
    workspace_id: uuid.UUID,
    name: str,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
):
    ws = require_owner(workspace_id=workspace_id, user=user, session=session)
    try:
        delete_workspace_branch(
            session,
            stamp=stamp_for_workspace(ws),
            workspace_id=workspace_id,
            name=name,
            workspaces_dir=request.app.state.settings.workspaces_dir,
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        _raise_http(exc)
    return Response(status_code=204)
```

- [ ] **Step 4: Confirm the compensating checkout reuses task 7's helper**

`_reconcile_head` is already defined in `branches.py` (task 7) and is idempotent,
so the route imports it rather than re-implementing the checkout. Verify the
import in `switch_branch_route`'s `except` arm resolves:

Run: `uv run python -c "from call_manager.proposal_assist.branches import _reconcile_head; print('ok')"`
Expected: `ok`.

The compensating checkout is not redundant with §4.1's reconciliation — that one
repairs HEAD on the *next* access, this one repairs it *now*, so the very next
git read on this workspace is already correct rather than merely recoverable.

- [ ] **Step 5: Mount the router**

In `src/call_manager/main.py`, beside the `versions_router` import (lines 64-65):

```python
from call_manager.proposal_assist.web.branches import router as branches_router
```

and beside `app.include_router(versions_router)` (line 801):

```python
    app.include_router(branches_router)
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_api_branches.py -q`
Expected: `11 passed` (3 of them from the parametrised permission test).

- [ ] **Step 7: Prove the owner gate is real**

Change `require_owner` to `require_member` in `switch_branch_route` and re-run.
Expected: `test_a_non_owner_member_cannot_mutate_branches[post-/branches/switch-...]` FAILS with `assert 200 == 403`. Restore and re-run — `11 passed`.

- [ ] **Step 8: Regenerate the OpenAPI types**

Run the project's schema codegen. **`EMBEDDING_MODEL=bge-m3` must be set** or the app fails to construct and the generator writes an empty schema over the good one:

```bash
EMBEDDING_MODEL=bge-m3 uv run python -c "import json,sys;from call_manager.main import create_app;json.dump(create_app().openapi(),sys.stdout)" > /tmp/openapi.json
cd frontend && pnpm exec openapi-typescript /tmp/openapi.json -o src/api/schema.gen.ts
```

Check `git diff --stat frontend/src/api/schema.gen.ts` shows **additions only** for the four new paths. A diff that removes unrelated paths means the schema was generated from a half-built app — throw it away and fix the environment.

- [ ] **Step 9: Full suite, lint, commit**

```bash
uv run pytest -n 16 --dist loadfile
uv run ruff check src/call_manager/proposal_assist/web/branches.py src/call_manager/proposal_assist/branches.py src/call_manager/main.py tests/proposal_assist/test_api_branches.py
uv run ruff format --check src/call_manager/proposal_assist/web/branches.py src/call_manager/proposal_assist/branches.py tests/proposal_assist/test_api_branches.py
git add -A
git commit -m "feat(api): branch routes, owner-only, with the teardown bridge bounded at the edge"
```

---

### Task 10: the branch UI

**Files:**
- Create: `frontend/src/api/branches.ts`
- Create: `frontend/src/cockpit/panes/BranchPicker.tsx`
- Create: `frontend/src/cockpit/panes/BranchPicker.test.tsx`
- Modify: `frontend/src/cockpit/CockpitSidebar.tsx:23` (the `versions` view)
- Modify: `frontend/src/shell/StatusBar.tsx`
- Test: `frontend/src/shell/StatusBar.branch.test.tsx` (create)

**Interfaces:**
- Consumes: the four routes from task 9, via the regenerated `schema.gen.ts`.
- Produces: `useBranches(id)`, `useCreateBranch(id)`, `useSwitchBranch(id)`, `useDeleteBranch(id)`; `<BranchPicker id={id} />`.

**Where it goes.** The branch control sits at the top of the **Versions** pane, above the version graph — the graph shows one branch's history, so the branch selector belongs on the thing it governs. This is the same rule the rail-IA slice applied when it moved the Draft/Spec switch onto the list it governs. The **StatusBar** additionally shows the current branch beside the commit, because "which branch am I on" is a question you have while looking at the editor, not while looking at the pane.

- [ ] **Step 1: Write the API hooks**

Create `frontend/src/api/branches.ts`, following `frontend/src/api/versions.ts`'s shape:

```ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "./client";

export type BranchList = { branches: string[]; current: string };

export function useBranches(id: string | null, opts?: { enabled?: boolean }) {
  return useQuery({
    queryKey: ["branches", id],
    enabled: (opts?.enabled ?? true) && !!id,
    queryFn: async () => {
      const { data, error } = await client.GET("/api/workspaces/{workspace_id}/branches", {
        params: { path: { workspace_id: id! } },
      });
      if (error) throw error;
      return data as BranchList;
    },
  });
}

/** Everything that changes a branch invalidates the SAME set, because a switch
 *  changes the document, its history and its status bar at once. Listing them
 *  here rather than at each call site is what stops one of them being forgotten. */
function useBranchMutation(id: string | null, run: (name: string) => Promise<void>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: run,
    onSuccess: async () => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["branches", id] }),
        qc.invalidateQueries({ queryKey: ["draft", id] }),
        qc.invalidateQueries({ queryKey: ["spec", id] }),
        qc.invalidateQueries({ queryKey: ["versions", id] }),
      ]);
    },
  });
}

export function useCreateBranch(id: string | null) {
  return useBranchMutation(id, async (name) => {
    const { error } = await client.POST("/api/workspaces/{workspace_id}/branches", {
      params: { path: { workspace_id: id! } },
      body: { name },
    });
    if (error) throw error;
  });
}

export function useSwitchBranch(id: string | null) {
  return useBranchMutation(id, async (name) => {
    const { error } = await client.POST("/api/workspaces/{workspace_id}/branches/switch", {
      params: { path: { workspace_id: id! } },
      body: { name },
    });
    if (error) throw error;
  });
}

export function useDeleteBranch(id: string | null) {
  return useBranchMutation(id, async (name) => {
    const { error } = await client.DELETE("/api/workspaces/{workspace_id}/branches/{name}", {
      params: { path: { workspace_id: id!, name } },
    });
    if (error) throw error;
  });
}
```

The keys above are the real ones, measured 2026-08-29: `["draft", id]`
(`editor.ts:15`), `["spec", id]` (`editor.ts:59`), and `["versions", id, artifact]`
(`versions.ts:33`).

**`["versions", id]` is deliberately shorter than the key it invalidates.**
TanStack Query matches `invalidateQueries` keys as **prefixes**, so the
two-element key invalidates both artifacts' version lists. That is what is wanted
— a switch changes the history of draft *and* spec — so do not "fix" it by adding
an artifact. A key that matches nothing makes the invalidation a silent no-op:
the switch succeeds and the editor keeps showing the old branch.

- [ ] **Step 2: Write the failing component test**

Create `frontend/src/cockpit/panes/BranchPicker.test.tsx`:

```tsx
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { BranchPicker } from "./BranchPicker";

const state = {
  data: { branches: ["main", "v2"], current: "main" } as const,
  switchTo: vi.fn(),
  remove: vi.fn(),
};

vi.mock("../../api/branches", () => ({
  useBranches: () => ({ data: state.data, isPending: false }),
  useCreateBranch: () => ({ mutate: vi.fn(), isPending: false }),
  useSwitchBranch: () => ({ mutate: state.switchTo, isPending: false }),
  useDeleteBranch: () => ({ mutate: state.remove, isPending: false }),
}));

function renderIt() {
  return render(<BranchPicker id="w1" />);
}

describe("BranchPicker", () => {
  it("shows every branch and marks the current one", () => {
    renderIt();
    expect(screen.getByRole("button", { name: /main/ })).toBeInTheDocument();
    expect(screen.getByText("v2")).toBeInTheDocument();
  });

  it("switching asks for confirmation before it evicts anybody", async () => {
    renderIt();
    await userEvent.click(screen.getByRole("button", { name: /main/ }));
    await userEvent.click(screen.getByRole("menuitem", { name: /v2/ }));
    // A switch disconnects every other editor and throws away unsaved keystrokes.
    // It must not happen on a single stray click.
    expect(state.switchTo).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /switch branch/i }));
    await waitFor(() => expect(state.switchTo).toHaveBeenCalledWith("v2"));
  });

  it("deleting warns that it destroys commits", async () => {
    renderIt();
    await userEvent.click(screen.getByRole("button", { name: /main/ }));
    await userEvent.click(screen.getByRole("menuitem", { name: /delete v2/i }));
    expect(screen.getByText(/cannot be undone/i)).toBeInTheDocument();
    expect(state.remove).not.toHaveBeenCalled();
  });

  it("offers no delete for the branch you are on", async () => {
    renderIt();
    await userEvent.click(screen.getByRole("button", { name: /main/ }));
    expect(screen.queryByRole("menuitem", { name: /delete main/i })).toBeNull();
  });
});
```

- [ ] **Step 3: Run it to verify it fails**

Run (from `frontend/`): `pnpm vitest run src/cockpit/panes/BranchPicker.test.tsx`
Expected: fails to resolve `./BranchPicker`.

- [ ] **Step 4: Build the component**

Create `frontend/src/cockpit/panes/BranchPicker.tsx` using Mantine's `Menu`, `Button` and `Modal` (all already used elsewhere in `frontend/src/cockpit/` — do not add a dependency, `pnpm add` is blocked). Requirements the tests above pin:

- the trigger is a `button` whose accessible name contains the current branch;
- choosing another branch opens a confirm dialog whose primary action is named "Switch branch"; the mutation fires only from that dialog;
- the delete item is offered for every branch **except** the current one, and its dialog says the deletion cannot be undone;
- a "New branch…" item opens a text input and calls `useCreateBranch`.

The confirm dialogs are not decoration. A switch disconnects every other editor and discards their unsaved keystrokes (spec §5); a delete drops commits nothing else in the app can reach (spec §3.2). Both are one click from the pane.

**Playwright note for any e2e:** Mantine's `SegmentedControl` cannot be clicked directly — its radio input is hidden, so click the label. Not needed for this task's jsdom tests, but recorded because the pane sits beside one.

- [ ] **Step 5: Run the component tests**

Run: `pnpm vitest run src/cockpit/panes/BranchPicker.test.tsx`
Expected: `4 passed`.

- [ ] **Step 6: Mount it in the Versions pane**

In `frontend/src/cockpit/CockpitSidebar.tsx:23`, replace

```tsx
      {view === "versions" && <VersionGraph id={id} />}
```

with

```tsx
      {view === "versions" && (
        <>
          <BranchPicker id={id} />
          <VersionGraph id={id} />
        </>
      )}
```

and import `BranchPicker`. The graph renders one branch's history (`list_commits` follows HEAD), so the selector belongs above it.

- [ ] **Step 7: Show the branch in the status bar**

Add a failing test first — `frontend/src/shell/StatusBar.branch.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { StatusBar } from "./StatusBar";

vi.mock("../api/branches", () => ({
  useBranches: () => ({ data: { branches: ["main", "v2"], current: "v2" } }),
}));

describe("StatusBar branch", () => {
  it("names the branch beside the commit on the cockpit", () => {
    render(
      <MemoryRouter initialEntries={["/cockpit/w1?tab=draft"]}>
        <StatusBar />
      </MemoryRouter>,
    );
    expect(screen.getByText(/v2/)).toBeInTheDocument();
  });

  it("shows no branch off the cockpit", () => {
    render(
      <MemoryRouter initialEntries={["/settings"]}>
        <StatusBar />
      </MemoryRouter>,
    );
    expect(screen.queryByText(/v2/)).toBeNull();
  });
});
```

The existing `StatusBar` already derives the workspace id from the URL and gates its queries on it (`enabled: !!id`) — follow that, do not fetch off the cockpit. Render the branch immediately before the existing `{Draft|Spec} · {short(head)}` text.

Run: `pnpm vitest run src/shell/StatusBar.branch.test.tsx` — expect both to fail, then implement, then expect `2 passed`.

- [ ] **Step 8: Prove the status-bar gate bites**

Remove the cockpit condition so the branch renders everywhere, and re-run.
Expected: `shows no branch off the cockpit` FAILS. Restore — `2 passed`.

This step exists because a status-bar element that renders on every route is a defect this file has had before: it showed `Draft · <sha>` on Scorecard and Review until it was gated.

- [ ] **Step 9: Typecheck, full frontend suite, build**

```bash
cd frontend
pnpm exec tsc --noEmit
pnpm vitest run
pnpm build
```

Expected: tsc clean; the suite passes with **6 more tests than the pre-task baseline** (4 from `BranchPicker.test.tsx`, 2 from `StatusBar.branch.test.tsx`) and **2 more files**; the build succeeds. Record the actual before/after numbers — do not adjust an expectation to match.

- [ ] **Step 10: Commit**

```bash
cd "$(git rev-parse --show-toplevel)"
git add frontend/src/api/branches.ts frontend/src/cockpit/panes/BranchPicker.tsx frontend/src/cockpit/panes/BranchPicker.test.tsx frontend/src/cockpit/CockpitSidebar.tsx frontend/src/shell/StatusBar.tsx frontend/src/shell/StatusBar.branch.test.tsx
git commit -m "feat(ui): branch picker in the Versions pane, branch name in the status bar"
```

---

## Acceptance — the spec's criteria, mapped

Spec §9 lists 26 criteria. The plan covers them as follows. **A criterion with no task is a plan defect — say so rather than skipping it.**

| Spec criterion | Where |
| --- | --- |
| 1 create/switch/switch back keeps both branches | Task 7 `test_switch_moves_the_column_and_HEAD` + Task 8 |
| 2 a switch checkpoints live edits to the branch left | Task 7 `test_switch_checkpoints_live_edits_to_the_branch_being_LEFT` |
| 3 refused while a drafting turn runs | Task 7 (`_has_running_turn`, both check sites) |
| 4 non-owner cannot create/switch/delete | Task 9 `test_a_non_owner_cannot_mutate_branches` |
| 5 names validated by git | Task 1 + Task 8 `test_create_rejects_a_name_git_rejects` |
| 6 current and last branch cannot be deleted | Task 8 |
| 7 creating disturbs nothing | Task 8 `test_create_makes_the_branch_without_moving_head`, `test_create_does_not_disturb_the_log` |
| 8 write between truncate and teardown | Task 7 Step 8 (**expected to find no existing gate — record it**) |
| 9 reconnect during the switch, in two steps | Task 3 `test_a_stale_generation_is_refused_on_the_WARM_path_too` + Task 4 |
| 10–13 failure at each step | Task 7 `test_a_failure_after_truncate_rolls_the_DATABASE_back` |
| 14 stale edit form with identical bytes | **Not covered by a task.** Spec §6.1's fence is the generation bump in Task 7 step 7; the *form* path lives in `editing/save.py`. Add a test there or record the gap. |
| 15 `build_bibliography` and the pass runner refused at a superseded generation | **Not covered.** Spec §6.2 makes fixing those two unfenced writers a requirement and an ESCALATE if skipped. **This plan does not do it** — see below. |
| 16 reconnect between teardown and truncate | Task 3 |
| 17 in-flight persist does not survive teardown | Task 5 `test_reports_locked_False_when_room_lock_is_held` + Step 6 |
| 18 a timed-out teardown aborts the switch | Task 5 + Task 7 `test_a_teardown_that_could_not_LOCK_aborts_the_switch` |
| 19 no lock inversion | Task 3 Step 6 |
| 20 a failed switch still evicts, and clients recover | Task 4 + Task 5 `test_closes_with_1013_so_the_client_reconnects` |
| 21 partial teardown aborts | Task 5 `prepare_branch_switch` gather + Task 7 |
| 22 timed-out teardown still tears down | Task 5 `test_still_FINISHES_the_teardown_when_the_lock_times_out` + Step 6 |
| 23 HEAD reconciled before the checkpoint | Task 7 `test_HEAD_is_reconciled_before_the_checkpoint_reads_it` + Step 6 |
| 24 switch closes 1013 | Task 4 + Task 5 |
| 25 a turn starting after the pre-check is still refused | Task 6 + Task 7's under-mutex re-check |
| 26 the migration backfills from the repo | Task 2 |

### Two known gaps, stated rather than hidden

**Spec §6.2's unfenced writers are not in this plan.** `build_bibliography`
(`citations/bibliography.py:627`, reached from `export.py:81` and
`tools/audit.py:214`) and the review pass runner (`review/pass_runner.py:349`)
both call `write_and_commit_files` with **no** `acquire_commit_mutex` — measured.
They commit to whatever HEAD is, so after a branch switch they can write to the
new branch on behalf of work started on the old one. The spec makes fixing them a
requirement and skipping it an **ESCALATE**.

They are excluded here because each is a change to a subsystem this plan does not
otherwise touch, and folding them in would put two unrelated regression surfaces
inside the branch feature's review. **This is a decision for the human, not for
the implementer:** either an eleventh task is added before merge, or the feature
ships with those two paths knowingly unfenced. Do not merge without an answer.

**Spec criterion 14 (the stale edit form)** needs a test in `editing/save.py`'s
suite, where the AND guard at `save.py:235` lives. The generation bump in Task 7
is the mechanism; nothing in this plan asserts it stops a form opened before the
switch. Add it as part of Task 7's review or record the gap.

---

## Execution notes

- **Order matters.** Tasks 1–6 are prerequisites and each is independently mergeable. Task 7 is unsafe without 3, 5 and 6; task 9 is unsafe without 7 and 8.
- **The mutation steps are the gates.** Tasks 1, 2, 4, 5, 7, 8, 9 and 10 each contain a step that breaks the mechanism and names the test that must go red. A green suite is not evidence for any of them.
- **Task 7 Step 8 expects to find nothing.** If the suite stays green when the teardown is moved after the truncate, that is a finding about test coverage, not a pass. Record it.
- **Never adjust an expected count to make it match.** Name the term that changed.
