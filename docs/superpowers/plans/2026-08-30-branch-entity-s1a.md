# Branch Entity (spec §8 S1, steps 1–4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a branch a first-class database object with an immutable id, a root
pointer on the workspace, and a lifecycle that keeps rows and git refs in agreement.

**Architecture:** A new `branches` table holds `(id, workspace_id, name, created_at,
last_sdk_session_id)`. `workspaces.root_branch_id` names the branch whose worktree is
`repo/` — deliberately distinct from `workspaces.current_branch`, which still moves on
every switch until S4. A migration founds one row per existing local git ref, and
`create_workspace_branch` / `delete_workspace_branch` maintain the rows from this stage
onward so the backfill never goes stale. Nothing reads the new table yet: this stage is
groundwork and must change no observable behaviour.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, Alembic, PostgreSQL 16, pytest +
testcontainers.

## Scope

This plan is **spec §8 S1 steps 1–4 only** — the branch entity and its lifecycle.

The spec's S1 has six steps. Steps 5 and 6 are two further mechanical sweeps and get
their own plans:

- **Step 5** — add the branch-id column to ~12 branch-scoped tables and rework
  `uq_artifact_versions_ws_sha` (trap 2).
- **Step 6** — make `workspace_repo_dir` branch-aware at all **58 call sites across 32
  files** (measured 2026-08-30), with the type-error gate that closes trap 1.

Splitting here is not a convenience. Steps 1–4 produce one coherent, independently
testable thing: a branch is now an object with a correct lifecycle. Steps 5 and 6 are
sweeps that consume that object and would each drown this plan's review surface.

## Global Constraints

Copied from `docs/superpowers/specs/2026-08-30-per-branch-worktrees-and-rooms-design.md`
(rev `1235c8bb`). These bind every task.

- **Behaviour must not change, with ONE stated exception.** No route, no response body,
  no user-visible string moves. Nothing reads `branches` yet.
  **The exception, and it is deliberate:** `delete_workspace_branch` starts refusing the
  **root** branch (§7.3). Today it refuses only the *current* branch, so a user who
  switched away could delete the branch `repo/` was founded on. That is the hole §7.3
  closes, and closing it is a behaviour change with a user-visible 409. Task 5 pins it at
  the API level; nothing else in this plan may change behaviour.
- **The id is the identity; the name is a mutable label** (§3.2). Never key anything on
  the name.
- **`root_branch_id` is the workspace's ROOT BRANCH, moved only by promote** (§7.3) —
  **not** "whatever `repo/` currently holds". Those coincide at migration time and then
  drift, because `switch_branch` step 9 still runs `checkout_branch(repo, target)`
  (`branches.py:224-226`) and physically moves `repo/` on every switch until S2.
  **This plan must not chase that drift.** A root pointer that followed every switch would
  be `current_branch` under another name, and the whole point of §7.3 is that promote —
  not switching — moves the root. The pointer records where the workspace's root is; S2's
  first act is to check that branch back out into `repo/` and stop the wandering, which is
  recorded as a required S2 step in the spec's §8.
- **`last_sdk_session_id` is branch state, not workspace state** (§3.1, §8 S1 step 1).
  Two branches cannot resume one hosted conversation. It is copied onto the row named by
  **`workspaces.current_branch`** — the conversation belongs to the branch the *user* was
  on, and §4.1 makes the database the source of truth for that, not git HEAD. (Those two
  differ after a crashed switch; the root pointer follows HEAD, the SDK handle follows the
  column, and a test pins each.) The workspace column **stays** in this plan and is
  dropped in S4 when its last reader (`turns/processor.py:389`) goes.
- **Backfill every local ref, not just the current one** (§8 S1 step 3). A ref with no row
  is invisible to every later stage. **Filter what `list_branches` returns.** Measured on a
  detached HEAD it emits a pseudo-entry before the real refs:

  ```
  $ git branch --format='%(refname:short)'
  (HEAD detached at 777c52e)
  main
  ```

  Founding a row from that line creates a branch literally named
  `(HEAD detached at 777c52e)`. Every name must be confirmed with `branch_exists` before
  a row is written.
- **Create and delete maintain rows from this stage on** (§8 S1 step 4). A one-time
  backfill is not enough.
- **Rows and refs are NOT atomic, and this plan does not pretend otherwise.** `git branch`
  is not transactional. A commit that fails *after* the git call leaves a ref with no row
  (create) or a row with no ref (delete). No ordering fixes this without two-phase commit.
  The mitigation is that `backfill_branch_entities` is a **two-way reconciler** — it adds
  missing rows and removes orphaned ones — and is safe to re-run at any time; S2 runs it
  again before it attaches anything. Task 2 tests both directions.
- **Do NOT touch any `checkout_branch` call site.** That is S2's work
  (`branches.py:226`, `branches.py:124`, `checkpoint.py:85`). Touching it here would
  attach half of S2 to a stage that does not attach worktrees.
- `docs/ARCHITECTURE.md` is **generated**. Regenerate with
  `uv run python scripts/gen_architecture.py` in the same commit that moves the code.
  Never hand-edit it.

## Project Conventions Reference

- **Tests:** `uv run pytest -n 16 --dist loadfile` for the full suite (~1m). Never
  `-n auto` (resolves to 72 here). The whole-suite signal is **`1 skipped`**, not the
  pass total — that skip is a real-SDK test gated on `ANTHROPIC_API_KEY`. `0 skipped`
  means a live API call ran.
- **Test fixtures:** `pg_session_factory` (`tests/conftest.py`), and from
  `tests.proposal_assist.conftest`: `make_workspace(session, title=...)`,
  `init_workspace_repo(repo, content)`, `stamp_for(session, workspace_id)`. Never
  construct a `Workspace` by hand — it needs an owner.
- **The test schema is built by `alembic upgrade head`** (`tests/conftest.py:90-92`),
  NOT `Base.metadata.create_all`. So a constraint mutation in `models.py` alone changes
  nothing the database enforces and every test still passes — measured. Any mutation check
  on a constraint must edit **both** the model and the migration, or it proves nothing.
- **`tmp_path` is the `workspaces_dir`** in these tests. An autouse fixture forces
  `WORKSPACES_DIR` to a temp dir so the suite can never write to `var/workspaces`
  (`tests/conftest.py`).
- **Lint:** `uv run ruff check src tests` and `uv run ruff format --check src tests`.
  `ruff format` will reformat unrelated hunks it touches — revert those by hand before
  committing.

## File Structure

| File | Responsibility |
|---|---|
| `src/call_manager/db/models.py` | Add `class Branch`; add `Workspace.root_branch_id`. Nothing else. |
| `alembic/versions/0068_branch_entity.py` | Create `branches`, add `workspaces.root_branch_id`, run the backfill. |
| `src/call_manager/proposal_assist/branch_entity_backfill.py` | **New.** The testable half of the migration: refs → rows, and the root pointer. |
| `src/call_manager/proposal_assist/branches.py` | `create_workspace_branch` writes a row; `delete_workspace_branch` deletes one and refuses the root. |
| `tests/proposal_assist/test_branch_entity_backfill.py` | **New.** The backfill's behaviour, including what it refuses to guess. |
| `tests/proposal_assist/test_branch_entity_lifecycle.py` | **New.** Create/delete keep rows and refs in agreement. |
| `tests/architecture/test_branch_entity_invariants.py` | **New.** The two invariants a later stage depends on. |
| `docs/ARCHITECTURE.md` | Regenerated in **Task 1**, with the model change. Never edited. |

---

### Task 1: The `branches` table and the root pointer

**Files:**
- Modify: `src/call_manager/db/models.py` (add `Branch` before `class WorkspaceViewer` at
  line 779; add one column inside `class Workspace`, after `current_branch` at line 704)
- Create: `alembic/versions/0068_branch_entity.py`
- Test: `tests/proposal_assist/test_branch_entity_backfill.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `call_manager.db.models.Branch` with fields `id: uuid.UUID`,
  `workspace_id: uuid.UUID`, `name: str`, `created_at: datetime`,
  `last_sdk_session_id: str | None`. And `Workspace.root_branch_id: uuid.UUID | None`.

**Why the FK is declared this way.** `workspaces` and `branches` reference each other:
`branches.workspace_id → workspaces.id` (NOT NULL) and
`workspaces.root_branch_id → branches.id` (nullable). SQLAlchemy cannot order the two for
whole-metadata DDL, so one side must defer to a post-create `ALTER`. It has to be the
**nullable** side — the NOT NULL side cannot wait. This repo already solved the identical
problem for `users` ↔ `profiles` (`models.py:100-112`); copy that reasoning.

`ON DELETE RESTRICT` on `root_branch_id` is what gives §7.3's *"the branch the pointer
names cannot be deleted"* a database-level guarantee instead of an application-level
promise. It is safe here because **no code path hard-deletes a `Workspace` row** —
retention is freeze-only (`ef8ccb3`), so there is no cascade that could collide with the
RESTRICT.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_branch_entity_backfill.py`:

```python
"""The branch entity: a branch is a row with an immutable id (spec §3.2).

Uses the house fixtures: `make_workspace` (tests/proposal_assist/conftest.py) and
`pg_session_factory` (tests/conftest.py). Do not construct a Workspace by hand —
it needs an owner.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import Branch, Workspace
from tests.proposal_assist.conftest import make_user, make_workspace


def test_a_branch_row_can_be_created_and_read_back(pg_session_factory):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()

    with pg_session_factory() as s:
        b = Branch(workspace_id=wid, name="main")
        s.add(b)
        s.commit()
        bid = b.id

    with pg_session_factory() as s:
        row = s.get(Branch, bid)
        assert row.workspace_id == wid
        assert row.name == "main"
        assert row.last_sdk_session_id is None
        assert row.created_at is not None


def test_one_name_per_workspace(pg_session_factory):
    """The name is a label, but it is a UNIQUE label within its workspace —
    otherwise `git branch` and the rows could disagree about how many exist."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()

    with pg_session_factory() as s:
        s.add(Branch(workspace_id=wid, name="dup"))
        s.commit()

    with pg_session_factory() as s:
        s.add(Branch(workspace_id=wid, name="dup"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_the_same_name_in_two_workspaces_is_fine(pg_session_factory):
    """Control for the test above. Uniqueness is per workspace, not global —
    every workspace has a 'main', and a global unique key would break the
    second one silently at backfill time."""
    with pg_session_factory() as s:
        # Distinct owner emails: `make_user`'s default is the fixed literal
        # "owner@ki.se", so two `make_workspace` calls in one session with no
        # explicit owner collide on `uq_users_email`
        # (`tests/proposal_assist/conftest.py:200-204` documents this).
        a = make_workspace(s, title="a", owner=make_user(s, email="a@ki.se")).id
        b = make_workspace(s, title="b", owner=make_user(s, email="b@ki.se")).id
        s.commit()

    with pg_session_factory() as s:
        s.add(Branch(workspace_id=a, name="main"))
        s.add(Branch(workspace_id=b, name="main"))
        s.commit()  # must not raise

    with pg_session_factory() as s:
        assert len(s.execute(select(Branch).where(Branch.name == "main")).all()) == 2


def test_root_pointer_holds_a_branch_id(pg_session_factory):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()

    with pg_session_factory() as s:
        b = Branch(workspace_id=wid, name="main")
        s.add(b)
        s.flush()
        s.get(Workspace, wid).root_branch_id = b.id
        s.commit()
        bid = b.id

    with pg_session_factory() as s:
        assert s.get(Workspace, wid).root_branch_id == bid


def test_root_pointer_starts_null(pg_session_factory):
    """A workspace with no repo has no branch to point at. Nullable is the honest
    shape; the invariant test in Task 6 pins that a workspace WITH a repo is
    never left null."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    with pg_session_factory() as s:
        assert s.get(Workspace, wid).root_branch_id is None


def test_root_pointer_rejects_an_unknown_branch(pg_session_factory):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    with pg_session_factory() as s:
        s.get(Workspace, wid).root_branch_id = uuid.uuid4()
        with pytest.raises(IntegrityError):
            s.commit()


def test_the_root_branch_cannot_be_deleted(pg_session_factory):
    """§7.3: 'The branch the pointer names cannot be deleted.' RESTRICT makes that
    a database guarantee rather than an application promise, so a future caller
    that forgets the check cannot destroy the root."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()

    with pg_session_factory() as s:
        b = Branch(workspace_id=wid, name="main")
        s.add(b)
        s.flush()
        s.get(Workspace, wid).root_branch_id = b.id
        s.commit()
        bid = b.id

    with pg_session_factory() as s:
        s.delete(s.get(Branch, bid))
        with pytest.raises(IntegrityError):
            s.commit()


def test_a_non_root_branch_can_be_deleted(pg_session_factory):
    """Control. RESTRICT must protect ONLY the root — if it blocked every branch
    the delete path would be dead, and the test above would pass for the wrong
    reason."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()

    with pg_session_factory() as s:
        root = Branch(workspace_id=wid, name="main")
        other = Branch(workspace_id=wid, name="new_ideas")
        s.add_all([root, other])
        s.flush()
        s.get(Workspace, wid).root_branch_id = root.id
        s.commit()
        other_id = other.id

    with pg_session_factory() as s:
        s.delete(s.get(Branch, other_id))
        s.commit()  # must not raise
        assert s.get(Branch, other_id) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/proposal_assist/test_branch_entity_backfill.py -x -q
```

Expected: collection error — `ImportError: cannot import name 'Branch' from
'call_manager.db.models'`.

- [ ] **Step 3: Add the `Branch` model**

In `src/call_manager/db/models.py`, immediately before `class WorkspaceViewer(Base):`
(line 779), add:

```python
class Branch(Base):
    """One branch of one workspace (spec 2026-08-30-per-branch-worktrees §3.2).

    The **id** is the identity; `name` is a mutable label on it. Every
    branch-scoped row and every job carries the id, never the name — otherwise a
    delete-then-recreate under the same name lets an in-flight job pass the
    workspace fence and write into the replacement. Names are also allowed to
    contain `/` (`git check-ref-format --branch`), which no single URL path
    segment can carry.
    """

    __tablename__ = "branches"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_branches_ws_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    #: The hosted-conversation resume handle for THIS branch (§3.1). Two branches
    #: cannot resume one remote session — `turns/processor.py:389` passes
    #: `resume=<id>`, and sharing one would interleave two branches' turns into a
    #: single upstream conversation. `workspaces.last_sdk_session_id` still exists
    #: and is still the only one read; it is dropped in S4 when its last reader
    #: goes.
    last_sdk_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
```

`UniqueConstraint`, `Text`, `DateTime`, `ForeignKey`, `func`, `uuid`, `datetime` and
`UUID` are all already imported at the top of `models.py` (lines 4-30). No import change
is needed.

- [ ] **Step 4: Add the root pointer to `Workspace`**

In `src/call_manager/db/models.py`, inside `class Workspace`, immediately after the
`current_branch` column (line 704), add:

```python
    #: The workspace's ROOT branch (§7.3) — the one whose worktree will BE `repo/`
    #: once S2 stops checkout-based switching, and the only branch that cannot be
    #: deleted. **Moved only by promote.** Deliberately NOT `current_branch`:
    #: switching is not promoting, and a pointer that followed every switch would
    #: be `current_branch` under another name.
    #:
    #: Until S2 lands, `switch_branch` step 9 still runs `checkout_branch(repo,
    #: target)` (`branches.py:224-226`), so what `repo/` physically holds drifts
    #: away from this pointer between a switch and S2. That is expected and
    #: harmless here — nothing resolves a path through this column yet — and S2
    #: checks the root back out before it attaches any worktree.
    #:
    #: Nullable because a workspace still in setup has no repo and so no branch to
    #: point at; `tests/architecture/test_branch_entity_invariants.py` pins that a
    #: workspace WITH a repo is never left null.
    root_branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        # use_alter: `workspaces` and `branches` reference each other (this
        # column, and branches.workspace_id -> workspaces.id), so SQLAlchemy
        # cannot order the two for whole-metadata DDL. Deferring THIS side to a
        # post-create ALTER breaks the cycle; it has to be this side because it
        # is the nullable one — branches.workspace_id is NOT NULL and cannot
        # wait. Same shape as fk_users_default_profile_id (models.py:100-112).
        #
        # RESTRICT, not SET NULL: §7.3 says the root branch cannot be deleted,
        # and a database guarantee cannot be forgotten by a future caller the way
        # an `if` can. Safe because nothing hard-deletes a Workspace row —
        # retention is freeze-only (ef8ccb3) — so no cascade collides with it.
        ForeignKey(
            "branches.id",
            ondelete="RESTRICT",
            name="fk_workspaces_root_branch_id",
            use_alter=True,
        ),
        nullable=True,
    )
```

- [ ] **Step 5: Write the migration**

Create `alembic/versions/0068_branch_entity.py`:

```python
"""branches — a branch is a row with an immutable id, and the workspace names its root

Revision ID: 0068
Revises: 0067
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


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


def downgrade() -> None:
    op.drop_constraint("fk_workspaces_root_branch_id", "workspaces", type_="foreignkey")
    op.drop_column("workspaces", "root_branch_id")
    op.drop_index("ix_branches_workspace_id", table_name="branches")
    op.drop_table("branches")
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
uv run pytest tests/proposal_assist/test_branch_entity_backfill.py -q
```

Expected: `8 passed`.

- [ ] **Step 7: Prove the RESTRICT test bites**

A constraint test that would pass without the constraint is worth nothing. Temporarily
change `ondelete="RESTRICT"` to `ondelete="SET NULL"` in **both** `models.py` and
`alembic/versions/0068_branch_entity.py`, then:

```bash
uv run pytest tests/proposal_assist/test_branch_entity_backfill.py -q
```

Expected: `test_the_root_branch_cannot_be_deleted` FAILS with
`DID NOT RAISE <class 'sqlalchemy.exc.IntegrityError'>`, and
`test_a_non_root_branch_can_be_deleted` still passes.

Revert both edits back to `RESTRICT` and re-run — `8 passed`.

- [ ] **Step 8: Regenerate the architecture map**

`tests/architecture/test_architecture_map_is_current.py` regenerates the map and compares
it to the committed file, so **the suite goes red the moment a model changes and stays red
until this runs.** It must happen in this task, not a later one.

```bash
uv run python scripts/gen_architecture.py
uv run pytest tests/architecture/test_architecture_map_is_current.py -q
```

Expected: the map gains the `branches` table and both new foreign keys with their ON
DELETE behaviour, and the test passes. **If `git diff docs/ARCHITECTURE.md` is empty, the
generator did not see the model and something in Steps 3-4 is wrong.**

`uq_branches_ws_name` will NOT appear: `scripts/gen_architecture.py` tracks partial unique
*indexes* (`Index(..., unique=True, postgresql_where=...)`) and does not read
`UniqueConstraint(...)` at all — measured, `grep -c UniqueConstraint scripts/gen_architecture.py`
is 0. Its absence is not a bug.

- [ ] **Step 9: Lint and commit**

```bash
uv run ruff check src tests && uv run ruff format --check src tests
git add src/call_manager/db/models.py alembic/versions/0068_branch_entity.py \
        tests/proposal_assist/test_branch_entity_backfill.py docs/ARCHITECTURE.md
git commit -m "feat(branches): a branch is a row with an immutable id

Spec 2026-08-30-per-branch-worktrees §3.2 and §8 S1 steps 1-2. Nothing reads
either yet — this stage founds the object.

workspaces.root_branch_id is DISTINCT from current_branch by design:
current_branch moves on every switch until S4, and resolving a path through a
pointer that moves would send a commit to repo/ while repo/ held a different
branch.

ON DELETE RESTRICT gives §7.3's 'the root branch cannot be deleted' a database
guarantee rather than an application promise. Safe because nothing hard-deletes
a Workspace row (retention is freeze-only, ef8ccb3).

use_alter on the nullable side, the same shape as fk_users_default_profile_id.

ARCHITECTURE.md regenerated in the same commit that moved the model."
```

---

### Task 2: The backfill — every ref gets a row

**Files:**
- Create: `src/call_manager/proposal_assist/branch_entity_backfill.py`
- Test: `tests/proposal_assist/test_branch_entity_backfill.py` (append)

**Interfaces:**
- Consumes: `Branch`, `Workspace.root_branch_id` from Task 1.
- Produces:
  `backfill_branch_entities(session: Session, *, workspaces_dir: Path) -> BackfillResult`
  — a `NamedTuple` with fields `created: int`, `deleted: int`, `rooted: int`,
  `skipped: int`. Does **not** commit.

**It is a reconciler, not a one-shot backfill.** Rows and refs cannot be kept atomic (git
is not transactional), so this function must be safe and useful to run at any time: it
adds a row for every ref that lacks one AND deletes every row whose ref is gone. That is
what repairs a create or delete whose commit failed after the git call, and it is what S2
runs before it attaches anything.

**`skipped` is returned, not swallowed.** A workspace whose repo cannot be read is left
without rows, and the caller has to be able to say so. Counting it is what stops
"the migration succeeded" from meaning "every workspace was founded".

**Why a separate module.** The migration is untestable in place, and this is the half that
can be wrong. `branch_backfill.py` already established exactly this split for
`current_branch`; follow it.

**The two columns split, and each follows a different source.** After a crashed switch,
git HEAD and `workspaces.current_branch` disagree — `_reconcile_head`
(`branches.py:100-126`) exists precisely because they can. At migration time:

| What | Follows | Why |
|---|---|---|
| `workspaces.root_branch_id` | **git HEAD** | The root is founded on what `repo/` actually is. A pointer founded on the column would name a tree nobody has. |
| `branches.last_sdk_session_id` | **`workspaces.current_branch`** | It is a *conversation*, and it belongs to the branch the **user** was on. §4.1 makes the database the source of truth for that. Spec §8 S1 step 1 says so explicitly. |

Getting these the same way round is not a detail — a test pins each, and they use the
same fixture with HEAD and the column deliberately disagreeing.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_branch_entity_backfill.py`:

```python
import subprocess

from call_manager.proposal_assist.branch_entity_backfill import backfill_branch_entities
from call_manager.proposal_assist.paths import workspace_repo_dir

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": "/usr/bin:/bin",
}


def _repo(workspaces_dir, workspace_id, head, *, extra=(), commit=True, detach=False):
    """A workspace repo on branch `head`, plus one ref per name in `extra`."""
    d = workspace_repo_dir(workspaces_dir, workspace_id)
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", head, "."], cwd=d, check=True)
    if commit:
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=d, check=True, env=_GIT_ENV
        )
        for name in extra:
            subprocess.run(["git", "branch", name], cwd=d, check=True)
    if detach:
        subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=d, check=True)
    return d


def _names(session, wid):
    return sorted(
        n for (n,) in session.execute(select(Branch.name).where(Branch.workspace_id == wid)).all()
    )


def test_every_local_ref_gets_a_row_not_just_the_current_one(pg_session_factory, tmp_path):
    """The whole point of step 3. `create_workspace_branch` has shipped since
    880ea243 and does nothing but `git branch`, so a workspace can already hold
    several refs. A ref with no row is invisible to every later stage."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo(tmp_path, wid, "main", extra=("new_ideas", "eu_version"))

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert (r.created, r.deleted, r.rooted, r.skipped) == (3, 0, 1, 0)
        assert _names(s, wid) == ["eu_version", "main", "new_ideas"]


def test_a_slashed_branch_name_round_trips(pg_session_factory, tmp_path):
    """`git check-ref-format --branch` accepts `aims/ambitious`, and
    `test_api_branches.py:114-121` already relies on it — the delete route uses
    `{name:path}` for exactly this. §3.2 makes slashes the reason a branch needs an
    id at all, so a slashed name must survive the row it is founded into."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo(tmp_path, wid, "main", extra=("aims/ambitious",))

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert _names(s, wid) == ["aims/ambitious", "main"]


def test_the_root_pointer_names_the_branch_repo_actually_holds(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo(tmp_path, wid, "aims-ambitious", extra=("main",))

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:
        root_id = s.get(Workspace, wid).root_branch_id
        assert s.get(Branch, root_id).name == "aims-ambitious"


def _head_and_column_disagree(session_factory, tmp_path):
    """The state a crashed switch leaves: `repo/` is on main, the column says
    new_ideas. `_reconcile_head` (branches.py:100-126) exists because of it."""
    with session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo(tmp_path, wid, "main", extra=("new_ideas",))
    with session_factory() as s:
        ws = s.get(Workspace, wid)
        ws.current_branch = "new_ideas"
        ws.last_sdk_session_id = "sess-abc"
        s.commit()
    return wid


def test_the_root_pointer_follows_git_not_the_column(pg_session_factory, tmp_path):
    """§7.3 founds the root on what `repo/` actually is. Reading `current_branch`
    here would point the root at a tree nobody has."""
    wid = _head_and_column_disagree(pg_session_factory, tmp_path)

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:
        root_id = s.get(Workspace, wid).root_branch_id
        assert s.get(Branch, root_id).name == "main", "root followed the column, not repo/"


def test_the_sdk_handle_follows_the_column_not_git(pg_session_factory, tmp_path):
    """The mirror image, on the SAME disagreeing fixture. A conversation belongs to
    the branch the USER was on, and §4.1 makes the column the source of truth for
    that (spec §8 S1 step 1). Getting these two the same way round would put the
    handle on a branch the user was never talking on."""
    wid = _head_and_column_disagree(pg_session_factory, tmp_path)

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:
        rows = s.execute(select(Branch).where(Branch.workspace_id == wid)).scalars().all()
        assert {b.name: b.last_sdk_session_id for b in rows} == {
            "main": None,
            "new_ideas": "sess-abc",
        }


def test_the_sdk_handle_lands_on_one_branch_only(pg_session_factory, tmp_path):
    """§3.1: copying it to every branch would let two branches resume the same
    remote session."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.get(Workspace, wid).last_sdk_session_id = "sess-abc"
        s.commit()
    _repo(tmp_path, wid, "main", extra=("new_ideas", "eu_version"))

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:
        rows = s.execute(select(Branch).where(Branch.workspace_id == wid)).scalars().all()
        carried = [b.name for b in rows if b.last_sdk_session_id == "sess-abc"]
        assert carried == ["main"]


def test_the_workspace_column_is_left_alone(pg_session_factory, tmp_path):
    """Control. S4 drops it, not this stage — `turns/processor.py:389` is still the
    only reader and it still reads the workspace. Nulling it here would break
    conversation resume for every workspace with no test noticing."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.get(Workspace, wid).last_sdk_session_id = "sess-abc"
        s.commit()
    _repo(tmp_path, wid, "main")

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:
        assert s.get(Workspace, wid).last_sdk_session_id == "sess-abc"


def test_a_row_whose_ref_is_gone_is_deleted(pg_session_factory, tmp_path):
    """The other half of the repair. `git branch -D` is not transactional, so a
    delete whose commit failed afterwards leaves a row with no ref — and nothing
    else would ever remove it."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo(tmp_path, wid, "main")
    with pg_session_factory() as s:
        # `main` needs its row ALREADY, or the reconciler creates it in the same
        # pass and `created` is 1 — which would make the assertion below about two
        # things at once instead of isolating the delete path.
        s.add(Branch(workspace_id=wid, name="main"))
        s.add(Branch(workspace_id=wid, name="deleted_behind_our_back"))
        s.commit()

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert (r.created, r.deleted) == (0, 1)
        assert _names(s, wid) == ["main"]


def test_an_unreadable_repo_deletes_nothing(pg_session_factory, tmp_path):
    """The dangerous direction of the reconciler. A transient git failure must
    never read as 'this workspace has no branches any more' and wipe its rows."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    repo = _repo(tmp_path, wid, "main", extra=("new_ideas",))
    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
    (repo / ".git" / "HEAD").write_text("garbage\n")

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert r.deleted == 0
        assert r.skipped == 1
        assert _names(s, wid) == ["main", "new_ideas"]


def test_running_it_twice_changes_nothing(pg_session_factory, tmp_path):
    """A migration can be re-run against a partially-migrated database, and the
    unique key would turn that into a hard failure rather than a no-op."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo(tmp_path, wid, "main", extra=("new_ideas",))

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert (r.created, r.deleted, r.rooted) == (2, 0, 1)

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert (r.created, r.deleted, r.rooted) == (0, 0, 1)
        assert _names(s, wid) == ["main", "new_ideas"]


def test_a_workspace_with_no_repo_is_skipped_and_not_a_failure(pg_session_factory, tmp_path):
    """Still in setup. There is nothing to found a branch on, and inventing a
    'main' row would create a row for a ref that does not exist. It is not a
    failure either, so it must not inflate `skipped`."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert (r.created, r.rooted, r.skipped) == (0, 0, 0)
        assert _names(s, wid) == []
        assert s.get(Workspace, wid).root_branch_id is None


def test_a_repo_with_no_commits_is_skipped(pg_session_factory, tmp_path):
    """`git init` with no commit has a branch name but no ref — there is no ref to
    found a row on."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo(tmp_path, wid, "main", commit=False)

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert (r.created, r.rooted) == (0, 0)
        assert _names(s, wid) == []


def test_a_detached_head_still_founds_the_refs_but_no_root(pg_session_factory, tmp_path):
    """Step 3 says every local ref gets a row, and detaching HEAD removes no ref —
    `main` is still there. Only the ROOT is unknown, so only the root is left
    unset. Skipping the whole workspace would violate step 3."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo(tmp_path, wid, "main", extra=("new_ideas",), detach=True)

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert (r.created, r.rooted) == (2, 0)
        assert _names(s, wid) == ["main", "new_ideas"]
        assert s.get(Workspace, wid).root_branch_id is None


def test_a_detached_head_founds_no_pseudo_branch(pg_session_factory, tmp_path):
    """`git branch --format=%(refname:short)` emits a pseudo-entry on a detached
    HEAD — measured, it prints `(HEAD detached at <sha>)` ABOVE the real refs.
    Founding a row from that line creates a branch every later stage would try to
    resolve to a worktree. Asserted as exact set equality: a membership check
    ('main is in there') would pass with the pseudo-row sitting beside it."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo(tmp_path, wid, "main", detach=True)

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert _names(s, wid) == ["main"]


def test_reconciling_survives_the_root_branchs_ref_being_deleted(pg_session_factory, tmp_path):
    """`fk_workspaces_root_branch_id` is ON DELETE RESTRICT, so deleting a row the
    workspace still points at raises ForeignKeyViolation — and this function holds
    ONE transaction for every workspace, so that would abort the whole batch and
    discard every workspace already reconciled.

    Not a contrived state: today's delete route guards only `current_branch`
    (`branches.py:348`), so switching off `main` and deleting it is allowed.

    The second workspace is the point. It is reconciled BEFORE the broken one in
    the same call, so if the delete raises, its rows roll back with everything
    else — asserting only on the broken workspace would miss the collateral.
    """
    with pg_session_factory() as s:
        broken = make_workspace(s, title="broken", owner=make_user(s, email="br@ki.se")).id
        other = make_workspace(s, title="other", owner=make_user(s, email="ot@ki.se")).id
        s.commit()
    for wid in (broken, other):
        _repo(tmp_path, wid, "main", extra=("new_ideas",))

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    d = workspace_repo_dir(tmp_path, broken)
    subprocess.run(["git", "checkout", "-q", "new_ideas"], cwd=d, check=True)
    subprocess.run(["git", "branch", "-D", "main"], cwd=d, check=True)

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)  # must not raise
        s.commit()
        assert r.deleted == 1
        assert _names(s, broken) == ["new_ideas"]
        assert _names(s, other) == ["main", "new_ideas"], "the healthy workspace rolled back"
        # repo/ is on new_ideas now, so that is the root the reconciler resolves.
        assert s.get(Branch, s.get(Workspace, broken).root_branch_id).name == "new_ideas"


def test_a_rootless_workspace_whose_root_ref_vanished_is_left_honestly_rootless(
    pg_session_factory, tmp_path
):
    """The other branch of the same fix: no new root is knowable AND the old root's
    ref is gone. Clearing the pointer is the honest answer — pointing it at a
    surviving branch would silently promote something nobody chose, and §7.3 gives
    that power to promote alone."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w", owner=make_user(s, email="rootless@ki.se")).id
        s.commit()
    _repo(tmp_path, wid, "main", extra=("new_ideas",))

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    d = workspace_repo_dir(tmp_path, wid)
    subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=d, check=True)
    subprocess.run(["git", "branch", "-D", "main"], cwd=d, check=True)

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)  # must not raise
        s.commit()
        assert (r.deleted, r.rooted) == (1, 0)
        assert _names(s, wid) == ["new_ideas"]
        assert s.get(Workspace, wid).root_branch_id is None


def test_a_surviving_root_pointer_is_not_cleared_by_an_unrelated_orphan(
    pg_session_factory, tmp_path
):
    """Control for the test above. The clear must fire ONLY when the doomed row is
    the one the pointer names — a blanket 'detached plus orphans, so null it'
    would silently unroot every healthy workspace that happened to have a stale
    row lying around."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w", owner=make_user(s, email="survive@ki.se")).id
        s.commit()
    _repo(tmp_path, wid, "main")

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.add(Branch(workspace_id=wid, name="stale_unrelated"))
        s.commit()
        root_before = s.get(Workspace, wid).root_branch_id

    d = workspace_repo_dir(tmp_path, wid)
    subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=d, check=True)

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert r.deleted == 1
        assert s.get(Workspace, wid).root_branch_id == root_before, "unrooted a healthy workspace"


def test_one_broken_repo_does_not_halt_the_rest(pg_session_factory, tmp_path):
    """A migration that dies on workspace 7 of 500 leaves the database half
    founded, and the unique key makes the retry fail differently."""
    with pg_session_factory() as s:
        # Distinct owners: `make_user`'s default email is the fixed literal
        # "owner@ki.se" and two calls in one session collide on `uq_users_email`
        # (`tests/proposal_assist/conftest.py:200-204`).
        good = make_workspace(s, title="good", owner=make_user(s, email="good@ki.se")).id
        bad = make_workspace(s, title="bad", owner=make_user(s, email="bad@ki.se")).id
        s.commit()
    _repo(tmp_path, good, "main")
    broken = workspace_repo_dir(tmp_path, bad)
    broken.mkdir(parents=True)
    (broken / ".git").write_text("not a git dir")

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert (r.created, r.rooted, r.skipped) == (1, 1, 1)
        assert _names(s, good) == ["main"]
        assert _names(s, bad) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/proposal_assist/test_branch_entity_backfill.py -x -q
```

Expected: collection error — `ModuleNotFoundError: No module named
'call_manager.proposal_assist.branch_entity_backfill'`.

- [ ] **Step 3: Write the backfill**

Create `src/call_manager/proposal_assist/branch_entity_backfill.py`:

```python
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
        except (GitCommitError, OSError, subprocess.TimeoutExpired):
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
        session.flush()  # assign ids before anything reads one back
        orphans = existing - set(names)

        # The root pointer: git HEAD. Detached leaves it alone rather than guessing.
        try:
            head = current_branch(repo)
        except (GitCommitError, OSError, subprocess.TimeoutExpired):
            head = None

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
        if head in names:
            root = session.execute(
                select(Branch).where(Branch.workspace_id == wid, Branch.name == head)
            ).scalar_one()
            session.execute(
                update(Workspace).where(Workspace.id == wid).values(root_branch_id=root.id)
            )
            rooted += 1
        elif orphans:
            # No new root is knowable (detached HEAD, or HEAD on a ref that is
            # somehow absent) AND rows are about to go. Clear the pointer only if
            # it names one of the doomed rows: a workspace whose root survived
            # keeps it, and one whose root's ref vanished is left honestly
            # rootless rather than pointed at a branch that no longer exists.
            rooted_at = session.get(Workspace, wid).root_branch_id
            if rooted_at is not None:
                doomed = session.get(Branch, rooted_at)
                if doomed is not None and doomed.name in orphans:
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
```


- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/proposal_assist/test_branch_entity_backfill.py -q
```

Expected: `26 passed` (8 from Task 1, 18 added here).

- [ ] **Step 5: Prove the three tests that can be hollow actually bite**

Run each mutation, record which tests redden, and revert before the next.

**Mutation A — swap the two sources.** Both halves must be swapped INDEPENDENTLY:

```python
            head = session.get(Workspace, wid).current_branch   # was: current_branch(repo)
        current = current_branch(repo)                          # was: the column
```

**Do not write `current = head`.** Once `head` reads the column, `current = head` is still
the column — the SDK half is unchanged and its test stays green, which reads as "that test
is hollow" when the mutation is simply not a swap. Measured: the correct swap above reddens
five tests, including both named below.

Expected: **five** tests fail — `test_the_root_pointer_follows_git_not_the_column` with
`root followed the column, not repo/`, `test_the_sdk_handle_follows_the_column_not_git`,
`test_the_root_pointer_names_the_branch_repo_actually_holds`,
`test_a_detached_head_still_founds_the_refs_but_no_root` and
`test_a_detached_head_founds_no_pseudo_branch`. The first two are the point: they share one
fixture where HEAD and the column disagree, so a mutation reddening only one would mean the
other is not reading what it claims.

**Mutation B — drop the ref filter.** Change `_real_refs` to `return list_branches(repo)`.

Expected: `test_a_detached_head_founds_no_pseudo_branch` FAILS, showing a row named
`(HEAD detached at ...)` in the set.

**Mutation C — make the reconciler one-way.** Delete the `orphans` block.

Expected: `test_a_row_whose_ref_is_gone_is_deleted` FAILS with
`assert (0, 0) == (0, 1)`, and `test_an_unreadable_repo_deletes_nothing` still PASSES —
if that one also fails, it is being satisfied by the deletion path rather than by the
skip.

**Mutation D — put the delete back before the pointer move.** Move the `if orphans:` /
`delete(Branch)` block above the `if head in names:` block.

Expected: `test_reconciling_survives_the_root_branchs_ref_being_deleted` and
`test_a_rootless_workspace_whose_root_ref_vanished_is_left_honestly_rootless` both FAIL
with `ForeignKeyViolation ... fk_workspaces_root_branch_id`. This is the ordering that
shipped in the first draft of this plan and was caught in review — `ON DELETE RESTRICT`
refused to let the reconciler orphan the root pointer, and because the function holds ONE
transaction for every workspace, the violation aborted the entire batch.

Revert all four and re-run — `26 passed`.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src tests && uv run ruff format --check src tests
git add src/call_manager/proposal_assist/branch_entity_backfill.py \
        tests/proposal_assist/test_branch_entity_backfill.py
git commit -m "feat(branches): found the branch entity from what git already has

Spec §8 S1 step 3. Every local ref gets a row, not just the current one: create
has shipped since 880ea243 as a bare \`git branch\`, so a workspace can already
hold refs no column names, and a ref with no row is invisible to every later
stage.

The root pointer reads git HEAD, never workspaces.current_branch. Those two
disagree after a crashed switch — _reconcile_head exists for that case — and
§7.3 makes the root the branch that physically lives in repo/. A path resolves
to files, so the pointer follows the files.

Separate module, not migration-local, for the reason branch_backfill.py already
established: the migration is untestable in place and this is the half that can
be wrong."
```

---

### Task 3: Run the backfill from the migration

**Files:**
- Modify: `alembic/versions/0068_branch_entity.py`
- Test: `tests/proposal_assist/test_branch_entity_backfill.py` (append one test)

**Interfaces:**
- Consumes: `backfill_branch_entities(session, *, workspaces_dir)` from Task 2.
- Produces: nothing new; the migration now founds branches on upgrade.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_branch_entity_backfill.py`:

```python
def test_the_migration_calls_the_backfill_inside_upgrade():
    """The migration body cannot be executed in a unit test — it needs a live
    Alembic op context — so pin the wiring by PARSING it.

    A substring search would not do: `assert "backfill_branch_entities" in source`
    is satisfied by the import line alone, by a comment, or by dead code after a
    `return`. That is the exact shape of a gate that passes because its subject is
    already present. This walks the AST of `upgrade()` and requires an actual Call
    whose callee is named `backfill_branch_entities`.
    """
    import ast
    import pathlib

    src = pathlib.Path("alembic/versions/0068_branch_entity.py").read_text()
    tree = ast.parse(src)
    upgrade = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"
    )
    called = {
        n.func.id
        for n in ast.walk(upgrade)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "backfill_branch_entities" in called, sorted(called)
```

**Why by path and not by import:** a module name starting with a digit is not a valid
Python identifier, so `import alembic.versions.0068_branch_entity` is a syntax error and
`importlib.import_module` on it also fails. Reading the file is the only form that runs.

**After pasting this test, run `uv run ruff format tests/proposal_assist/test_branch_entity_backfill.py`.**
The block above does not satisfy `ruff format --check` as written — one wrapped line gets
collapsed. Formatting changes no assertion; skipping it fails the lint gate.

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest "tests/proposal_assist/test_branch_entity_backfill.py::test_the_migration_calls_the_backfill_inside_upgrade" -q
```

Expected: FAIL with `assert 'backfill_branch_entities' in {'UUID'}` — measured.

The set is that short because the walk collects only `ast.Name` callees, and every
`op.create_table(...)` / `op.create_index(...)` is an `ast.Attribute` call. That is fine
for this test's job (`backfill_branch_entities` IS a bare name) but it means the failure
message will not list the migration's `op.*` calls. Do not "fix" the test when the set
looks short.

- [ ] **Step 3: Wire the backfill into the migration**

In `alembic/versions/0068_branch_entity.py`, append to the end of `upgrade()`:

```python
    # Then found the branches from each repo. Same split as 0066: the migration is
    # untestable in place, so the half that can be wrong lives in a module with
    # tests. `branches` and `root_branch_id` both exist by this point.
    import os

    from sqlalchemy.orm import Session

    from call_manager.proposal_assist.branch_entity_backfill import backfill_branch_entities

    workspaces_dir = os.environ.get("WORKSPACES_DIR", "var/workspaces")
    with Session(bind=op.get_bind()) as s:
        result = backfill_branch_entities(s, workspaces_dir=workspaces_dir)
        s.commit()
    # Say what happened. A workspace whose repo could not be read is left with no
    # rows and no root pointer, and the migration still succeeds — that is the
    # right trade (one bad repo must not block a deploy) but it must not be
    # silent, or "the migration succeeded" reads as "every workspace was founded".
    print(
        f"0068: founded {result.created} branch rows, removed {result.deleted} orphans, "
        f"set {result.rooted} root pointers, SKIPPED {result.skipped} unreadable repos"
    )
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
uv run pytest tests/proposal_assist/test_branch_entity_backfill.py -q
```

Expected: `27 passed`.

- [ ] **Step 5: Run the migration against a real database**

The unit tests never execute this migration's body — the AST test above only parses it.
Run it for real.

**First find out which database `alembic.ini` / `DATABASE_URL` resolves to.** This repo's
main tree runs the user's live app against `call-monitor-db-1` on port 5432. **Never
migrate that.** If the resolved URL is the live database, stop and say so. The safe route,
used when this plan was executed, is a throwaway Postgres from the repo's own
`docker-compose.yml` under a distinct Compose project name and port, torn down afterwards.
Verify afterwards with `docker ps` that nothing was left running.

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

Expected: all three succeed with no error. The down-then-up proves `downgrade()` drops the
FK before the table — dropping the table first fails with a dependency error.

**If the app is running against this database, do not stop it.** These commands are
additive and the down-migration only removes columns nothing reads yet.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src tests alembic && uv run ruff format --check src tests alembic
git add alembic/versions/0068_branch_entity.py \
        tests/proposal_assist/test_branch_entity_backfill.py
git commit -m "feat(branches): the migration founds the branches

Same split as 0066: the migration body is untestable in place, so the half that
can be wrong lives in branch_entity_backfill.py with tests, and this wires it.
The test pins the wiring by name — a migration that creates the table and
forgets to call the backfill leaves every existing workspace branchless, and
nothing else in the suite would notice.

Verified with a real upgrade / downgrade / upgrade cycle."
```

---

### Task 4: `create_workspace_branch` writes the row

**Files:**
- Modify: `src/call_manager/proposal_assist/branches.py:269-314` (`create_workspace_branch`)
- Test: `tests/proposal_assist/test_branch_entity_lifecycle.py` (new)

**Interfaces:**
- Consumes: `Branch` from Task 1.
- Produces: `create_workspace_branch` now inserts one `Branch` row alongside the git ref.
  Its signature is unchanged:
  `create_workspace_branch(session, *, stamp, workspace_id, name, workspaces_dir) -> None`.

**Why this cannot wait for the next stage.** A one-time backfill goes stale the moment the
next branch is created. Between Task 3 and S2 the app keeps serving `POST
/branches`, and every branch it creates would be a ref with no row — invisible to the
resolver S2 installs. (Spec §8 S1 step 4; round-3 findings 5 and 8.)

**Do not touch the `_reconcile_head` call.** It checks out, and removing checkout is S2's
work across all three sites at once.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_branch_entity_lifecycle.py`:

```python
"""Create and delete keep the rows and the refs in agreement.

A one-time backfill goes stale the moment the next branch is created, and the app
keeps serving POST /branches between this stage and S2 (spec §8 S1 step 4).
"""

import subprocess

import pytest
from sqlalchemy import select

from call_manager.db.models import Branch, Workspace
from call_manager.proposal_assist.branches import (
    BranchExists,
    create_workspace_branch,
    delete_workspace_branch,
)
from call_manager.proposal_assist.citations.gitio import branch_exists, list_branches
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import init_workspace_repo, make_workspace, stamp_for


def _ws(session_factory, tmp_path):
    with session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    repo = workspace_repo_dir(tmp_path, wid)
    init_workspace_repo(repo, "# Aims\n\nbody\n")
    with session_factory() as s:
        b = Branch(workspace_id=wid, name="main")
        s.add(b)
        s.flush()
        s.get(Workspace, wid).root_branch_id = b.id
        s.commit()
    return wid, repo


def _rows(session, wid):
    return sorted(
        n for (n,) in session.execute(select(Branch.name).where(Branch.workspace_id == wid)).all()
    )


def test_create_writes_a_row_beside_the_ref(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="new_ideas",
            workspaces_dir=tmp_path,
        )
        s.commit()

    assert branch_exists(repo, "new_ideas")
    with pg_session_factory() as s:
        assert _rows(s, wid) == ["main", "new_ideas"]


def test_the_new_row_carries_a_fresh_id_and_no_sdk_handle(pg_session_factory, tmp_path):
    """§3.2: the id is the identity. A new branch has held no conversation, so its
    resume handle starts empty rather than inheriting the parent's — sharing one
    would interleave two branches' turns upstream (§3.1)."""
    wid, _ = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        s.execute(
            select(Branch).where(Branch.workspace_id == wid, Branch.name == "main")
        ).scalar_one().last_sdk_session_id = "sess-parent"
        s.commit()

    with pg_session_factory() as s:
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="new_ideas",
            workspaces_dir=tmp_path,
        )
        s.commit()

    with pg_session_factory() as s:
        rows = s.execute(select(Branch).where(Branch.workspace_id == wid)).scalars().all()
        by_name = {b.name: b for b in rows}
        assert by_name["new_ideas"].last_sdk_session_id is None
        assert by_name["new_ideas"].id != by_name["main"].id


def test_a_refused_create_writes_no_row(pg_session_factory, tmp_path):
    """The ref and the row must fail together. A row for a branch git refused to
    create is exactly the divergence this task exists to prevent."""
    wid, _ = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="new_ideas",
            workspaces_dir=tmp_path,
        )
        s.commit()

    with pg_session_factory() as s:
        with pytest.raises(BranchExists):
            create_workspace_branch(
                s,
                stamp=stamp_for(s, wid),
                workspace_id=wid,
                name="new_ideas",
                workspaces_dir=tmp_path,
            )
        s.rollback()

    with pg_session_factory() as s:
        assert _rows(s, wid) == ["main", "new_ideas"]


def test_rows_and_refs_agree_after_a_create(pg_session_factory, tmp_path):
    """The invariant stated as itself, over the whole set. A subset assertion
    ('new_ideas is in there') would pass while an orphan row sat beside it."""
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        for name in ("new_ideas", "eu_version"):
            create_workspace_branch(
                s,
                stamp=stamp_for(s, wid),
                workspace_id=wid,
                name=name,
                workspaces_dir=tmp_path,
            )
        s.commit()

    with pg_session_factory() as s:
        assert _rows(s, wid) == sorted(list_branches(repo))
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/proposal_assist/test_branch_entity_lifecycle.py -q
```

Expected: all four fail, but **not all with the same message** — check each, because a
test that fails for the wrong reason is not a test:

| Test | Expected failure |
|---|---|
| `test_create_writes_a_row_beside_the_ref` | `assert ['main'] == ['main', 'new_ideas']` |
| `test_the_new_row_carries_a_fresh_id_and_no_sdk_handle` | `KeyError: 'new_ideas'` — `by_name` is dereferenced before any equality assertion |
| `test_a_refused_create_writes_no_row` | `assert ['main'] == ['main', 'new_ideas']` — the `BranchExists` half already passes today |
| `test_rows_and_refs_agree_after_a_create` | `assert ['main'] == ['eu_version', 'main', 'new_ideas']` |

- [ ] **Step 3: Write the row inside `create_workspace_branch`**

In `src/call_manager/proposal_assist/branches.py`, in `create_workspace_branch`, replace:

```python
    git_create_branch(repo, name)
    logger.info(
        "branches.created",
        extra={"event": "branch_created", "workspace_id": str(workspace_id), "branch": name},
    )
```

with:

```python
    git_create_branch(repo, name)
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
```

Change the import at `branches.py:20` from:

```python
from call_manager.db.models import TurnJob, Workspace
```

to:

```python
from call_manager.db.models import Branch, TurnJob, Workspace
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/proposal_assist/test_branch_entity_lifecycle.py -q
```

Expected: `4 passed`.

- [ ] **Step 5: Run the existing branch suite for regressions**

```bash
uv run pytest tests/proposal_assist/test_branch_create_delete.py \
              tests/proposal_assist/test_api_branches.py \
              tests/proposal_assist/test_branch_switch.py -q
```

Expected: all pass. Behaviour must not have changed — if `test_api_branches.py` moves, a
response body changed and that violates this plan's first global constraint.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src tests && uv run ruff format --check src tests
git add src/call_manager/proposal_assist/branches.py \
        tests/proposal_assist/test_branch_entity_lifecycle.py
git commit -m "feat(branches): create founds the row beside the ref

Spec §8 S1 step 4. A one-time backfill goes stale the moment the next branch is
created, and the app keeps serving POST /branches between this stage and S2 —
every branch it made would be a ref with no row, invisible to the branch-aware
resolver S2 installs.

The row is added AFTER git_create_branch so a ref git refused leaves no orphan
behind. last_sdk_session_id starts NULL: a new branch has held no hosted
conversation, and inheriting the parent's handle would let two branches resume
one remote session."
```

---

### Task 5: `delete_workspace_branch` removes the row

**Files:**
- Modify: `src/call_manager/proposal_assist/branches.py:317-364` (`delete_workspace_branch`)
- Test: `tests/proposal_assist/test_branch_entity_lifecycle.py` (append)

**Interfaces:**
- Consumes: `Branch` from Task 1, `create_workspace_branch` from Task 4.
- Produces: `delete_workspace_branch` now deletes the `Branch` row and raises
  `BranchInUse` when the branch is the workspace's root. Signature unchanged.

**Why the root check is added here.** §7.3 says the root branch cannot be deleted. The
database enforces it via `ON DELETE RESTRICT` (Task 1), but an `IntegrityError` is not an
error a user can read. This raises the existing `BranchInUse` first, so the RESTRICT
becomes the backstop rather than the message.

**Do not add `git worktree remove` here.** No worktrees exist until S2, and
`git worktree remove` against a repo with none is S2's work.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_branch_entity_lifecycle.py`:

```python
from call_manager.proposal_assist.branches import BranchInUse


def test_delete_removes_the_row_with_the_ref(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="new_ideas",
            workspaces_dir=tmp_path,
        )
        s.commit()

    with pg_session_factory() as s:
        delete_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="new_ideas",
            workspaces_dir=tmp_path,
        )
        s.commit()

    assert not branch_exists(repo, "new_ideas")
    with pg_session_factory() as s:
        assert _rows(s, wid) == ["main"]


def test_delete_refuses_the_root_branch(pg_session_factory, tmp_path):
    """§7.3: the branch the root pointer names cannot be deleted. Raised as
    BranchInUse — the error a user can read — with the database RESTRICT as the
    backstop behind it."""
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="new_ideas",
            workspaces_dir=tmp_path,
        )
        s.get(Workspace, wid).current_branch = "new_ideas"  # so the OLD guard cannot fire
        s.commit()

    with pg_session_factory() as s:
        with pytest.raises(BranchInUse):
            delete_workspace_branch(
                s,
                stamp=stamp_for(s, wid),
                workspace_id=wid,
                name="main",
                workspaces_dir=tmp_path,
            )
        s.rollback()

    assert branch_exists(repo, "main"), "the ref was destroyed before the refusal"
    with pg_session_factory() as s:
        assert _rows(s, wid) == ["main", "new_ideas"]


def test_rows_and_refs_agree_after_a_delete(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        for name in ("new_ideas", "eu_version"):
            create_workspace_branch(
                s,
                stamp=stamp_for(s, wid),
                workspace_id=wid,
                name=name,
                workspaces_dir=tmp_path,
            )
        s.commit()

    with pg_session_factory() as s:
        delete_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="eu_version",
            workspaces_dir=tmp_path,
        )
        s.commit()

    with pg_session_factory() as s:
        assert _rows(s, wid) == sorted(list_branches(repo))


def test_deleting_a_branch_with_no_row_still_drops_the_ref(pg_session_factory, tmp_path):
    """A ref created before this stage — or by `git branch` outside the app — has
    no row. Delete must not raise on the missing row and strand the ref forever."""
    wid, repo = _ws(pg_session_factory, tmp_path)
    subprocess.run(["git", "branch", "orphan"], cwd=repo, check=True)

    with pg_session_factory() as s:
        delete_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="orphan",
            workspaces_dir=tmp_path,
        )
        s.commit()

    assert not branch_exists(repo, "orphan")
    with pg_session_factory() as s:
        assert _rows(s, wid) == ["main"]
```

Also append the API-level test, because this is the one intended behaviour change in the
whole plan and a unit-level `pytest.raises` does not prove what a user sees:

```python
def test_the_api_refuses_to_delete_the_root_branch(client_authed_owner, pg_session_factory, tmp_path):
    """The ONE deliberate behaviour change in this plan (see Global Constraints).
    Today a user who switched away from the root can delete it; §7.3 closes that.
    Pinned at the API because the response code is what actually changed — and
    `{name:path}` means the route itself is easy to get wrong.

    Mirrors the setup in `test_api_branches.py`; `client_authed_owner` is that
    file's fixture."""
    wid = _api_workspace(client_authed_owner, pg_session_factory, tmp_path)
    client_authed_owner.post(f"/api/workspaces/{wid}/branches", json={"name": "new_ideas"})
    client_authed_owner.post(
        f"/api/workspaces/{wid}/branches/switch", json={"name": "new_ideas"}
    )

    resp = client_authed_owner.delete(f"/api/workspaces/{wid}/branches/main")
    assert resp.status_code == 409, resp.text
```

**Before writing it, read `tests/proposal_assist/test_api_branches.py`** and reuse its
workspace-setup helper and its `client_authed_owner` fixture verbatim rather than
inventing `_api_workspace` — the name above is a placeholder for whatever that file
already calls it. Check what status `_raise_http` maps `BranchInUse` to and assert that
code; if it is not 409, assert the real one rather than changing the mapping.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/proposal_assist/test_branch_entity_lifecycle.py -q
```

Expected, per test:

| Test | Expected failure |
|---|---|
| `test_delete_removes_the_row_with_the_ref` | `assert ['main', 'new_ideas'] == ['main']` |
| `test_delete_refuses_the_root_branch` | `DID NOT RAISE <class '...BranchInUse'>` |
| `test_rows_and_refs_agree_after_a_delete` | `assert ['eu_version', 'main', 'new_ideas'] == ['main', 'new_ideas']` |
| `test_deleting_a_branch_with_no_row_still_drops_the_ref` | PASSES already — it is a control for the change, not a driver of it |
| `test_the_api_refuses_to_delete_the_root_branch` | `assert 204 == 409` — today the delete succeeds |

- [ ] **Step 3: Add the root guard and the row delete**

In `src/call_manager/proposal_assist/branches.py`, in `delete_workspace_branch`, replace:

```python
    row = session.get(Workspace, workspace_id)
    if row is not None and row.current_branch == name:
```

with:

```python
    row = session.get(Workspace, workspace_id)
    if row is not None and row.root_branch_id is not None:
        # §7.3: the branch the root pointer names cannot be deleted — its worktree
        # IS `repo/`. The database enforces this too (fk_workspaces_root_branch_id
        # is ON DELETE RESTRICT), but an IntegrityError is not an error a user can
        # read. Raise the readable one first and leave RESTRICT as the backstop.
        root = session.get(Branch, row.root_branch_id)
        if root is not None and root.name == name:
            raise BranchInUse(name)
    if row is not None and row.current_branch == name:
```

Then replace:

```python
    git_delete_branch(repo, name)
    logger.info(
        "branches.deleted",
        extra={"event": "branch_deleted", "workspace_id": str(workspace_id), "branch": name},
    )
```

with:

```python
    git_delete_branch(repo, name)
    # Drop the row with the ref (spec §8 S1 step 4). A ref created before this
    # stage — or with `git branch` outside the app — has no row; deleting nothing
    # is correct, not an error, and raising here would strand that ref forever.
    #
    # Not atomic, and this direction is the worse one: `git branch -D` is
    # irreversible, so a commit that fails after it leaves a row whose ref is
    # gone. `backfill_branch_entities` deletes such orphans — that is why it is a
    # reconciler and not a one-shot backfill.
    session.execute(
        delete(Branch).where(Branch.workspace_id == workspace_id, Branch.name == name)
    )
    logger.info(
        "branches.deleted",
        extra={"event": "branch_deleted", "workspace_id": str(workspace_id), "branch": name},
    )
```

Change the import at `branches.py:16` from:

```python
from sqlalchemy import select
```

to:

```python
from sqlalchemy import delete, select
```

(The file already aliases `update as sql_update` on the next line; `delete` collides with
nothing and needs no alias.)

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/proposal_assist/test_branch_entity_lifecycle.py -q
```

Expected: `8 passed`.

- [ ] **Step 5: Prove the root guard is not the old guard in disguise**

`test_delete_refuses_the_root_branch` sets `current_branch = "new_ideas"` precisely so the
pre-existing `current_branch == name` check cannot be what refuses. Confirm that: comment
out the new `root_branch_id` block and run:

```bash
uv run pytest tests/proposal_assist/test_branch_entity_lifecycle.py::test_delete_refuses_the_root_branch -q
```

Expected: FAIL with `DID NOT RAISE`. If it passes, the test is being satisfied by the old
guard and proves nothing about the root pointer. Restore the block and re-run.

- [ ] **Step 6: Run the existing branch suite for regressions**

```bash
uv run pytest tests/proposal_assist/test_branch_create_delete.py \
              tests/proposal_assist/test_api_branches.py \
              tests/proposal_assist/test_branch_switch.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
uv run ruff check src tests && uv run ruff format --check src tests
git add src/call_manager/proposal_assist/branches.py \
        tests/proposal_assist/test_branch_entity_lifecycle.py
git commit -m "feat(branches): delete drops the row and refuses the root

Spec §8 S1 step 4 and §7.3. The row goes with the ref so a row can never outlive
the branch it names; a ref with no row (created before this stage, or by git
outside the app) deletes cleanly rather than raising and stranding the ref.

The root refusal raises BranchInUse — the error a user can read — with the
database's ON DELETE RESTRICT as the backstop behind it. The test sets
current_branch to a DIFFERENT branch so the pre-existing current_branch guard
cannot be what refuses; verified by commenting the new block out."
```

---

### Task 6: Pin the invariants and regenerate the architecture map

**Files:**
- Create: `tests/architecture/test_branch_entity_invariants.py`
- Modify: `docs/ARCHITECTURE.md` (regenerated, never hand-edited)

**Interfaces:**
- Consumes: everything from Tasks 1–5.
- Produces: nothing the code imports. This task exists so S2 can rely on the two facts it
  resolves paths with.

**Why these two.** S2 resolves every filesystem path through `root_branch_id`, and S1's
whole promise is that a ref never exists without a row. Both are invariants that hold
today and could be silently broken by a later change; both are what a reviewer of S2 will
assume without checking.

- [ ] **Step 1: Write the failing test**

Create `tests/architecture/test_branch_entity_invariants.py`:

```python
"""The two facts S2 resolves paths with. Both hold after S1; neither is obvious.

Not a unit test of the backfill (that is
tests/proposal_assist/test_branch_entity_backfill.py) — these are the properties a
later stage assumes without checking.
"""

import subprocess

from sqlalchemy import select

from call_manager.db.models import Branch, Workspace
from call_manager.proposal_assist.branch_entity_backfill import backfill_branch_entities
from call_manager.proposal_assist.branches import create_workspace_branch
from call_manager.proposal_assist.citations.gitio import list_branches
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import init_workspace_repo, make_workspace, stamp_for


def test_every_ref_has_a_row_after_backfill_and_after_a_create(pg_session_factory, tmp_path):
    """S1's promise, over the whole set and across BOTH ways a branch appears.

    Asserted as set equality, not membership: 'new_ideas is in the rows' would
    pass with an orphan row beside it, and an orphan row is what sends a later
    path resolution to a directory that does not exist.
    """
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    repo = workspace_repo_dir(tmp_path, wid)
    init_workspace_repo(repo, "# Aims\n\nbody\n")
    subprocess.run(["git", "branch", "made_before_s1"], cwd=repo, check=True)

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="made_after_s1",
            workspaces_dir=tmp_path,
        )
        s.commit()

    with pg_session_factory() as s:
        rows = {
            n
            for (n,) in s.execute(select(Branch.name).where(Branch.workspace_id == wid)).all()
        }
    assert rows == set(list_branches(repo))


def test_a_workspace_with_a_repo_always_has_a_root_pointer(pg_session_factory, tmp_path):
    """S2 resolves `repo/` versus `worktrees/<id>/` through this pointer. A null
    one after backfill means every path for that workspace resolves nowhere."""
    with pg_session_factory() as s:
        a = make_workspace(s, title="a").id
        b = make_workspace(s, title="b").id
        s.commit()
    init_workspace_repo(workspace_repo_dir(tmp_path, a), "# A\n")
    init_workspace_repo(workspace_repo_dir(tmp_path, b), "# B\n")

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:
        for wid in (a, b):
            root_id = s.get(Workspace, wid).root_branch_id
            assert root_id is not None, f"workspace {wid} has a repo but no root pointer"
            assert s.get(Branch, root_id).workspace_id == wid


def test_the_root_pointer_never_names_another_workspaces_branch(pg_session_factory, tmp_path):
    """`root_branch_id` is a bare FK to `branches.id` with no workspace
    correlation in the constraint, so nothing in the schema stops a
    cross-workspace pointer — only the backfill's own scoping does.

    Asserted by loading each pointer and checking the branch it names BELONGS to
    the workspace holding it. `assert ra != rb` would not do: two SWAPPED pointers
    are still unequal, so that form passes on precisely the bug it names. Both
    workspaces use the same branch name, which is what a scoping bug would confuse.
    """
    with pg_session_factory() as s:
        a = make_workspace(s, title="a").id
        b = make_workspace(s, title="b").id
        s.commit()
    init_workspace_repo(workspace_repo_dir(tmp_path, a), "# A\n")
    init_workspace_repo(workspace_repo_dir(tmp_path, b), "# B\n")

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:
        for wid in (a, b):
            root = s.get(Branch, s.get(Workspace, wid).root_branch_id)
            assert root.workspace_id == wid, f"{wid}'s root belongs to {root.workspace_id}"
```

- [ ] **Step 2: Run the tests to verify they pass**

Unlike the earlier tasks these should pass immediately — they pin behaviour Tasks 1–5
already built.

```bash
uv run pytest tests/architecture/test_branch_entity_invariants.py -q
```

Expected: `3 passed`.

- [ ] **Step 3: Prove each invariant test bites**

A gate that reddens nothing is a finding, not a pass. Run both mutations and record the
result of each:

**Mutation A** — in `branch_entity_backfill.py`, change the ref loop to found only the
checked-out branch:

```python
        for name in [head]:
```

Expected: `test_every_ref_has_a_row_after_backfill_and_after_a_create` FAILS.

**Mutation B** — in `branch_entity_backfill.py`, remove the root pointer write by deleting
the `session.execute(update(Workspace)...)` call.

Expected: `test_a_workspace_with_a_repo_always_has_a_root_pointer` FAILS with
`workspace ... has a repo but no root pointer`.

Revert both. If either mutation leaves the suite green, the test is hollow — fix the test
before continuing.

- [ ] **Step 4: Confirm the architecture map is still current**

Task 1 regenerated it when the model changed. Tasks 2-5 touched no model, so this should
be a no-op — confirm rather than assume:

```bash
uv run python scripts/gen_architecture.py
git diff --stat docs/ARCHITECTURE.md
```

Expected: **empty diff.** A non-empty diff here means a model changed after Task 1 without
the map being regenerated with it; commit the regenerated map with this task.

- [ ] **Step 5: Run the full suite**

```bash
uv run pytest -n 16 --dist loadfile
```

Expected: all pass with **`1 skipped`**. The skip is the signal, not the total — it is a
real-SDK test gated on `ANTHROPIC_API_KEY`. `0 skipped` means a live API call ran. The
pass total moves with every slice; do not read a different total as a broken suite.

If a testcontainers Postgres drops under load ("connection refused"), that is
infrastructure, not this change — re-run the failing file alone to confirm.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src tests && uv run ruff format --check src tests
git add tests/architecture/test_branch_entity_invariants.py
git commit -m "test(branches): pin the two invariants S2 resolves paths with

Every ref has a row, across BOTH ways a branch appears (backfilled and created),
asserted as set equality — membership would pass with an orphan row beside it,
and an orphan row sends a later path resolution to a directory that does not
exist.

Every workspace with a repo has a root pointer, and it names its own workspace's
branch. root_branch_id is a bare FK with no workspace correlation, so nothing in
the schema stops a cross-workspace pointer; only the backfill's scoping does.

Both mutation-checked: founding only the checked-out branch reddens the first,
removing the root write reddens the second."
```

---

## Done means

- `branches` exists with `(id, workspace_id, name, created_at, last_sdk_session_id)` and
  `uq_branches_ws_name`.
- `workspaces.root_branch_id` exists, is distinct from `current_branch`, and is
  `ON DELETE RESTRICT`.
- Migration `0068` founds one row per existing local ref and sets every root pointer, and
  `alembic upgrade / downgrade / upgrade` is clean.
- `create_workspace_branch` and `delete_workspace_branch` keep rows and refs in agreement.
- Deleting the root branch raises `BranchInUse`, proved not to be the old
  `current_branch` guard.
- Full suite green with **`1 skipped`**; `docs/ARCHITECTURE.md` regenerated.
- **One behaviour change, and only one:** the API refuses to delete the root branch,
  pinned at the API level. Every other route and response body unmoved —
  `test_api_branches.py` passes apart from any case that deleted the root.

## Deliberately NOT in this plan

- Any `checkout_branch` change (`branches.py:226`, `branches.py:124`,
  `checkpoint.py:85`) — S2 removes all three together.
- Any `git worktree` call — S2.
- The branch-id column on the branch-scoped tables — S1 step 5, its own plan.
- Branch-aware `workspace_repo_dir` at the 58 call sites — S1 step 6, its own plan.
- Dropping `workspaces.last_sdk_session_id` or `workspaces.current_branch` — S4, when
  their last readers go.
