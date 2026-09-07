# Board Auto-Publish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a corridor board publish ranked calls without a human ticking each one, per board, with an immediate takedown that survives the nightly job.

**Architecture:** One migration adds a flag, a suppression table, and makes `approved_by` nullable. The candidate filters are extracted from `board_queue` so the new selection cannot drift from the admin queue. A nightly job reconciles each auto board's items under the same row lock every other writer takes, encoding QRs outside the transaction. The settings screen gains the flag and a four-number preview.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, Alembic, PostgreSQL 16, APScheduler, pytest (+ testcontainers Postgres), React 19 + Vite + TanStack Query.

**Spec:** `docs/superpowers/specs/2026-09-06-board-auto-publish-design.md` **rev 2**. The spec is INPUT. If it is wrong, that is a `PLAN-DEFECTS.md` entry, not an edit.

## Global Constraints

- **Base:** `master` at `99590b97`.
- **Every "verified at `bf5a64a4`" citation below is equally true at `99590b97`.** Measured:
  `git diff --stat bf5a64a4..99590b97` is **one file, this plan** (1389 insertions). The two
  trees are identical under `src/`, `tests/`, `alembic/` and `frontend/`, so the citations were
  not re-checked one by one — the tree they were taken against did not move.
- **Measured baseline: `5428 passed, 1 skipped`**, re-measured on `99590b97` at 152.53s.
  Re-measure before asserting a total.
- **`1 skipped` stays exactly 1.** `0 skipped` means a live Anthropic call ran — stop and
  record it. Never export `ANTHROPIC_API_KEY`.
- **KNOWN BROKEN, NOT YOURS:**
  `tests/proposal_assist/test_turns_submit.py::test_concurrent_submits_cannot_overshoot_cap`
  fails deterministically when run alone or as a file (`got ['ok', 'ok']`), and passes under
  `-n 16` because the timing changes. Measured on master at `99590b97` with none of this
  plan's code, 3 runs of 3. **Do not chase it, do not fix it, do not let it block a task.**
  If your `-n 16` run shows it red, that is this test, not your change.
- **Never read, write, copy or back up `/home/parlar_ai/dev/grant-lab/.env`.** Set any env var
  inline in the one command that needs it.
- **`docs/superpowers/specs/` byte-identical at every commit.**
- **The new migration is `0078`.** Head is `0077` (`0077_board_item_qr.py`), verified by listing
  `alembic/versions/`. One head at the end.
- **`docs/ARCHITECTURE.md` is generated.** `uv run python scripts/gen_architecture.py` in the
  same commit as any model change. Never hand-edit.
- **Gates:** `uv run pytest -n 16 --dist loadfile` (never `-n auto`, never `--dist loadgroup`);
  `uv run ruff check src tests alembic scripts`. Never `ruff format` a directory.
  Frontend: `cd frontend && pnpm run lint && pnpm run test`.
- **False-pass signature:** a broken import shows as mass teardown ERRORs with zero FAILED.
  **Judge every mutation on FAILED, never on ERROR.** `no tests ran` is also not a pass.
- **A fresh worktree needs `uv sync --python 3.12`, and `cd frontend && pnpm install`**
  (`node_modules` is gitignored; "Command vitest not found" means the install is missing).

### Three constraints the dry run added. Each one bit at least twice.

- **Every code block in this plan has been through `uv run ruff check`.** The first draft's
  blocks had not been, and all five test/migration blocks failed the task's OWN Step 6 gate
  (unused imports, import ordering, `E501` at the repo's 100-char limit). They are fixed below.
  If you edit a block, re-run ruff on the file before you commit — do not "fix" it by widening
  the line-length or adding a `noqa`.

- **This repo has PINNED-MANIFEST tests, and they are invisible to a Files list that only names
  the code.** A model column reddens `tests/test_models_boards.py::test_board_columns_match_the_spec`
  (it asserts `Board`'s complete column set); a route reddens **three** things in
  `tests/architecture/`. Both bit in the dry run, and both surfaced a task or two later,
  attributed to the wrong change. Task 1 and Task 6 now list them explicitly. **If you add a
  column or a route that this plan does not describe, go and find its manifest before you
  commit.**

- **NEVER restore a mutation with `git checkout -- src/`.** Step 5 of Tasks 3, 4 and 5 runs
  BEFORE Step 6 commits, so at that moment the implementation lives only in the working tree:
  `git checkout -- src/` reverts to the PREVIOUS task's commit and wipes the whole fix, not just
  the mutant. Every later mutation is then applied to code that no longer has the feature, so
  every later verdict is meaningless — and `git status --porcelain` comes back **empty**, which
  is exactly the "clean" signal the first draft told you to check for. The correct loop:

  ```bash
  # once, before the first mutation:
  git -C <worktree> stash push -u -m "pre-mutation" && git -C <worktree> stash pop   # no-op sanity
  cp src/grant_lab/<file>.py /tmp/pre-mutation-<file>.py     # the real safety net
  # per mutation:
  #   edit the ONE file, run the named test, record FAILED/PASSED
  cp /tmp/pre-mutation-<file>.py src/grant_lab/<file>.py     # restore
  git -C <worktree> diff HEAD --stat                          # MUST be NON-EMPTY: the fix is back
  ```

  **The check is `git diff HEAD --stat` being non-empty, not `git status --porcelain` being
  empty.** An empty porcelain here means you deleted your own work.

## Fixtures — the verified list. Do not re-derive this.

All defined in the ROOT `tests/conftest.py`, so visible to every test under `tests/`:

- `pg_url` — `tests/conftest.py:117`
- `pg_session_factory` — `tests/conftest.py:151`
- `profile_factory` — `tests/conftest.py:184`, returns `(profile_id, owner_user_id)`
- `migrated_engine` — `tests/conftest.py:138`, the engine at head. Task 1 Step 5 needs it.

**There is NO `ranked_call_factory` fixture in this repo.** The first draft gave four Task 4
signatures that took one; pytest reports a missing fixture as an **ERROR**, not a FAILED, which
is this suite's false-pass signature. Task 4 writes a plain helper function instead — see there.

`client_unauth` is defined at `tests/proposal_assist/conftest.py`, is **package-scoped, and is
NOT available here.** Board tests build their own app and client. Copy the idiom from
`tests/test_boards_qr_at_approval.py:29-64`: `_setup(monkeypatch, pg_url)` returns an app,
`_client_for(app, uid)` returns a cookie-authed `TestClient`, `_make_board(...)` and
`_make_call(...)` insert rows directly. Reuse those helpers by importing them or copying them —
do not invent fixture names.

## File structure

| File | Responsibility | Task |
|---|---|---|
| `alembic/versions/0078_board_auto_publish.py` | the flag, the suppression table, nullable `approved_by` | 1 |
| `src/grant_lab/db/models/boards.py` | `Board.auto_publish`, `BoardSuppression`, `BoardItem.approved_by` nullable | 1 |
| `src/grant_lab/web/api/boards.py` | `BoardItemOut` nullable; suppression on unapprove; clear on approve; `auto_publish` setting; preview endpoint | 2, 3, 6 |
| `src/grant_lab/boards/queue.py` | extract the shared candidate filters | 4 |
| `src/grant_lab/boards/auto_publish.py` | the selection and the reconcile | 4, 5 |
| `src/grant_lab/scheduler.py` | register the 05:00 job | 5 |
| `frontend/src/routes/BoardDetail.tsx` | the toggle and the four-number preview | 6 |
| `frontend/src/api/schema.gen.ts` | regenerated, never hand-edited | **0**, 2, 6 |
| `tests/test_models_boards.py` | **pinned** `Board` column set — a new column reddens it | 1 |
| `tests/architecture/test_boards_permission_single_source.py` | **pinned** boards route manifest | 6 |
| `tests/architecture/test_edition_surface.py` | **pinned** route COUNT (`117`) and its ledger | 6 |
| `tests/architecture/full_edition_routes.txt` | **pinned** route baseline, regenerated | 6 |

---

### Task 0: land the pre-existing `schema.gen.ts` drift on its own

**This task fixes nothing in this feature. It exists so the next five commits are readable.**

**Files:**
- Modify: `frontend/src/api/schema.gen.ts` (regenerated, never hand-edited)

**Why, measured at `99590b97`.** `frontend/src/api/schema.gen.ts` is **already stale on
`master`**, before a line of this plan is written:

```
$ git log -1 --format='%h %s' 99590b97 -- src/grant_lab/web/api/profiles.py
4f11b1b7 fix(review): correct four claims this branch itself made false
$ git log -1 --format='%h %s' 99590b97 -- frontend/src/api/schema.gen.ts
84b6ebd9 fix(boards): bound QR-encode cost by CPU/bytes, ...
$ grep -c 'update_profile` inlines the same ladder' src/grant_lab/web/api/profiles.py
1
$ grep -c 'update_profile` inlines the same ladder' frontend/src/api/schema.gen.ts
0
```

`4f11b1b7` rewrote `transfer_profile_owner`'s docstring three commits AFTER the types were last
generated, and `.github/workflows/ci.yml` runs `bash scripts/gen_openapi_types.sh` followed by
`git diff --exit-code frontend/src/api/schema.gen.ts`. **So `master` is red on that CI gate
today.** Without this task the ~14 lines of unrelated profiles-docstring churn land inside
Task 2's commit, and a reviewer reading a boards commit finds a profiles change in it.

- [ ] **Step 1: Regenerate and look at what moves**

```bash
cd <worktree>/frontend && pnpm install     # node_modules is gitignored
pnpm run gen:api
git -C <worktree> diff --stat -- frontend/src/api/schema.gen.ts
```

`pnpm run gen:api` delegates to `scripts/gen_openapi_types.sh`, which **already pins
`EMBEDDING_MODEL=bge-m3`, `SPA_MODE=disabled`, `APP_EDITION=full` and `--python 3.12` inside
itself** — read its header comment for why each one. Do **not** prefix the command with
`EMBEDDING_MODEL=...`; the script overrides you anyway and the prefix implies a dependency that
does not exist. The script needs **no** database and **no** `.env`: it falls back to a dummy
`DATABASE_URL` and builds the schema from the route definitions.

Expected: the only hunk is `transfer_profile_owner`'s description. **If any route path or
schema NAME moves, stop** — that is not pre-existing drift, and this task must not absorb it.

- [ ] **Step 2: Commit it alone**

```bash
git -C <worktree> add frontend/src/api/schema.gen.ts
git -C <worktree> commit -m "chore(api): regenerate schema.gen.ts, stale since 4f11b1b7"
git -C <worktree> diff --stat 99590b97..HEAD    # MUST be exactly one file
```

No test-count change: `schema.gen.ts` is a type file, nothing imports it at runtime.
Baseline stays `5428 passed, 1 skipped`.

**After this, `schema.gen.ts:2821`'s `approved_by` moves to `:2825`.** Task 2 cites the NAME,
not the line, for that reason.

---

### Task 1: the migration and the models

**Files:**
- Create: `alembic/versions/0078_board_auto_publish.py`
- Modify: `src/grant_lab/db/models/boards.py`
- Modify: `docs/ARCHITECTURE.md` (generated)
- Modify: **`tests/test_models_boards.py`** — see the blast-radius note below
- Test: `tests/test_migration_0078_board_auto_publish.py`

**Interfaces:**
- Produces: `Board.auto_publish: Mapped[bool]` (default `False`);
  `BoardItem.approved_by: Mapped[int | None]`;
  new model `BoardSuppression` with `board_id`, `call_id`, `suppressed_by`, `suppressed_at`,
  composite PK `(board_id, call_id)`, table name `board_suppressions`.

**Why `approved_by` becomes nullable.** Verified at `bf5a64a4`: it is
`nullable=False, ForeignKey("users.id", ondelete="RESTRICT")` at
`src/grant_lab/db/models/boards.py:168`. A card nobody approved has no approver; inventing a
system user would make the audit trail lie. `NULL` means published automatically and stays that
way for the life of the row. `approved_at` stays `NOT NULL`.

**BLAST RADIUS — `tests/test_models_boards.py` pins `Board`'s exact column set.**
`test_board_columns_match_the_spec` asserts `{c.name for c in inspect(Board).columns}` equals a
15-name literal. Adding `auto_publish` turns the full suite from clean to `1 failed`, and it is
**not** caught by `tests/architecture -q`. In the dry run this surfaced two tasks later, at
Task 3's full-suite run, and was attributed to Task 3. Step 1 below repairs the literal in the
same breath as writing the new tests, and Step 6 runs that file explicitly.

**`suppressed_by` keeps `ON DELETE RESTRICT`. This is a decision, not an inheritance.**
The spec's open item 4 asks the plan to answer this rather than inherit it, so:

RESTRICT, deliberately, for three reasons. (1) It **mirrors `board_items.approved_by`**, which
is `RESTRICT` and stays `RESTRICT` even as it becomes nullable in this same migration — nothing
here relaxes who may be deleted, it only adds a value meaning "nobody decided". A suppression
that is *less* protective of its author than the approval beside it would be the inconsistency.
(2) A takedown is a human decision on an audit surface; `SET NULL` would silently convert
"Anna took this down" into "somebody took this down", which is the same lie the plan refuses to
tell with a system user. (3) **Measured at `99590b97`: this repo has no hard user-delete path.**
`grep -rn 'router.delete("/users|session.delete(user|DELETE FROM users' src/` returns nothing —
users are deactivated via `User.status`, not deleted. So the named consequence is real but
currently unreachable.

**The named consequence, stated so it is not a surprise:** if a hard user-delete is ever added,
a user who has taken any card off any board cannot be deleted until their `board_suppressions`
rows are removed, and this plan ships no UI to remove them (the spec puts "un-suppressing from
a list" out of scope). Whoever builds that delete path owns clearing the rows, exactly as they
will already own `board_items.approved_by`.

- [ ] **Step 1: Write the failing test, and repair the pinned column set**

**FIRST**, in `tests/test_models_boards.py::test_board_columns_match_the_spec`, add
`"auto_publish",` to the literal set (it sits beside `"page_seconds",`). That test pins
`Board`'s complete column set; without this edit Task 1 leaves the suite at `1 failed` and the
break gets blamed on Task 3. It is a repair, not a new test — the count below reflects that.

**THEN** create `tests/test_migration_0078_board_auto_publish.py`:

```python
"""Migration 0078: the auto-publish flag, the suppression table, and an
approver that may be absent."""

from __future__ import annotations

from sqlalchemy import inspect, text

_BOARD_INSERT = text(
    "INSERT INTO boards (slug, title, profile_id, created_by, is_published, "
    "orientation, items_per_page, max_pages, page_seconds) "
    "VALUES (:slug, 't', :p, :u, false, 'landscape', 8, 3, 30)"
)


def test_the_flag_defaults_off_for_every_existing_board(pg_session_factory, profile_factory):
    """Nothing that exists today changes behaviour."""
    pid, owner = profile_factory(name="0078 default profile")
    slug = "a" * 16
    with pg_session_factory() as s:
        s.execute(_BOARD_INSERT, {"slug": slug, "p": pid, "u": owner})
        s.commit()
        got = s.scalar(text("SELECT auto_publish FROM boards WHERE slug = :slug"), {"slug": slug})
    assert got is False


def test_approved_by_accepts_null(pg_session_factory, profile_factory):
    """The whole point: an auto-published row has no approver."""
    pid, owner = profile_factory(name="0078 null approver profile")
    slug = "b" * 16
    with pg_session_factory() as s:
        s.execute(_BOARD_INSERT, {"slug": slug, "p": pid, "u": owner})
        bid = s.scalar(text("SELECT id FROM boards WHERE slug = :slug"), {"slug": slug})
        src = s.scalar(text("SELECT id FROM sources WHERE slug = 'vinnova'"))
        s.execute(
            text(
                "INSERT INTO calls (source_id, external_id, url, title, deadline) VALUES "
                "(:s, 'x-0078', 'https://example.org/0078', 't', now() + interval '30 days')"
            ),
            {"s": src},
        )
        cid = s.scalar(text("SELECT id FROM calls WHERE external_id = 'x-0078'"))
        s.execute(
            text(
                "INSERT INTO board_items (board_id, call_id, position, approved_by, qr_modules) "
                "VALUES (:b, :c, 0, NULL, 0)"
            ),
            {"b": bid, "c": cid},
        )
        s.commit()
        got = s.scalar(
            text("SELECT approved_by FROM board_items WHERE board_id = :b"), {"b": bid}
        )
    assert got is None


def test_the_suppression_table_exists_with_a_composite_key(pg_session_factory):
    """A call is suppressed on ONE board, so (board_id, call_id) is the key."""
    with pg_session_factory() as s:
        insp = inspect(s.get_bind())
        assert "board_suppressions" in insp.get_table_names()
        pk = insp.get_pk_constraint("board_suppressions")["constrained_columns"]
    assert sorted(pk) == ["board_id", "call_id"]
```

**`grant_lab.*` and `tests.*` sit in ONE import block with no blank line between them.**
Both are first-party to ruff's isort here (there is no `src` override in `pyproject.toml`, and
`tests/test_api_spec_lock.py` shows the canonical layout). A blank line between them is `I001`,
which fails every task's own `ruff check` gate. **Measured** by writing each of this plan's
blocks to its real path in the worktree and running ruff there — running ruff on a copy in
`/tmp` gives the WRONG answer, because ruff decides first-party from the file's path.

**Two things in that block are load-bearing and were wrong in the first draft (both measured).**

1. **The slug is a BOUND PARAMETER, never `'a'*16` inside the SQL string.** `'a' * 16` is
   Python; inside a `text()` string Postgres parses it as *multiplication*, casts `'a'` to
   integer and raises
   `psycopg.errors.InvalidTextRepresentation: invalid input syntax for type integer: "a"`.
   The first draft did exactly that, and Step 2 duly printed `3 failed` — **red that proves
   nothing about the feature**, on the one step whose whole purpose is to see the right red.
   (`repeat('a', 16)` also works; a bound parameter is clearer and is the idiom Tasks 3-6 use.)
2. **`select` is NOT imported.** The first draft imported it and never used it: ruff `F401`,
   and `uv run ruff check src tests alembic scripts` is this task's own Step 6 gate.

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_migration_0078_board_auto_publish.py -v`

Expected, and each one is a **different** red — check all three, not the count:

| Test | Expected failure |
|---|---|
| `test_the_flag_defaults_off_for_every_existing_board` | `UndefinedColumn: column "auto_publish" does not exist` |
| `test_approved_by_accepts_null` | `NotNullViolation` on `board_items.approved_by` |
| `test_the_suppression_table_exists_with_a_composite_key` | `assert 'board_suppressions' in [...]` |

**`invalid input syntax for type integer: "a"` means you pasted `'a'*16` into the SQL string.**
That is a SQL parse error, not the feature failing — go back to Step 1. Likewise, an ERROR on a
missing fixture is the false-pass signature: check the fixture list above, do not invent one.

Also run `uv run pytest tests/test_models_boards.py -q` now — it must be **green**, because
Step 1 already repaired the pinned set. If it is red, the repair did not land.

- [ ] **Step 3: Write the migration**

```python
"""board auto-publish: the flag, suppressions, and an optional approver

DOWNGRADE HAZARD: see `downgrade()`. Dropping `board_suppressions` destroys
every takedown decision, and the next 05:00 run re-publishes every card an
operator had taken off a wall. This caveat lives in the migration rather than
in the plan because this repo prunes executed plans (CLAUDE.md) -- 0077
carries its own hazard the same way.

Revision ID: 0078
Revises: 0077
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "boards",
        sa.Column(
            "auto_publish",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # `approved_by` becomes optional. NULL means "published automatically" and
    # is the only thing that distinguishes an auto card from a human-approved
    # one for the life of the row -- so this is not merely relaxing a
    # constraint, it is adding a value with meaning.
    op.alter_column("board_items", "approved_by", existing_type=sa.BigInteger(), nullable=True)
    op.create_table(
        "board_suppressions",
        sa.Column("board_id", sa.BigInteger(), nullable=False),
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column("suppressed_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "suppressed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["board_id"], ["boards.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["suppressed_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("board_id", "call_id"),
    )


def downgrade() -> None:
    # DOWNGRADE HAZARD, stated here because this repo prunes executed plans and
    # a migration must carry its own caveats: dropping board_suppressions
    # DESTROYS every takedown decision. Re-upgrading gives an empty table, and
    # the next 05:00 run re-publishes every call an operator had taken off a
    # wall. Export the table before downgrading in an environment that has
    # been live.
    op.drop_table("board_suppressions")
    # An auto board's rows have approved_by = NULL and cannot satisfy NOT NULL.
    # Delete them: they are derived data the next run rebuilds, whereas
    # inventing an approver would write a false audit record.
    op.execute("DELETE FROM board_items WHERE approved_by IS NULL")
    op.alter_column("board_items", "approved_by", existing_type=sa.BigInteger(), nullable=False)
    op.drop_column("boards", "auto_publish")
```

- [ ] **Step 4: Mirror it in the ORM**

In `src/grant_lab/db/models/boards.py`, add to `Board` (beside `page_seconds`):

```python
    # Off by default: every board that exists when 0078 lands keeps the
    # approval gate. Turning this on replaces the board's hand-picked items on
    # the next 05:00 run -- see the spec's "Flipping the switch".
    auto_publish: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
```

Change `BoardItem.approved_by` (currently line 168) to:

```python
    # NULL means published automatically by boards/auto_publish.py. Inventing a
    # system user to keep this NOT NULL would make the audit trail lie about
    # who decided. The value survives a board returning to manual mode.
    approved_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
```

Add the new model after `BoardItem`:

```python
class BoardSuppression(Base):
    """One call an operator took off ONE board.

    This is an EXCEPTION list, not a second approval queue. It exists because
    an auto board rebuilds itself at 05:00, so deleting a row is not a
    takedown on its own -- the next run would put it back. It is written by
    `unapprove_item` and cleared by `approve_items`, which is a newer and
    stronger signal from the same human.

    Scoped to one board on purpose. Archiving a call reaches every board on
    that profile AND the owner's dashboard, and takes effect only at the next
    run; both are wrong for "this one card should not be on this one wall".
    """

    __tablename__ = "board_suppressions"

    board_id: Mapped[int] = mapped_column(
        ForeignKey("boards.id", ondelete="CASCADE"), primary_key=True
    )
    call_id: Mapped[int] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), primary_key=True
    )
    suppressed_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    suppressed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

Export it from `src/grant_lab/db/models/__init__.py` alongside `BoardItem`.

- [ ] **Step 5: Run the tests and the migration round trip**

```bash
uv run pytest tests/test_migration_0078_board_auto_publish.py -v   # 3 passed (4 after below)
uv run alembic heads                                               # expect exactly: 0078 (head)
```

**Do NOT run the round trip from the shell.** `uv run alembic upgrade head` needs a
`DATABASE_URL`, a fresh worktree has none, and the only place one exists is
`/home/parlar_ai/dev/grant-lab/.env` — **which you must never read.** (`alembic heads` is fine:
it reads `alembic.ini` and touches no database.)

Run it inside pytest against the `pg_url` fixture instead, which is the idiom this repo already
uses — copy `tests/test_migration_0038_reference_tombstone.py` almost verbatim. Append to the
new test file:

```python
def test_0078_round_trip(migrated_engine, pg_url):
    """pg_url is the SHARED session container: this downgrades then upgrades,
    so it MUST leave the schema at head for later tests -- done in `finally`."""
    engine = create_engine(pg_url)
    try:
        with engine.connect() as conn:
            assert "auto_publish" in {c["name"] for c in inspect(conn).get_columns("boards")}

        # SEED OUR OWN auto row. Do not rely on `test_approved_by_accepts_null`
        # having run first: that is definition order on one xdist worker today,
        # and a silent no-op the day the file is reordered or split. A downgrade
        # that deletes nothing proves nothing about the DELETE.
        with engine.begin() as conn:
            pid = conn.execute(text("SELECT id FROM profiles LIMIT 1")).scalar()
            uid = conn.execute(text("SELECT created_by FROM profiles WHERE id = :p"),
                               {"p": pid}).scalar()
            conn.execute(_BOARD_INSERT, {"slug": "c" * 16, "p": pid, "u": uid})
            bid = conn.execute(text("SELECT id FROM boards WHERE slug = :s"),
                               {"s": "c" * 16}).scalar()
            src = conn.execute(text("SELECT id FROM sources WHERE slug = 'vinnova'")).scalar()
            conn.execute(
                text(
                    "INSERT INTO calls (source_id, external_id, url, title, deadline) VALUES "
                    "(:s, 'x-0078rt', 'https://example.org/rt', 't', now() + interval '30 days')"
                ),
                {"s": src},
            )
            cid = conn.execute(
                text("SELECT id FROM calls WHERE external_id = 'x-0078rt'")
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO board_items (board_id, call_id, position, approved_by, "
                    "qr_modules) VALUES (:b, :c, 0, NULL, 0)"
                ),
                {"b": bid, "c": cid},
            )
            before = conn.execute(
                text("SELECT count(*) FROM board_items WHERE approved_by IS NULL")
            ).scalar()
        assert before > 0, "the seed did not land; the DELETE below would prove nothing"

        command.downgrade(_cfg(pg_url), "0077")
        with engine.connect() as conn:
            cols = {c["name"] for c in inspect(conn).get_columns("boards")}
            assert "auto_publish" not in cols
            assert "board_suppressions" not in inspect(conn).get_table_names()
    finally:
        # ALWAYS restore head, even if an assertion above failed -- later tests
        # in this session reuse the shared container at head.
        command.upgrade(_cfg(pg_url), "head")
        engine.dispose()
```

and the file's import block **REPLACED** by the following. This is the whole final block — do
not paste it *beside* Step 1's `from sqlalchemy import inspect, text`, which would be ruff
`F811 Redefinition of unused 'inspect'` twice over. Note the blank line: `alembic` is
first-party here, `sqlalchemy` is not.

```python
from __future__ import annotations

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg
```

**Measured in the dry run:** the round trip works, including the
`DELETE FROM board_items WHERE approved_by IS NULL`, the subsequent
`alter_column(nullable=False)`, the `board_suppressions` drop, and the re-upgrade.

This adds a **4th** test to the file, so re-run it and expect **4 passed** before Step 6. The
delta there counts it.

- [ ] **Step 6: Regenerate the architecture map and commit**

```bash
uv run python scripts/gen_architecture.py
uv run pytest tests/architecture -q          # expect: 192 passed (measured, right)
uv run pytest tests/test_models_boards.py -q # MUST be green -- the pinned column set
uv run ruff check src tests alembic scripts
uv run pytest -n 16 --dist loadfile          # DELTA +4 on 5428 -> expect 5432, EXACTLY 1 skipped
git add -A && git commit -m "feat(boards): a board can publish itself, and a card can be kept off one"
```

**Why the full suite here and not just `tests/architecture`.** `tests/architecture` cannot see
`tests/test_models_boards.py`, which is the file this task breaks. Running only the narrow gate
is how the dry run pushed that failure two tasks downstream.

**`+4` is measured, not predicted:** 3 new migration tests + the round trip = 4 added; the
pinned-column test is *repaired*, not added, so it contributes 0. The dry run measured
`5428 -> 5431` for the 3-test version of this task; the round trip is the 4th.

---

### Task 2: `approved_by` may be absent in the API too

**Files:**
- Modify: `src/grant_lab/web/api/boards.py:458` (`BoardItemOut.approved_by`)
- Modify: `frontend/src/api/schema.gen.ts` (regenerated)
- Modify: `frontend/src/routes/BoardDetail.test.tsx` (fixtures)
- Test: `tests/web/test_api_board_items.py` (add one)

**Interfaces:**
- Consumes: `BoardItem.approved_by: int | None` from Task 1.
- Produces: `BoardItemOut.approved_by: int | None`.

**Why this is its own task.** Task 1 alone ships a column that the response model rejects.
Verified at `bf5a64a4`: `BoardItemOut.approved_by` is typed `int` (`boards.py:458`) and
`_item_out` passes `item.approved_by` straight into it (`:466-478`). An auto board's item list
would fail pydantic validation on the way out — a 500 on a GET, with the cause in the response
model rather than anywhere near the job that wrote the row.

**Blast radius, measured.** `approved_by` appears in the frontend ONLY in
`frontend/src/api/schema.gen.ts` (typed `number`, inside the `BoardItemOut` schema) and in
`BoardDetail.test.tsx` fixtures. **No component renders it.** So this is a regeneration and a
fixture update, not a UI change.

**Cited by NAME, not by line.** The first draft said `schema.gen.ts:2821`. That was correct
when written and became a trap the moment **Task 0** regenerated the file — the declaration
moves to roughly `:2825`. Search for `BoardItemOut` and then `approved_by` within it.

**Task 0 must be committed before this task**, or the ~14 lines of unrelated
`transfer_profile_owner` docstring churn land inside this commit.

- [ ] **Step 1: Write the failing test**

Append to `tests/web/test_api_board_items.py`, in that module's own idiom (read the file first
and reuse whatever it already uses to build an app and a client):

```python
def test_an_item_with_no_approver_serializes(
    pg_session_factory, profile_factory, monkeypatch, pg_url
):
    """GATE. An auto-published row has approved_by = NULL. Listing items must
    return it, not 500 in the response model."""
    from sqlalchemy import text

    # MEASURED: this module has `_setup`, `_client_for`, `_create_board` (via
    # the API, needs a client, returns the RESPONSE not an id) and `_make_call`
    # (which DERIVES the url as https://example.org/<external_id> and takes NO
    # url kwarg -- that is the OTHER `_make_call`, in
    # tests/test_boards_qr_at_approval.py, which REQUIRES url=). This module
    # has no `_make_board`. Use what is there; the two `_make_call`s are not
    # interchangeable.
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="null approver api profile")
    setup_client = _client_for(app, owner)
    with setup_client:
        # `_create_board` returns the RESPONSE, not an id (:72-77).
        created = _create_board(setup_client, profile_id=pid)
        assert created.status_code == 201, created.text
        board_id = created.json()["id"]
    call_id = _make_call(pg_session_factory, external_id="null-appr")
    with pg_session_factory() as s:
        s.execute(
            text(
                "INSERT INTO board_items (board_id, call_id, position, approved_by, qr_modules) "
                "VALUES (:b, :c, 0, NULL, 0)"
            ),
            {"b": board_id, "c": call_id},
        )
        s.commit()

    client = _client_for(app, owner)
    with client:
        r = client.get(f"/api/boards/{board_id}/items")
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["approved_by"] is None
```

- [ ] **Step 2: Run and watch it fail**

Run the **node id**, not a `-k` guess:

```bash
uv run pytest "tests/web/test_api_board_items.py::test_an_item_with_no_approver_serializes" -v
```

Expected: FAIL with a pydantic `ValidationError` on `approved_by` — `Input should be a valid
integer`. A 500 in the response body is the same failure; either is the red you want. An ERROR
about a missing `_setup`/`_make_board` means you did not reuse this module's helpers.

**Do NOT use `-k null_approver`.** The test is named `..._no_approver_...`, so that pattern
matches nothing: pytest deselects all 24 tests in the file and exits **0** with
`24 deselected`. Run verbatim, the "watch it fail" step prints a green, zero-test run — this
suite's documented `no tests ran` false-pass, on the one step whose purpose is to see red.
Measured in the dry run. The node id cannot do this: a typo in it is a hard collection error.

- [ ] **Step 3: Widen the type**

At `src/grant_lab/web/api/boards.py:458`:

```python
    # NULL when boards/auto_publish.py published this row. The column went
    # nullable in 0078 and this MUST move with it: _item_out passes the value
    # straight through, so leaving this `int` turns every auto board's item
    # list into a 500 raised inside the response model, far from the job that
    # wrote the row.
    approved_by: int | None
```

- [ ] **Step 4: Run and watch it pass, then regenerate the client types**

```bash
uv run pytest "tests/web/test_api_board_items.py::test_an_item_with_no_approver_serializes" -v
cd <worktree>/frontend && pnpm run gen:api
git -C <worktree> diff --stat -- frontend/src/api/schema.gen.ts
```

Expected: PASS; `schema.gen.ts` shows `approved_by: number | null`, **and that hunk is the only
one** — because Task 0 already absorbed the profiles drift. If you see `transfer_profile_owner`
in this diff, Task 0 was skipped; go back and do it.

`pnpm run gen:api` delegates to `scripts/gen_openapi_types.sh`, which pins `EMBEDDING_MODEL`,
`SPA_MODE`, `APP_EDITION` and the interpreter **inside itself**. Do not prefix it with
`EMBEDDING_MODEL=bge-m3` — the script overrides you, and the prefix implies a dependency that
does not exist. It needs no database and no `.env`.

- [ ] **Step 5: Update the frontend fixtures and commit**

`BoardDetail.test.tsx` sets `approved_by: 1` in four places (`:268`, `:285`, `:289`, `:293`).
Leave three as they are and change ONE to `approved_by: null`, so the fixtures cover both
shapes.

```bash
cd <worktree>/frontend && pnpm run lint && pnpm run test
cd <worktree>
uv run ruff check src tests alembic scripts
uv run pytest -n 16 --dist loadfile   # DELTA +1 on 5432 -> expect 5433, EXACTLY 1 skipped
git add -A && git commit -m "fix(boards): an item's approver is optional in the API, not just the column"
```

---

### Task 3: taking a card down, so it stays down

**Files:**
- Modify: `src/grant_lab/web/api/boards.py:829-848` (`unapprove_item`)
- Modify: `src/grant_lab/web/api/boards.py:581+` (`approve_items`)
- Test: `tests/test_board_suppression.py`

**Interfaces:**
- Consumes: `BoardSuppression` from Task 1.
- Produces: after `DELETE /api/boards/{id}/items/{call_id}`, a `BoardSuppression` row exists for
  `(board_id, call_id)`. After `POST /api/boards/{id}/items` naming that call, it does not.

**The route is called `unapprove_item`**, not `delete_item` — verified at `boards.py:829`.
`BoardItem`'s primary key is composite, so the existing lookup is
`session.get(BoardItem, (board.id, call_id))`.

**Why DELETE suppresses on manual boards too.** Uniform behaviour beats a rule that depends on a
mode, and it means flipping a board to auto later does not resurrect everything its owner ever
removed. The cost is that re-approving must clear the row, which Step 3 does.

- [ ] **Step 1: Write the failing tests**

```python
"""Taking a card off one board keeps it off, and approving it puts it back."""

from __future__ import annotations

from grant_lab.db.models import BoardItem, BoardSuppression
from tests.test_boards_qr_at_approval import (
    _client_for,
    _make_board,
    _make_call,
    _setup,
)

# NOTE: this file uses `tests.test_boards_qr_at_approval._make_call`, which
# REQUIRES `url=`. The same-named helper in tests/web/test_api_board_items.py
# does NOT take `url` and derives it. They are not interchangeable, and
# importing both into one module is ruff F811 -- see Task 4.


def test_unapproving_records_a_suppression(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 1. Deleting the row is not enough -- the 05:00 job would re-add it."""
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="suppression gate1")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="s" * 16)
    call_id = _make_call(pg_session_factory, external_id="sup-1", url="https://example.org/s1")

    client = _client_for(app, owner)
    with client:
        approve = client.post(
            f"/api/boards/{board_id}/items", json={"call_ids": [call_id]}
        )
        assert approve.status_code == 200, approve.text
        assert client.delete(f"/api/boards/{board_id}/items/{call_id}").status_code == 204

    with pg_session_factory() as s:
        assert s.get(BoardItem, (board_id, call_id)) is None
        sup = s.get(BoardSuppression, (board_id, call_id))
        assert sup is not None, "the takedown was not recorded"
        assert sup.suppressed_by == owner


def test_approving_clears_the_suppression(monkeypatch, pg_url, pg_session_factory, profile_factory):
    """GATE 2 -- the discriminating control. Without it a call removed once
    could never be re-approved, on any board."""
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="suppression gate2")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="t" * 16)
    call_id = _make_call(pg_session_factory, external_id="sup-2", url="https://example.org/s2")

    client = _client_for(app, owner)
    with client:
        client.post(f"/api/boards/{board_id}/items", json={"call_ids": [call_id]})
        client.delete(f"/api/boards/{board_id}/items/{call_id}")
        r = client.post(f"/api/boards/{board_id}/items", json={"call_ids": [call_id]})
    assert r.status_code == 200, r.text

    with pg_session_factory() as s:
        assert s.get(BoardItem, (board_id, call_id)) is not None
        assert s.get(BoardSuppression, (board_id, call_id)) is None, (
            "an explicit re-approval must clear the takedown"
        )


def test_a_suppression_is_scoped_to_one_board(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 3 -- the control that proves this is not archive by another name.
    Two boards on the SAME profile: removing from one leaves the other alone."""
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="suppression gate3")
    a = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="u" * 16)
    b = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="v" * 16)
    call_id = _make_call(pg_session_factory, external_id="sup-3", url="https://example.org/s3")

    client = _client_for(app, owner)
    with client:
        client.post(f"/api/boards/{a}/items", json={"call_ids": [call_id]})
        client.post(f"/api/boards/{b}/items", json={"call_ids": [call_id]})
        client.delete(f"/api/boards/{a}/items/{call_id}")

    with pg_session_factory() as s:
        assert s.get(BoardSuppression, (a, call_id)) is not None
        assert s.get(BoardSuppression, (b, call_id)) is None
        assert s.get(BoardItem, (b, call_id)) is not None, "board B lost a card it never removed"


def test_reapproving_on_one_board_leaves_another_boards_takedown_alone(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 4 -- the one that pins the APPROVE-CLEAR is board-scoped.

    Gate 3 pins that UNAPPROVE is board-scoped. Nothing pins the other half,
    and the two are different code paths. The sequence that discriminates puts
    an approval AFTER a suppression exists on another board -- gate 3 never
    does, so gate 3 cannot see the bug at all.

    The bug, if this is missing: approving a call on board B deletes EVERY
    board's suppression for that call, so an operator's takedown on board A is
    silently undone by an unrelated approval on board B. That is exactly the
    cross-board reach the spec rejects archive for.
    """
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="suppression gate4")
    a = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="J" * 16)
    b = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="K" * 16)
    call_id = _make_call(pg_session_factory, external_id="sup-4", url="https://example.org/s4")

    client = _client_for(app, owner)
    with client:
        client.post(f"/api/boards/{a}/items", json={"call_ids": [call_id]})
        client.post(f"/api/boards/{b}/items", json={"call_ids": [call_id]})
        assert client.delete(f"/api/boards/{a}/items/{call_id}").status_code == 204
        assert client.delete(f"/api/boards/{b}/items/{call_id}").status_code == 204
        # Re-approve on B ONLY. A's takedown must survive.
        r = client.post(f"/api/boards/{b}/items", json={"call_ids": [call_id]})
    assert r.status_code == 200, r.text

    with pg_session_factory() as s:
        assert s.get(BoardSuppression, (b, call_id)) is None, "B's own re-approval did not clear B"
        assert s.get(BoardSuppression, (a, call_id)) is not None, (
            "approving on board B cleared board A's takedown - the clear is not board-scoped"
        )
```

**Gate 4 is not optional and is not a nicety.** The dry run measured the first draft's third
mutation (`BoardSuppression.board_id == board.id` -> a bare `True`) against gates 1-3: **all
three passed.** 27% of this plan's mutations proved nothing, and this was one of them. Gate 4
was written and measured in the dry run: `FAILED` with the mutant, `PASSED` restored.

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_board_suppression.py -v`

Expected, **four** tests:

| Gate | Now | Why |
|---|---|---|
| 1 `..._records_a_suppression` | **FAIL** | no `BoardSuppression` row is written |
| 2 `..._clears_the_suppression` | **PASS** | nothing to clear yet — a control, not a gate. It must keep passing after Step 3, not start passing |
| 3 `..._scoped_to_one_board` | **FAIL** | no row is written on board A either |
| 4 `..._leaves_another_boards_takedown_alone` | **FAIL** | no row exists for board A to keep |

An ERROR naming `BoardSuppression` means Task 1 was not committed — that is an import failure,
not a gate.

- [ ] **Step 3: Write the suppression on unapprove**

In `unapprove_item`, replace `session.delete(item)` with:

```python
        session.delete(item)
        # Deleting the row is not a takedown on an auto board: the 05:00 job
        # rebuilds from the candidate set and would put this card straight
        # back. Record the decision so it survives. Written on MANUAL boards
        # too -- uniform behaviour beats a rule that depends on a mode, and it
        # means flipping a board to auto later does not resurrect everything
        # its owner ever removed.
        session.merge(
            BoardSuppression(board_id=board.id, call_id=call_id, suppressed_by=user.id)
        )
        session.commit()
```

`session.merge`, not `session.add`: a call removed, re-approved and removed again would
otherwise violate the primary key.

- [ ] **Step 4: Clear it on approve**

In `approve_items`, inside the write transaction, immediately before the `for offset, cid in
enumerate(new_ids):` loop:

```python
            # An explicit approval is a newer and stronger signal from the same
            # human than an old takedown. Without this a call removed once
            # could never be re-approved, on any board.
            session.execute(
                delete(BoardSuppression).where(
                    BoardSuppression.board_id == board.id,
                    BoardSuppression.call_id.in_(new_ids),
                )
            )
```

Add `delete` to the `sqlalchemy` import at the top of the module if it is not already there, and
`BoardSuppression` to the models import.

- [ ] **Step 5: Run the tests and prove the gates bite**

```bash
uv run pytest tests/test_board_suppression.py -v          # expect 4 passed
cp src/grant_lab/web/api/boards.py /tmp/pre-mutation-boards.py    # the safety net, ONCE
```

Then run each mutation, restoring between, and record the ACTUAL result:

| # | Mutation | Must redden |
|---|---|---|
| 1 | delete the `session.merge(BoardSuppression(...))` line | gates 1, 3 **and 4** |
| 2 | delete the `delete(BoardSuppression)` block in `approve_items` | gate 2 |
| 3 | change `BoardSuppression.board_id == board.id` to a bare `True` in the approve clear | **gate 4** |

**Mutation 3 used to say "gate 3", and that was measured false.** With `True` in place, gates
1-3 all pass — the delete is board-scoped in a sequence that never approves after a suppression
exists. Gate 4 is the only test in this plan that can see it.

A mutation that reddens nothing is a **finding**, not a pass. Judge on FAILED, never on ERROR.

**Restoring.** `cp /tmp/pre-mutation-boards.py src/grant_lab/web/api/boards.py`, then
`git diff HEAD --stat` and confirm it is **NON-EMPTY** — the fix is still there.

> **Never `git checkout -- src/` here.** Step 6 has not committed yet, so that reverts to Task
> 2 and deletes the whole implementation, not just the mutant. Mutations 2 and 3 would then run
> against code with no feature in it, and every verdict after the first would be meaningless.
> Worse, it leaves `git status --porcelain` **empty**, which the first draft told you to read as
> "clean". Measured in the dry run; see the Global Constraints.

- [ ] **Step 6: Commit**

```bash
uv run pytest -n 16 --dist loadfile   # DELTA +4 on 5433 -> expect 5437, EXACTLY 1 skipped
uv run ruff check src tests alembic scripts
git add -A && git commit -m "feat(boards): removing a card from one board keeps it off that board"
```

**Carry the DELTA, never a copied absolute.** The first draft said `5428 + 3 = 5431` here, which
ignored Tasks 1 and 2 entirely; the dry run measured `5435` at this point for its 3-test version
of the task. `+4` is this task's own count (gates 1-4). If your total differs, **name the term
that changed** — do not adjust the number to make the arithmetic close.

---

### Task 4: one candidate set, shared

**Files:**
- Modify: `src/grant_lab/boards/queue.py`
- Create: `src/grant_lab/boards/auto_publish.py`
- Test: `tests/test_board_auto_selection.py`

**Interfaces:**
- Consumes: `BoardSuppression` (Task 1), `Board.auto_publish` (Task 1).
- Produces, in `queue.py`:
  `board_candidate_parts(session, *, board) -> tuple[int, list, Any]` returning
  `(owner_id, filters, ranking_join)`.
- Produces, in `auto_publish.py`:
  `@dataclass Selected: call_id: int; url: str; score: int` and
  `board_selection(session, *, board) -> list[Selected]`, best score first, already capped at
  `board.items_per_page * board.max_pages`, suppressed calls excluded.

**Why extract rather than copy.** `queue.py`'s own module docstring says it CALLS
`_decision_predicate` rather than copying it, because "a second copy is how the rail and the feed
drift apart under a filter nobody re-tested". The same argument applies with more force here: if
the admin queue and the auto selection disagree about what qualifies, the queue shows an admin a
call the board will never publish, or the reverse.

- [ ] **Step 1: Extract the shared parts**

In `src/grant_lab/boards/queue.py`, lift **`:50-56` and `:62`** into a function and have
`board_queue` call it.

**Not `:50-62`, which the first draft said.** `:57` is `approved_ids` and `:58-61` its comment;
those belong to `board_queue` (the admin list flags already-approved candidates) and must stay
put. Measured. Cite the NAMES if the lines have moved: `owner_id`, the `filters` list, and
`ranking_join` move; `approved_ids` and its comment do not.

```python
def board_candidate_parts(session: Session, *, board: Board):
    """The filters and join every view of a board's candidates shares.

    `board_queue` (the admin's list) and `boards/auto_publish.board_selection`
    (what actually goes up) MUST agree about what qualifies. A second copy is
    how they drift under a filter nobody re-tested -- this module's own
    docstring makes the same argument about `_decision_predicate`.
    """
    owner_id = session.scalar(select(Profile.created_by).where(Profile.id == board.profile_id))
    filters = [
        _decision_predicate("archive", user_id=owner_id, profile_id=board.profile_id),
        CallRanking.aggregate_score >= board.min_score,
        Call.deadline.is_not(None),
        Call.deadline > func.now(),
    ]
    # Scope the ranking join to the BOARD'S OWN profile. A call ranked on
    # several profiles has a CallRanking row per profile; without this clause
    # the join fans out one row per profile and judges `min_score` against a
    # score another group produced.
    ranking_join = (CallRanking.call_id == Call.id) & (CallRanking.profile_id == board.profile_id)
    return owner_id, filters, ranking_join
```

Then in `board_queue`, replace those lines with:

```python
    # `_owner_id`: MEASURED dead after the extraction -- its only use in this
    # function was the `_decision_predicate` call that just moved. Underscored
    # so the next reader is not hunting for a use that is not there.
    _owner_id, filters, ranking_join = board_candidate_parts(session, board=board)
```

Leave everything else in `board_queue` exactly as it is — `approved_ids` in particular.

**The first draft said "`owner_id` stays bound because the rest of the function may use it".
That is false, measured:** `grep -n owner_id src/grant_lab/boards/queue.py` returns exactly two
hits, `:50` (the assignment) and `:52` (inside the filters), and both move. Nothing below the
unpack touches it. It is benign either way — this repo selects no `PL*` rules and pyflakes does
not flag unused tuple-unpack targets — but the stated reason was wrong, and the next reader
would have believed it. `board_selection` in Step 4 already writes `_owner_id`; match it.

- [ ] **Step 2: Prove the extraction changed nothing**

Run: `uv run pytest tests/ -k board_queue -v` and any file that exercises the queue
(`ls tests | grep -i queue` to find them).
Expected: the same passes as before the edit. A pure move that reddens a test is a move that was
not pure — fix it before continuing.

- [ ] **Step 3: Write the failing selection tests**

**Before writing a single body, prove the imports resolve.** A missing helper is an **ERROR**,
not a FAILED, and this suite reads a wall of ERRORs with zero FAILED as a clean run:

```bash
uv run python -c "from tests.web.test_api_board_items import _make_call, _rank; print('ok')"
uv run python -c "from tests.test_boards_qr_at_approval import _make_board; print('ok')"
```

```python
"""What an auto board would publish, before anything writes it."""

from __future__ import annotations

from grant_lab.boards.auto_publish import board_selection
from grant_lab.db.models import Board, BoardSuppression
from tests.test_boards_qr_at_approval import _make_board
from tests.web.test_api_board_items import FUTURE, _make_call, _rank


def _board(pg_session_factory, board_id) -> Board:
    with pg_session_factory() as s:
        return s.get(Board, board_id)


def _ranked_call(pg_session_factory, *, external_id, profile_id, score, deadline=FUTURE):
    """A Call plus its CallRanking against ONE profile.

    The ranking is what `board_candidate_parts` joins on, so a call without one
    never qualifies. The url is derived by `_make_call` as
    https://example.org/<external_id>. `deadline` is threaded through because
    `test_an_expired_call_is_excluded` needs a PAST one and there is no other
    way to get it.
    """
    call_id = _make_call(pg_session_factory, external_id=external_id, deadline=deadline)
    _rank(pg_session_factory, call_id=call_id, profile_id=profile_id, score=score)
    return call_id


def test_the_threshold_is_the_floor(pg_session_factory, profile_factory):
    """A call below min_score never appears, however empty the board is."""
    ...


def test_the_capacity_is_the_ceiling(pg_session_factory, profile_factory):
    """items_per_page * max_pages, best score first. With the defaults that is
    8 * 3 = 24."""
    ...


def test_a_suppressed_call_is_excluded(pg_session_factory, profile_factory):
    """GATE -- this is what makes a takedown stick."""
    ...


def test_an_expired_call_is_excluded(pg_session_factory, profile_factory):
    """The deadline filter, inherited from board_candidate_parts."""
    ...
```

**`ruff check` on this block reports three `F401`s (`board_selection`, `BoardSuppression`,
`_make_board`) while the bodies are still `...`. That is expected and you must NOT "fix" it by
deleting the imports** — all three are used the moment Step 5 fills the bodies. Run ruff for
real at Step 6, not here. (Measured: those three, and nothing else.)

**Four things in that block are load-bearing. Three were wrong in the first draft (measured).**

1. **There is NO `ranked_call_factory` fixture.** The first draft gave all four signatures one.
   `fixture 'ranked_call_factory' not found` is an **ERROR**, not a FAILED — the exact trap this
   plan's own fixture note warns about, and the same step contradicted itself twelve lines later
   by correctly calling the helper a plain function. The signatures take
   `(pg_session_factory, profile_factory)` and call `_ranked_call(...)` directly.
2. **Import `_make_call` from EXACTLY ONE module.** The first draft imported `_make_board,
   _make_call` from `tests.test_boards_qr_at_approval` **and** `_make_call, _rank` from
   `tests.web.test_api_board_items`. That is ruff `F811 Redefinition of unused '_make_call'`,
   which fails this task's own Step 6 gate — and silently worse than the lint: the two helpers
   have **different signatures** (`test_boards_qr_at_approval`'s *requires* `url=`;
   `test_api_board_items`' *refuses* it), so whichever import wins changes what the file can
   call. Take `_make_board` from the first and `_make_call, _rank, FUTURE` from the second.
3. **`_ranked_call` takes `deadline`.** The first draft's wrapper had no such parameter, so
   `test_an_expired_call_is_excluded` was unwritable with it. `_make_call` already accepts
   `deadline=`; thread it through and default it to `FUTURE`.
4. **`_rank` already fills `rationale`, `model` and `prompt_version`** — the three NOT NULL
   columns on `CallRanking` with no usable default (`db/models/ranking.py`). Omitting them
   raises `IntegrityError`, which pytest reports as an **ERROR**: a task that "went red" that
   way has proved nothing. Reuse `_rank`; do not write a second copy of those kwargs.

- [ ] **Step 4: Write the selection**

```python
"""What an auto board publishes, and the job that makes it so.

The selection is a pure query: given a board, what should be on it right now.
It is separated from the writing so it can be tested without a scheduler, and
so the settings preview can call it to show an admin the four numbers before
they save.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from grant_lab.boards.queue import board_candidate_parts
from grant_lab.db.models import Board, BoardSuppression, Call, CallRanking


@dataclass(frozen=True)
class Selected:
    call_id: int
    url: str
    score: int


def board_selection(session: Session, *, board: Board) -> list[Selected]:
    """The cards an auto board should carry, best score first, already capped.

    The threshold is the FLOOR and the capacity is the CEILING: everything
    qualifying is published, across as many pages as it needs, until
    `items_per_page * max_pages`. Past that the highest-scoring win. The
    settings preview shows the truncation so the cap is never silent.
    """
    _owner_id, filters, ranking_join = board_candidate_parts(session, board=board)
    suppressed = (
        select(BoardSuppression.call_id)
        .where(BoardSuppression.board_id == board.id)
        .scalar_subquery()
    )
    cap = board.items_per_page * board.max_pages
    rows = session.execute(
        select(Call.id, Call.url, CallRanking.aggregate_score)
        .join(CallRanking, ranking_join)
        .where(*filters, Call.id.notin_(suppressed))
        .order_by(CallRanking.aggregate_score.desc(), Call.id.desc())
        .limit(cap)
    ).all()
    return [Selected(call_id=cid, url=url, score=score) for cid, url, score in rows]


def qualifying_count(session: Session, *, board: Board) -> int:
    """How many calls clear the threshold, BEFORE the capacity cap.

    The preview needs this and `len(board_selection(...))` cannot give it --
    that is already capped, which is exactly the number that hides truncation.
    """
    _owner_id, filters, ranking_join = board_candidate_parts(session, board=board)
    suppressed = (
        select(BoardSuppression.call_id)
        .where(BoardSuppression.board_id == board.id)
        .scalar_subquery()
    )
    return session.scalar(
        select(func.count())
        .select_from(Call)
        .join(CallRanking, ranking_join)
        .where(*filters, Call.id.notin_(suppressed))
    )
```

- [ ] **Step 5: Fill in the test bodies, run, and prove the cap**

Write each body using `_ranked_call`. The capacity test must insert **30** ranked calls and
assert `len(board_selection(...)) == 24` AND that the 24 returned are the highest-scoring —
asserting only the length passes an implementation that returns the WORST 24.

**The 30 scores MUST BE DISTINCT AND DESCENDING: `score=100 - i` for `i` in `range(30)`, i.e.
100 down to 71.** This is not a style note, it is what makes mutation 3 able to fail. The order
is `ORDER BY aggregate_score <dir>, Call.id DESC`; with N **tied** scores that returns the same
rows in the same order whichever direction the score sorts, so `.asc()` reddens nothing and the
gate is hollow — the same shape as Task 3's mutation 3. Every other test in this plan uses one
flat score (`score=90`, `score=95`), so this is the exception and it has to be stated. In the
dry run this mutation reddened **only** because the implementer chose 30 distinct scores on
their own; the plan had not asked for it.

Board defaults are `min_score=60`, `items_per_page=8`, `max_pages=3` (`db/models/boards.py`),
so 71-100 all clear the floor and the cap is 24.

```bash
uv run pytest tests/test_board_auto_selection.py -v      # expect 4 passed
cp src/grant_lab/boards/auto_publish.py /tmp/pre-mutation-autopub.py   # safety net, ONCE
cp src/grant_lab/boards/queue.py /tmp/pre-mutation-queue.py
```

Mutations, restoring between:

| Mutation | Must redden |
|---|---|
| drop `.limit(cap)` | the capacity test |
| drop `Call.id.notin_(suppressed)` | the suppression test |
| change `.desc()` to `.asc()` on the score order | the capacity test's "highest-scoring" assertion — **only if the scores are distinct** |

**Restore by `cp` from `/tmp`, then check `git diff HEAD --stat` is NON-EMPTY.** Step 6 has not
committed yet: `git checkout -- src/` here deletes this task's whole implementation and leaves
`git status --porcelain` empty, which reads as clean. See the Global Constraints.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src tests alembic scripts
uv run pytest -n 16 --dist loadfile   # DELTA +4 on 5437 -> expect 5441, EXACTLY 1 skipped
git add -A && git commit -m "feat(boards): the cards an auto board should carry, as a query"
```

`+4` is this task's own four selection tests. `board_candidate_parts` is a pure move and adds
none — Step 2 already proved it reddens nothing. Carry the delta, not a copied absolute.

---

### Task 5: the nightly job

**Files:**
- Modify: `src/grant_lab/boards/auto_publish.py` (add `reconcile_auto_boards`)
- Modify: `src/grant_lab/scheduler.py` (register the job)
- Test: `tests/test_board_auto_publish_job.py`

**Interfaces:**
- Consumes: `board_selection` (Task 4), `BoardSuppression` (Task 1), `qr_for` from
  `grant_lab.boards.qr`.
- Produces: `@dataclass AutoPublishSummary: boards_seen: int; rows_added: int; rows_removed: int`
  and `reconcile_auto_boards(*, session_factory) -> AutoPublishSummary`.

**Do NOT import `_lock_board_row` from `web/api/boards.py`.** Verified at `bf5a64a4`:
`web/api/boards.py:48-50` imports three modules from `grant_lab.boards`, so an import the other
way is a cycle. Take the lock inline with
`session.get(Board, board_id, with_for_update=True)` and skip a board that has been deleted —
the same 404-shaped outcome `_lock_board_row` produces for a request, expressed as "skip" for a
job that has no caller to answer.

**The two-pass shape, and why.** This mirrors `approve_items` after `5e8329dd`. Encoding is
CPU-bound; running it inside the write transaction holds the board row lock — and, in the
request path, the profile row lock behind it — for as long as the encode takes. So:

1. **Pass 1, no lock.** Read the board, compute `board_selection`, encode every url. Nothing is
   held.
2. **Pass 2, under the lock.** Re-read the board `FOR UPDATE`, **re-check `auto_publish` and the
   settings**, recompute the selection from the locked row, and apply it — looking each QR up in
   pass 1's dict with an inline encode as fallback.

Without the re-check, the job can read `auto_publish = true`, compute a replacement set, and
write it *after* a settings request has turned automation off — breaking the spec's own promise
that turning it off freezes the board. `update_board` (`boards.py:307`) and `approve_items`
(`:581`) both take this lock before deciding anything from a read, for the same reason.

- [ ] **Step 1: Write the failing tests**

**Seven tests, not five.** Gates 6 and 7 are the two the first draft was missing, and without
them the entire two-pass lock discipline — the architectural point of this task, and a stated
spec requirement — has **no test at all**. Measured: see Step 5.

```python
"""The 05:00 job: what it publishes, what it refuses to publish, what it
re-decides under the lock, and what it does not hold while it works."""

from __future__ import annotations

from sqlalchemy import select, text

from grant_lab.boards import auto_publish as ap
from grant_lab.boards.auto_publish import reconcile_auto_boards
from grant_lab.db.models import BoardItem, BoardSuppression
from tests.test_board_auto_selection import _ranked_call
from tests.test_boards_qr_at_approval import _make_board


def _sql(pg_session_factory, board_id, sql, **kw):
    """One-statement UPDATE against `boards`, committed. Used both to arm a
    board and, from inside the `qr_for` seam, to simulate a settings request
    landing BETWEEN the two passes."""
    with pg_session_factory() as s:
        s.execute(text(sql), {"b": board_id, **kw})
        s.commit()


def _set_auto(pg_session_factory, board_id, on=True):
    _sql(pg_session_factory, board_id, "UPDATE boards SET auto_publish = :v WHERE id = :b", v=on)


def test_a_manual_board_is_untouched(pg_session_factory, profile_factory):
    """GATE 1 -- the control. Every board that exists when this ships is manual,
    and this job must be invisible to all of them.

    NOTE what this does NOT cover: the board here was never auto, so it never
    enters `board_ids` and never reaches pass 2. Pass 2's own `auto_publish`
    re-check is invisible to it. That is gate 6's job.
    """
    pid, owner = profile_factory(name="job manual")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="m" * 16)
    _ranked_call(pg_session_factory, external_id="job-m1", profile_id=pid, score=90)
    reconcile_auto_boards(session_factory=pg_session_factory)
    with pg_session_factory() as s:
        assert s.scalars(select(BoardItem).where(BoardItem.board_id == bid)).all() == []


def test_an_auto_board_publishes_qualifying_calls(pg_session_factory, profile_factory):
    """GATE 2. And the row records that no human approved it."""
    pid, owner = profile_factory(name="job auto")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="w" * 16)
    _set_auto(pg_session_factory, bid)
    cid = _ranked_call(pg_session_factory, external_id="job-a1", profile_id=pid, score=90)
    reconcile_auto_boards(session_factory=pg_session_factory)
    with pg_session_factory() as s:
        item = s.get(BoardItem, (bid, cid))
        assert item is not None
        assert item.approved_by is None, "an auto row must not claim a human approver"
        assert item.qr_modules > 0, "the QR was not rendered"


def test_a_suppressed_call_is_not_republished(pg_session_factory, profile_factory):
    """GATE 3 -- the load-bearing one. This is what makes a takedown a takedown."""
    pid, owner = profile_factory(name="job suppressed")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="x" * 16)
    _set_auto(pg_session_factory, bid)
    cid = _ranked_call(pg_session_factory, external_id="job-s1", profile_id=pid, score=95)
    with pg_session_factory() as s:
        s.add(BoardSuppression(board_id=bid, call_id=cid, suppressed_by=owner))
        s.commit()
    reconcile_auto_boards(session_factory=pg_session_factory)
    with pg_session_factory() as s:
        assert s.get(BoardItem, (bid, cid)) is None, "the 05:00 job undid a takedown"


def test_a_call_that_stops_qualifying_leaves(pg_session_factory, profile_factory):
    """GATE 4. The board is the current best, not an accumulating pile.

    SCOPE, stated because the first draft's mutation table claimed more: this
    covers TWO SEQUENTIAL RUNS CONVERGING. It does NOT cover a settings change
    landing between the two passes of ONE run -- the second call's pass 1
    already reads the new `min_score`, so pass 2 recomputing changes nothing
    here. Gate 7 covers that, and only gate 7 can.
    """
    pid, owner = profile_factory(name="job leaves")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="y" * 16)
    _set_auto(pg_session_factory, bid)
    cid = _ranked_call(pg_session_factory, external_id="job-l1", profile_id=pid, score=90)
    reconcile_auto_boards(session_factory=pg_session_factory)
    with pg_session_factory() as s:
        assert s.get(BoardItem, (bid, cid)) is not None
    _sql(pg_session_factory, bid, "UPDATE boards SET min_score = 95 WHERE id = :b")
    reconcile_auto_boards(session_factory=pg_session_factory)
    with pg_session_factory() as s:
        assert s.get(BoardItem, (bid, cid)) is None


def test_the_encode_does_not_run_under_the_board_lock(
    pg_session_factory, profile_factory, monkeypatch
):
    """GATE 5. Asserting the QR is CORRECT passes with the encode inside the
    write transaction, which is the defect. `FOR UPDATE NOWAIT` from a second
    connection answers 'was the row locked' without a timeout. Same shape as
    tests/test_boards_qr_at_approval.py's gate 12."""
    from sqlalchemy.exc import DBAPIError, OperationalError

    pid, owner = profile_factory(name="job lock")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="z" * 16)
    _set_auto(pg_session_factory, bid)
    for i in range(3):
        _ranked_call(pg_session_factory, external_id=f"job-k{i}", profile_id=pid, score=90 - i)

    held: list[str] = []
    real = ap.qr_for

    def probing(url):
        try:
            with pg_session_factory() as probe:
                probe.execute(
                    text("SELECT id FROM boards WHERE id = :b FOR UPDATE NOWAIT"), {"b": bid}
                )
        except (OperationalError, DBAPIError):
            held.append("board")
        return real(url)

    monkeypatch.setattr(ap, "qr_for", probing)
    reconcile_auto_boards(session_factory=pg_session_factory)

    with pg_session_factory() as s:
        stored = s.scalars(select(BoardItem.qr_modules).where(BoardItem.board_id == bid)).all()
    assert stored and all(m > 0 for m in stored), f"nothing was published: {stored}"
    assert not held, f"the encode ran under the board lock ({len(held)} of 3)"


def test_auto_publish_turned_off_between_the_passes_publishes_nothing(
    pg_session_factory, profile_factory, monkeypatch
):
    """GATE 6. Turning automation off must FREEZE the board -- the spec's own
    promise. This is the ONLY test that reaches pass 2's `auto_publish`
    re-check: gate 1's board was never auto, so it never enters `board_ids`.

    The seam is `qr_for`. It runs after pass 1 and before pass 2 with NO lock
    held, which is exactly where a concurrent settings request would land.
    """
    pid, owner = profile_factory(name="job flip off midway")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="R" * 16)
    _set_auto(pg_session_factory, bid)
    _ranked_call(pg_session_factory, external_id="job-f1", profile_id=pid, score=90)

    real = ap.qr_for

    def flip(url):
        _sql(pg_session_factory, bid, "UPDATE boards SET auto_publish = false WHERE id = :b")
        return real(url)

    monkeypatch.setattr(ap, "qr_for", flip)
    reconcile_auto_boards(session_factory=pg_session_factory)

    with pg_session_factory() as s:
        rows = s.scalars(select(BoardItem).where(BoardItem.board_id == bid)).all()
    assert rows == [], "the job published to a board whose automation was switched off"


def test_min_score_raised_between_the_passes_is_honoured(
    pg_session_factory, profile_factory, monkeypatch
):
    """GATE 7. Pass 1's selection is ADVISORY. This is the ONLY test that can
    tell `board_selection` recomputed under the lock from `likely` being reused,
    because it is the only one where the settings move BETWEEN the passes.
    """
    pid, owner = profile_factory(name="job raise min_score midway")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="S" * 16)
    _set_auto(pg_session_factory, bid)
    _ranked_call(pg_session_factory, external_id="job-f2", profile_id=pid, score=90)

    real = ap.qr_for

    def raise_floor(url):
        _sql(pg_session_factory, bid, "UPDATE boards SET min_score = 95 WHERE id = :b")
        return real(url)

    monkeypatch.setattr(ap, "qr_for", raise_floor)
    reconcile_auto_boards(session_factory=pg_session_factory)

    with pg_session_factory() as s:
        rows = s.scalars(select(BoardItem).where(BoardItem.board_id == bid)).all()
    assert rows == [], "pass 2 wrote pass 1's stale selection instead of re-deciding"
```

**Notes on that block, all measured in the dry run.**

- `Board` is **not** imported: the first draft imported it and never used it (ruff `F401`,
  fails this task's own Step 6 gate). Nor is the 103-char `def` line for gate 5 kept.
- `ap` is imported at module level rather than inside gate 5, because gates 6 and 7 need it too.
- **The `monkeypatch.setattr(ap, "qr_for", ...)` seam works.** Confirmed: the job calls the
  module-global `qr_for`, not a locally re-imported name, so the patch takes effect.
- Gates 6 and 7 flip settings from **inside** `qr_for`. That is between the passes with no lock
  held. Both were written and measured in the dry run: green on correct code, and each kills its
  own mutation and only its own.

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_board_auto_publish_job.py -v`
Expected: all **seven** FAIL on `ImportError: cannot import name 'reconcile_auto_boards'`. That
is an ERROR, not a FAILED — it is the correct red for a function that does not exist yet, but do
not mistake it for a passing gate later. Once the function exists, re-read this list: every one
must be a genuine FAILED before Step 3.

- [ ] **Step 3: Write the job**

Append to `src/grant_lab/boards/auto_publish.py`:

```python
@dataclass
class AutoPublishSummary:
    boards_seen: int = 0
    rows_added: int = 0
    rows_removed: int = 0
    # One board that raises must not cost the others their night. Counted so
    # the tick can log a non-zero and an operator can see it.
    boards_failed: int = 0


def reconcile_auto_boards(*, session_factory) -> AutoPublishSummary:
    """Make every auto board's items match what `board_selection` says.

    Two passes per board. The encode is CPU-bound and must not run under the
    board row lock -- see this module's docstring and the shape
    `approve_items` uses since 5e8329dd.

    PER-BOARD ISOLATION. Each board's two passes sit in their own try/except.
    A bare loop would let the first board that raises -- a `qr_for` failure, an
    FK conflict, a profile deleted mid-run -- abort boards N+1..M for that
    night AND lose the summary with the exception, which is precisely the
    "a board silently stops updating" outcome this feature exists to prevent.
    APScheduler swallows the traceback into its own logger, so it would also be
    invisible.
    """
    summary = AutoPublishSummary()
    with session_factory() as session:
        board_ids = list(
            session.scalars(select(Board.id).where(Board.auto_publish.is_(True))).all()
        )

    for board_id in board_ids:
        try:
            # PASS 1 -- no lock held. Work out what is likely wanted and encode it.
            prefetch: dict[str, QrCode] = {}
            with session_factory() as session:
                board = session.get(Board, board_id)
                if board is None:
                    continue
                likely = board_selection(session, board=board)
            for sel in likely:
                if sel.url and sel.url not in prefetch:
                    prefetch[sel.url] = qr_for(sel.url)

            # PASS 2 -- under the lock, and everything is decided from the
            # LOCKED row. A settings request may have turned auto_publish off,
            # or moved min_score, between the passes; pass 1's answer is
            # advisory only.
            with session_factory() as session:
                board = session.get(Board, board_id, with_for_update=True)
                if board is None or not board.auto_publish:
                    continue
                summary.boards_seen += 1
                wanted = {s.call_id: s for s in board_selection(session, board=board)}
                current = {
                    i.call_id: i
                    for i in session.scalars(
                        select(BoardItem).where(BoardItem.board_id == board.id)
                    ).all()
                }

                for call_id in current.keys() - wanted.keys():
                    session.delete(current[call_id])
                    summary.rows_removed += 1

                for position, sel in enumerate(wanted.values()):
                    item = current.get(sel.call_id)
                    if item is None:
                        qr = (prefetch.get(sel.url) or qr_for(sel.url)) if sel.url else None
                        session.add(
                            BoardItem(
                                board_id=board.id,
                                call_id=sel.call_id,
                                position=position,
                                # NULL: nobody approved this. See BoardItem's
                                # own comment on the column.
                                approved_by=None,
                                qr_path=(qr.path or None) if qr else None,
                                qr_modules=qr.modules if qr else 0,
                                qr_source_url=sel.url or None,
                            )
                        )
                        summary.rows_added += 1
                    else:
                        item.position = position
                session.commit()
        except Exception as e:  # noqa: BLE001
            # Count it, name the board, keep going. Same idiom as
            # scheduler.py's `_reconcile_rankings_tick`.
            summary.boards_failed += 1
            _logger.exception(
                "boards.auto_publish.board_failed",
                extra={
                    "event": "boards_auto_publish_board_failed",
                    "board_id": board_id,
                    "error": str(e),
                },
            )
    return summary
```

Add the imports this needs at the top of the module: `dataclass` is already there; add
`Board`, `BoardItem` to the models import, `from grant_lab.boards.qr import QrCode, qr_for`, and
— for the isolation above — `import logging` plus
`_logger = logging.getLogger(__name__)` beside it. **`_logger`, not `log`** — see Step 4.

**`wanted.values()` is ordered** because `board_selection` returns score order and Python dicts
preserve insertion order — that is what makes `position` the score rank. If you rewrite this
loop, keep that property or `position` becomes arbitrary.

**Two things the dry run proved correct here — do NOT "fix" them.**

- **`session.get(Board, board_id, with_for_update=True)` takes a real row lock.** Measured on
  SQLAlchemy 2.0.49: a `FOR UPDATE NOWAIT` probe from a second session raised
  `OperationalError` while the `get` was held, and the control (a plain `get()`) did **not**
  lock. Do not rewrite it as `session.execute(select(Board)...with_for_update())`.
- **The `continue` inside the `try` is correct.** `continue` from a `try` block runs no
  `except` and proceeds to the next board, which is what a deleted or de-automated board should
  do.

- [ ] **Step 4: Register the job at 05:00**

In `src/grant_lab/scheduler.py`, beside the existing `_reconcile_rankings_tick` registration
(the `CronTrigger(hour=4, minute=0)` block), add:

```python
    def _auto_publish_tick() -> None:
        # 05:00, after the 04:00 reconcile has enqueued ranking work. The
        # ranking queue drains on an INTERVAL (RANKING_TICK_SECONDS, default
        # 60s), so there is no instant at which ranking is "finished" and no
        # completion signal to wait for. This runs on the clock instead, is
        # idempotent, and is correct if it runs mid-drain: it publishes what
        # the scores say now, and tomorrow corrects it.
        try:
            summary = reconcile_auto_boards(session_factory=session_factory)
            _logger.info(
                "boards.auto_publish.tick.done",
                extra={
                    "event": "boards_auto_publish_tick_done",
                    "boards_seen": summary.boards_seen,
                    "rows_added": summary.rows_added,
                    "rows_removed": summary.rows_removed,
                    "boards_failed": summary.boards_failed,
                },
            )
        except Exception as e:  # noqa: BLE001
            _logger.exception(
                "boards.auto_publish.failed",
                extra={"event": "boards_auto_publish_failed", "error": str(e)},
            )

    sched.add_job(
        _auto_publish_tick,
        trigger=CronTrigger(hour=5, minute=0),
        id="boards:auto-publish",
        name="Daily rebuild of self-publishing corridor boards",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=6 * 3600,
    )
```

**The logger is `_logger`, not `log`. Measured:** `src/grant_lab/scheduler.py:39` defines
`_logger = logging.getLogger(__name__)` and there is no name `log` anywhere in the module. The
first draft's block said `log.info(...)`. That is a `NameError` **inside the tick body, not at
import**, so it does not fail collection and no test in this plan calls the tick — it would have
surfaced for the first time at 05:00 in production and been swallowed into APScheduler's own
job-error log. `session_factory` **is** genuinely in scope at the registration point (measured).

**The `try/except` is not decoration.** `_reconcile_rankings_tick` (`scheduler.py:542-566`) —
the block this one sits beside — wraps its whole body in `try/except Exception` +
`_logger.exception`, and so do `_embeddings_tick` and the ingest job. A tick that breaks that
idiom loses its traceback into APScheduler. Two layers, deliberately: `reconcile_auto_boards`
isolates ONE board, this isolates the whole job.

Import `reconcile_auto_boards` at the top of `scheduler.py`.

**No scheduler test pins an exact job-id set** — measured, the existing check is a `.issubset()`
— so adding `boards:auto-publish` reddens nothing. Do not go looking for a manifest to update
here; the manifests that do exist are the two in Task 6.

- [ ] **Step 5: Run the tests and prove the gates bite**

```bash
uv run pytest tests/test_board_auto_publish_job.py -v     # 7 passed (8 after Step 5b)
cp src/grant_lab/boards/auto_publish.py /tmp/pre-mutation-autopub.py   # safety net, ONCE
```

Mutations, restoring between, judged on FAILED:

| # | Mutation | Must redden |
|---|---|---|
| 1 | drop `if board is None or not board.auto_publish: continue` in pass 2 | **gate 6** |
| 2 | in pass 2, use `likely` instead of recomputing under the lock | **gate 7** |
| 3 | encode inline: `qr = qr_for(sel.url) if sel.url else None` | gate 5 |
| 4 | force `prefetch = {}` before pass 2 | gate 5 |
| 5 | drop `Call.id.notin_(suppressed)` in `board_selection` | gate 3 **and** Task 4's suppression test |
| 6 | delete the `except Exception` around the per-board body | gate 8 (Step 5b) |

**Mutations 1 and 2 used to point at gates 1 and 4. Both were measured PASSING — the largest
finding of the dry run.**

```
MUTANT 1 applied: pass-2 auto_publish re-check removed
$ uv run pytest tests/test_board_auto_publish_job.py -q
.....                                                                    [100%]
5 passed in 5.15s

MUTANT 2 applied: pass 2 uses pass-1's `likely`
$ uv run pytest tests/test_board_auto_publish_job.py -q
.....                                                                    [100%]
5 passed in 5.26s
```

Why neither could discriminate: **mutation 1** — gate 1's board was never auto, so it never
enters `board_ids` and never reaches pass 2; the guard is invisible to it. **Mutation 2** —
gate 4 calls the job twice with the `min_score` UPDATE *between the calls*, so the second call's
**pass 1** already reads the new value and `likely` is already correct. The re-read-under-the-
lock is the whole architectural point of this task and a stated spec requirement, and a
regression deleting it shipped green. Gates 6 and 7 close both.

**Restore by `cp` from `/tmp`, then check `git diff HEAD --stat` is NON-EMPTY.** Step 6 has not
committed yet. See the Global Constraints.

- [ ] **Step 5b: One more gate — the per-board isolation (gate 8)**

The `try/except` added in Step 3 is a behaviour, so it needs a test that can fail. Append:

```python
def test_one_boards_failure_does_not_cost_the_others(
    pg_session_factory, profile_factory, monkeypatch
):
    """GATE 8. A nightly job that stops at the first bad board is the
    'silently stops updating' failure this feature exists to prevent -- and
    APScheduler swallows the traceback, so it would be invisible."""
    pid, owner = profile_factory(name="job isolation")
    good = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="T" * 16)
    bad = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="U" * 16)
    _set_auto(pg_session_factory, good)
    _set_auto(pg_session_factory, bad)
    _ranked_call(pg_session_factory, external_id="job-i1", profile_id=pid, score=90)

    real = ap.qr_for
    seen: list[int] = []

    def blow_up_once(url):
        # Fail the FIRST board the job reaches, whichever that is, then behave.
        if not seen:
            seen.append(1)
            raise RuntimeError("qr backend down")
        return real(url)

    monkeypatch.setattr(ap, "qr_for", blow_up_once)
    summary = reconcile_auto_boards(session_factory=pg_session_factory)

    assert summary.boards_failed == 1, f"the failure was not isolated: {summary}"
    with pg_session_factory() as s:
        rows = s.scalars(select(BoardItem).where(BoardItem.board_id.in_([good, bad]))).all()
    assert rows, "the second board got nothing -- the first board's failure aborted the run"
```

Then `uv run pytest tests/test_board_auto_publish_job.py -v` is **8 passed**.

Mutation 6 (delete the `except Exception` block) must redden this: without it the
`RuntimeError` escapes `reconcile_auto_boards` and the test errors on the raise instead of
reaching its assertions. **An ERROR here is the defect showing itself** — the exception escaping
IS the bug — but record it as an ERROR, do not write it down as a FAILED.

- [ ] **Step 6: Commit**

```bash
uv run pytest -n 16 --dist loadfile   # DELTA +8 on 5441 -> expect 5449, EXACTLY 1 skipped
uv run ruff check src tests alembic scripts
git add -A && git commit -m "feat(boards): a self-publishing board rebuilds itself at 05:00"
```

`+8` = gates 1-8. The first draft said `5431 + 5 = 5436`, which was wrong twice over: it carried
a stale base AND the old five-gate count. The dry run measured `5444` here for its five-gate
version. Carry the delta; if your total differs, **name the term that changed.**

---

### Task 6: the switch, and the four numbers

**Files:**
- Modify: `src/grant_lab/web/api/boards.py` — `BoardSettingsIn`, `BoardOut`, **`_to_out`**,
  **`update_board`'s capacity guard**, new preview route
- Modify: `frontend/src/routes/BoardDetail.tsx` — the `SettingsForm` component and its `submit`
  payload (cited by NAME: the first draft's `:115-140` / `:134-137` / `:110-205` had already
  drifted at `99590b97`, where `SettingsForm` begins at `:111`, the `minScore`/`pageSeconds`
  state is at `:127-128` and the submit payload at `:132-138`)
- Modify: `frontend/src/api/schema.gen.ts` (regenerated)
- **Modify: `tests/architecture/test_boards_permission_single_source.py`** — pinned route manifest
- **Modify: `tests/architecture/test_edition_surface.py`** — pinned route COUNT and its ledger
- **Modify: `tests/architecture/full_edition_routes.txt`** — pinned route baseline
- Test: `tests/web/test_api_board_auto_settings.py`, `frontend/src/routes/BoardDetail.test.tsx`

**BLAST RADIUS — adding one route reddens THREE pinned things.** Measured in the dry run, where
the first draft named none of them and the break surfaced only in the final full-suite run:

| Thing | What to do |
|---|---|
| `test_boards_permission_single_source.py` `_EXPECTED_ROUTES` | add `("get", "/boards/{board_id}/auto-preview")`, and update the comment that says "All **nine** must call `_board_for_edit`" to ten |
| `tests/architecture/full_edition_routes.txt` | regenerate/append `/api/boards/{board_id}/auto-preview` |
| `test_edition_surface.py` `test_the_baseline_is_not_empty` | `117` -> `118`, **and add a ledger paragraph** above `_BASELINE` in that file's own idiom ("one added path, /api/boards/{board_id}/auto-preview (GET)") — the file's comment says the literal is bumped BY HAND so a route arriving without intent cannot be waved through |

The match edition needs nothing: it pins **subset** properties, not a count, and there is no
`match_edition_routes.txt`. Measured — the new route appears in both editions and
`tests/architecture` is green once the three above are updated.

**Interfaces:**
- Consumes: `board_selection`, `qualifying_count` (Task 4); `Board.auto_publish` (Task 1).
- Produces: `BoardSettingsIn.auto_publish: bool | None`; `BoardSettingsIn.max_pages: int | None`;
  `GET /api/boards/{board_id}/auto-preview` returning
  `{qualifying: int, displayed: int, truncated: int, pages: int, loop_seconds: int}`.

**`max_pages` must become settable.** Verified at `bf5a64a4`: `BoardSettingsIn` carries only
`title`, `orientation`, `items_per_page`, `min_score`, `page_seconds` (`boards.py:268-306`). On
an auto board `max_pages` is the only lever that bounds loop length, so an admin who cannot
raise it cannot fix a truncated board. Its DB floor is `ck_boards_max_pages` (`max_pages > 0`);
mirror that with `ge=1` and add a ceiling of `le=64` for the same reason `items_per_page` has
one — a value clearing int4 dies on the UPDATE as an uncaught 500.

### CRITICAL — making `max_pages` settable DISARMS the capacity guard unless you also fix it

Reproduced in the dry run, implementing Task 6 exactly as the first draft wrote it. A landscape
board (`items_per_page=8`, `max_pages=3`, capacity 24) holding 24 approved items, then
`PUT /api/boards/{id} {"max_pages": 1}`:

```
PROBE STATUS: 200
PROBE BODY: {"id":1,...,"items_per_page":8,"max_pages":1,...,"approved_count":24,...}
PROBE ROW COUNT AFTER PUT: 24
PROBE board.max_pages AFTER PUT: 1
PROBE new real cap (items_per_page*max_pages): 8
```

**200 OK, all 24 rows survive, the board's real capacity is now 8.** `update_board` computes

```python
old_cap = board.items_per_page * board.max_pages
new_cap = new_items_per_page * board.max_pages     # <-- board.max_pages, the OLD value
if new_cap < old_cap and new_cap < approved_count:
```

Both sides multiply by the OLD `max_pages`, so `new_cap == old_cap == 24`, `24 < 24` is False,
and the `approved_count` comparison is never reached. Reachable by any profile editor.

**"Just assign it earlier" is not a fix.** Applying `board.max_pages = body.max_pages` *before*
the guard gives the same 200 for the opposite reason: both caps then read the NEW value and
collapse to `8 == 8`, so the comparison becomes structurally incapable of firing on a shrink.
Also measured.

**The fix**, mirroring the `new_items_per_page` pattern already in the function:

```python
        new_max_pages = body.max_pages if body.max_pages is not None else board.max_pages
        old_cap = board.items_per_page * board.max_pages
        new_cap = new_items_per_page * new_max_pages
```

and in the apply block below the guard, `if body.max_pages is not None: board.max_pages = body.max_pages`.
Step 1 adds gate 4 for it and Step 5b mutates it back. Both were measured in the dry run: the
mutant went `FAILED`, the fix went `4 passed`.

### `BoardOut` is not enough — `_to_out` builds it with EXPLICIT kwargs

`_to_out(board, approved_count, owner_undeliverable)` constructs `BoardOut(id=..., slug=..., ...)`
by hand, one keyword per field. Adding a **required** `auto_publish: bool` to `BoardOut` without
adding `auto_publish=board.auto_publish` to `_to_out` is a pydantic `ValidationError` on
**every** board response, not just an auto one. The first draft mentioned `BoardOut` and never
mentioned `_to_out`. `max_pages` is already on both.

- [ ] **Step 1: Write the failing backend tests**

```python
"""The auto-publish switch, and the preview that stops the cap being silent."""

from __future__ import annotations

from grant_lab.db.models import Board
from tests.test_board_auto_selection import _ranked_call
from tests.test_boards_qr_at_approval import _client_for, _make_board, _setup
from tests.web.test_api_board_items import _approve_n

# `_approve_n` seeds N approved BoardItems directly (and their CallRankings
# when `profile_id` is given). It is the only helper that can build the
# at-capacity board gate 4 needs. It does NOT clash with anything above:
# `_make_call` is deliberately NOT imported here.


def test_the_switch_can_be_set(monkeypatch, pg_url, pg_session_factory, profile_factory):
    """GATE 1."""
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="switch gate1")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="A" * 16)
    client = _client_for(app, owner)
    with client:
        r = client.put(f"/api/boards/{bid}", json={"auto_publish": True})
    assert r.status_code == 200, r.text
    assert r.json()["auto_publish"] is True


def test_max_pages_can_be_raised(monkeypatch, pg_url, pg_session_factory, profile_factory):
    """GATE 2. Without this an admin cannot fix a truncated auto board."""
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="switch gate2")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="B" * 16)
    client = _client_for(app, owner)
    with client:
        r = client.put(f"/api/boards/{bid}", json={"max_pages": 18})
    assert r.status_code == 200, r.text
    assert r.json()["max_pages"] == 18


def test_the_preview_separates_qualifying_from_displayed(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 3 -- the load-bearing one. A preview that returns ONE number cannot
    show truncation, and truncation is the failure this exists to surface.

    Board defaults are items_per_page=8, max_pages=3 (db/models/boards.py:93-101),
    so 30 qualifying calls give 24 displayed and 6 truncated."""
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="preview gate3")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="C" * 16)
    for i in range(30):
        _ranked_call(pg_session_factory, external_id=f"prev-{i}",
                 profile_id=pid, score=90)
    client = _client_for(app, owner)
    with client:
        r = client.get(f"/api/boards/{bid}/auto-preview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["qualifying"] == 30
    assert body["displayed"] == 24
    assert body["truncated"] == 6
    assert body["pages"] == 3
    assert body["loop_seconds"] == 90       # 3 pages x page_seconds 30


def test_shrinking_max_pages_below_approved_count_is_refused(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 4 -- the one that stops this task shipping a reachable capacity
    bypass. Exposing `max_pages` disarms `update_board`'s guard unless the
    guard is changed in the same breath. REPRODUCED in the dry run: without
    the Step 3 fix this PUT returns 200, all 24 rows survive, and the board's
    real capacity is now 8. Any profile editor can do it.

    `approve_items`' own docstring calls this shape 'exactly the silent
    truncation spec Sec 6 forbids, just performed by the server instead of the
    display'."""
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="switch gate4")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="D" * 16)
    # Landscape defaults: items_per_page 8 x max_pages 3 = capacity 24, full.
    _approve_n(pg_session_factory, board_id=bid, approved_by=owner, n=24, profile_id=pid)

    client = _client_for(app, owner)
    with client:
        r = client.put(f"/api/boards/{bid}", json={"max_pages": 1})
    assert r.status_code == 409, f"a capacity-shrinking max_pages was accepted: {r.text}"

    with pg_session_factory() as s:
        board = s.get(Board, bid)
        assert board.max_pages == 3, "the refused change was applied anyway"
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/web/test_api_board_auto_settings.py -v`

Expected — **four different reds. Check each one; the first draft got gate 1's cause wrong.**

| Gate | Failure | Why |
|---|---|---|
| 1 `..._switch_can_be_set` | **`KeyError: 'auto_publish'`** | `auto_publish` is not on `BoardOut` **at all**, so `r.json()["auto_publish"]` raises. NOT a stale value |
| 2 `..._max_pages_can_be_raised` | stale value: `3 != 18` | `max_pages` IS already on `BoardOut`, so pydantic's `extra="ignore"` drops the unknown input and the response carries the old value |
| 3 `..._preview_separates_qualifying_from_displayed` | `404` | the route does not exist |
| 4 `..._shrinking_max_pages_..._refused` | `200 != 409` | `max_pages` is dropped, so nothing changes and the guard has nothing to refuse |

Gates 1 and 2 fail for **different reasons with different fixes** — gate 1 needs the field added
to `BoardOut` *and* to `_to_out`; gate 2 needs it added to `BoardSettingsIn` and applied. A
worker told to look for a stale value on gate 1 goes hunting and finds a `KeyError`.

- [ ] **Step 3: Add the two settings fields**

In `BoardSettingsIn` (`boards.py:268`):

```python
    # Off by default and explicitly settable. Turning this on replaces the
    # board's hand-picked items on the next 05:00 run -- the UI warns before
    # saving, this does not, because a partial update has no place to say it.
    auto_publish: bool | None = None
    # 1-64. The DB floor is ck_boards_max_pages ("max_pages > 0"); the ceiling
    # mirrors items_per_page's, and for the same measured reason -- a value
    # clearing int4's range sails past pydantic and dies on the UPDATE as an
    # uncaught 500. Exposed because on an auto board this is the ONLY lever
    # that bounds loop length.
    max_pages: int | None = Field(default=None, ge=1, le=64)
```

Then, in `update_board`, **all three of these, in this order**:

1. **Fix the capacity guard** (see the CRITICAL section above — this is not optional):
   ```python
        new_max_pages = body.max_pages if body.max_pages is not None else board.max_pages
        old_cap = board.items_per_page * board.max_pages
        new_cap = new_items_per_page * new_max_pages
   ```
2. **Apply the two fields**, in the explicit apply block *below* the guard, beside
   `if body.min_score is not None:`:
   ```python
        if body.auto_publish is not None:
            board.auto_publish = body.auto_publish
        if body.max_pages is not None:
            board.max_pages = body.max_pages
   ```
   `update_board` uses an explicit per-field allow-list, **not** `model_dump(exclude_unset=True)`
   — measured. A field not named here is accepted and silently dropped.
3. **Add `auto_publish: bool` to `BoardOut` AND `auto_publish=board.auto_publish` to `_to_out`.**
   `max_pages` is already on both. Missing the `_to_out` half is a `ValidationError` on every
   board response — see the section above.

- [ ] **Step 4: Add the preview route**

```python
class AutoPreviewOut(BaseModel):
    """The four numbers an admin needs before saving a threshold.

    One number cannot express this: a threshold set too low truncates
    invisibly, and a max_pages raised to fix that makes any given call visible
    for a fraction of the loop. Both are decisions to make on purpose.
    """

    qualifying: int
    displayed: int
    truncated: int
    pages: int
    loop_seconds: int


@router.get("/boards/{board_id}/auto-preview", response_model=AutoPreviewOut)
def auto_preview(
    request: Request,
    board_id: int,
    user: User = Depends(get_current_user),  # noqa: B008
):
    """What this board WOULD publish right now. Read-only, writes nothing."""
    sf = request.app.state.session_factory
    with sf() as session:
        board = _board_for_edit(session, board_id=board_id, user=user)
        qualifying = qualifying_count(session, board=board)
        displayed = len(board_selection(session, board=board))
        pages = -(-displayed // board.items_per_page) if displayed else 0
        return AutoPreviewOut(
            qualifying=qualifying,
            displayed=displayed,
            truncated=qualifying - displayed,
            pages=pages,
            loop_seconds=pages * board.page_seconds,
        )
```

Import `board_selection` and `qualifying_count` from `grant_lab.boards.auto_publish` at the top.

- [ ] **Step 5: Run, regenerate types, wire the UI**

```bash
uv run pytest tests/web/test_api_board_auto_settings.py -v    # expect 4 passed
cd <worktree>/frontend && pnpm run gen:api
```

(Again: no `EMBEDDING_MODEL=` prefix — the script pins it. See Task 0.)

In `frontend/src/routes/BoardDetail.tsx`, inside the **`SettingsForm`** component, beside the
existing `minScore` / `pageSeconds` state, add `autoPublish` and `maxPages` state seeded from
`board`, include both in the **`submit`** payload, and render:

- a checkbox **"Publish new calls automatically"**
- **a number input for `max_pages`** (Mantine `NumberInput`, `min={1} max={64}`, labelled
  "Maximum pages"). **This is not optional and the first draft omitted it.** Step 3's entire
  justification for exposing `max_pages` is *"an admin who cannot raise it cannot fix a
  truncated board"* — a UI that shows the truncation and offers no way to fix it delivers half
  the feature. The API ceiling is `le=64`; mirror it so the control cannot send a 422.
- when the checkbox is on, the preview line, fetched from `/auto-preview` with TanStack Query:
  **"142 qualify · 24 shown · 118 not shown · 3 pages · 90s loop"**
- when the box is being turned ON and the board has items, a warning before save:
  **"This replaces the N cards on this board at the next nightly run."**

Follow this file's existing idiom for state, mutation and error handling — read `SettingsForm`
end to end first. Do not introduce a new data-fetching pattern. **Cite names, not lines:** this
file's line numbers had already drifted between the first draft and `99590b97`.

- [ ] **Step 5b: Update the three pinned manifests, then prove the guard gate bites**

The new route is red in `tests/architecture` until all three are updated. Do them together:

```bash
uv run pytest tests/architecture -q      # RED first -- look at WHICH two files
# 1. test_boards_permission_single_source.py: add ("get", "/boards/{board_id}/auto-preview")
#    to _EXPECTED_ROUTES, and change the "All nine" comment to "All ten".
# 2. tests/architecture/full_edition_routes.txt: add /api/boards/{board_id}/auto-preview
# 3. test_edition_surface.py: test_the_baseline_is_not_empty 117 -> 118, plus a ledger
#    paragraph above _BASELINE in that file's own idiom.
uv run pytest tests/architecture -q      # expect 192 passed
```

Then the mutation, restoring by `cp` (Step 6 has not committed yet):

```bash
cp src/grant_lab/web/api/boards.py /tmp/pre-mutation-boards6.py
# MUTANT: revert the guard to the OLD max_pages on both sides --
#         new_cap = new_items_per_page * board.max_pages
uv run pytest tests/web/test_api_board_auto_settings.py -v
cp /tmp/pre-mutation-boards6.py src/grant_lab/web/api/boards.py
git diff HEAD --stat                     # MUST be NON-EMPTY
```

Measured in the dry run: `FAILED ...::test_shrinking_max_pages_below_approved_count_is_refused`,
`1 failed, 3 passed`; restored, `4 passed`. If it does not redden, the guard fix did not land.

- [ ] **Step 6: Frontend tests and commit**

Add to `BoardDetail.test.tsx`: turning the checkbox on sends `auto_publish: true`, the
`max_pages` input sends its value, and the preview line renders all four numbers. Then:

```bash
cd <worktree>/frontend && pnpm run lint && pnpm run test
cd <worktree>
uv run pytest -n 16 --dist loadfile   # DELTA +6 on 5449 -> expect 5455, EXACTLY 1 skipped
uv run ruff check src tests alembic scripts
uv run python scripts/gen_architecture.py && git diff --stat -- docs/ARCHITECTURE.md
git add -A && git commit -m "feat(boards): the auto-publish switch, and the numbers behind it"
```

**`+6`, and every term is named — this is the one delta that is not just "the tests I wrote".**

| Term | Why |
|---|---|
| **+4** | the four backend gates in Step 1 |
| **+2** | `test_boards_permission_single_source.py` parametrizes **two** tests over `_board_id_handlers()` (`test_every_board_id_route_calls_board_for_edit` and `test_no_board_id_route_looks_a_board_up_directly`). Adding one `{board_id}`-keyed route auto-creates one instance of each. **Measured in the dry run** — it is the whole reason the first draft's chain missed by 6 at the end. |

The frontend tests are vitest and never enter the pytest count. The first draft's `+3` counted
three backend gates, missed the capacity gate, missed the parametrize, and carried a stale base.

**That second parametrized test is a real constraint on `auto_preview`, not just a counter:**
`test_no_board_id_route_looks_a_board_up_directly` forbids a `{board_id}` handler from doing
`session.get(Board, ...)` or `select(Board...)` itself. Step 4's route calls `_board_for_edit`,
which is what satisfies both halves. Do not "simplify" it to a direct lookup.

**The preview arithmetic is right and was verified: 30 qualifying -> 24 displayed, 6 truncated,
3 pages, 90 s.** Do not re-derive it.

---

## Self-review notes

> **This plan has been through a full dry run.** Every task below was implemented end to end in
> a throwaway worktree at this same base, every mutation was executed, and the 30 defects that
> found are fixed above. Where a number, a citation or a gate is called "measured", it was run.
> Ledger: `/home/parlar_ai/auto-publish-run/PLAN-DEFECTS.md` and `STATUS.md`.

**Spec coverage.** Every section of rev 2 maps to a task: the switch (1, 6); what an auto board
shows (4); threshold-floor / capacity-backstop (4); the four-number preview (6); taking a call
down (1, 3); who writes the rows and the two-pass lock discipline (5); `approved_by` in both the
column and the response model (1, 2); `max_pages` becoming settable, **and the capacity guard it
would otherwise disarm** (6); manual boards untouched (gate 1 of Task 5); publishing stays
separate (nothing in this plan touches `is_published`).

**The spec's four open items — all four now answered, not three.** The first draft said
"three", which miscounted rev 2 and silently skipped the one the spec explicitly asked the plan
to settle.

1. **Churn measurement** — open question, no answer to implement. Do it after Task 5 lands,
   against real data.
2. **Position stability** — open question. `position` is the score rank, which is a decision
   this plan makes and Task 5 states; whether it should instead be sticky is a product call.
3. **Whether the remove control should also offer archiving** — open question, out of scope.
4. **`suppressed_by` and `ON DELETE RESTRICT`** — **ANSWERED in Task 1: RESTRICT, deliberately**,
   with the reasoning and the named consequence written there. This is the one the spec said
   the plan should answer rather than inherit, and the first draft inherited it in silence.

**The alerts wording requirement — checked, and no edit is needed.** The spec says *"the plan
must word the auto-board message so it does not assert a cause it has not established."*
Measured at `99590b97`, `boards/alerts.py` sends exactly:

> subject: `Corridor board "<title>" is short <N> item(s)`
> body: `The corridor board "<title>" currently has <N> fewer approved, still-open item(s) than
> its configured page size.`

That reports a **count**, not a cause — it does not say a human failed to approve anything, does
not name automation, and does not tell the reader what to do. It satisfies the requirement as
written, so this plan changes nothing in `boards/alerts.py` and no task lists it.

Two residuals recorded here rather than fixed, because both are product calls the spec did not
make and neither is a defect in this plan:
- The word "approved" describes an auto board's rows imprecisely (nobody approved them).
  Changing it would reword every manual board's alert too and redden the alerts tests.
- An auto board whose candidate pool is genuinely small will alert every night with no action
  available to its owner. `boards_needing_attention` fires on *published AND underfull*; whether
  auto boards should be exempt, or alert differently, belongs with open item 1's churn data.

**Test-count chain — DELTAS, measured, not a predicted absolute.**

Base **`5428 passed, 1 skipped`** at `99590b97` (152.53 s, re-measured at launch).

| Task | Delta | Running total | Where the delta comes from |
|---|---|---|---|
| 0 | +0 | 5428 | `schema.gen.ts` is a type file; nothing imports it at runtime |
| 1 | +4 | 5432 | 3 migration tests + the round trip. The pinned-column test is **repaired**, not added |
| 2 | +1 | 5433 | one serialization gate |
| 3 | +4 | 5437 | gates 1-4 (gate 4 is new: the board-scoped approve-clear) |
| 4 | +4 | 5441 | four selection tests; `board_candidate_parts` is a pure move and adds none |
| 5 | +8 | 5449 | gates 1-8 (6 and 7 are new: the two-pass lock; 8 is new: per-board isolation) |
| 6 | +6 | **5455** | 4 backend gates **+ 2 auto-created architecture parametrize instances** |

**What is measured and what is derived, stated separately so neither is oversold.** The dry run
measured this chain for its own smaller task set: `5428 -> 5431 -> 5432 -> 5435 -> 5439 -> 5444
-> 5450`, deltas `+3 +1 +3 +4 +5 +6`, reconciled term by term with no number adjusted. The
deltas above are **those measured deltas plus the gates this revision adds** (+1 to Task 1, +1
to Task 3, +3 to Task 5, +1 to Task 6 — which the dry run itself also added and measured as part
of its `+6`). So `5455` is **derived from measured parts, not measured as a total.**

**Measure your own base before you assert any of it.** If a total differs, name the term that
changed. The first draft's `5443` was wrong two ways at once — its own stated deltas summed to
`+19` (i.e. `5447`), and the base under them was stale — which is exactly the failure this
project's CLAUDE.md records from a previous run.

**Type consistency.** `Selected(call_id, url, score)` is defined in Task 4 and consumed in Task
5. `AutoPublishSummary(boards_seen, rows_added, rows_removed, boards_failed)` is defined in Task
5 and used in the scheduler log **and gate 8** in the same task. `board_candidate_parts` returns
`(owner_id, filters, ranking_join)` in Task 4 and is unpacked that way in both callers (as
`_owner_id` in both, because it is dead in both). `qualifying_count` is defined in Task 4 and
consumed in Task 6. `_ranked_call(..., deadline=FUTURE)` is defined in Task 4 and consumed in
Tasks 5 and 6.

**Fixture risk — the trap that bit hardest, and how this revision closes it.**
There is **no `ranked_call_factory`**, and there are **two different `_make_call`s**:

| Helper | Module | Signature |
|---|---|---|
| `_make_board` | `tests/test_boards_qr_at_approval.py` | `(pg_session_factory, *, profile_id, created_by, slug, is_published=True)` |
| `_make_call` | `tests/test_boards_qr_at_approval.py` | `(pg_session_factory, *, external_id, url)` — **url REQUIRED** |
| `_setup`, `_client_for` | `tests/test_boards_qr_at_approval.py` | as the file has them |
| `_make_call` | `tests/web/test_api_board_items.py` | `(pg_session_factory, *, external_id, deadline=FUTURE)` — **no url; derived** |
| `_rank` | `tests/web/test_api_board_items.py` | `(pg_session_factory, *, call_id, profile_id, score)`, fills the 3 NOT NULL columns |
| `_approve_n` | `tests/web/test_api_board_items.py` | `(pg_session_factory, *, board_id, approved_by, n, profile_id=None, min_score=90)` |
| `_create_board` | `tests/web/test_api_board_items.py` | `(client, *, profile_id, ...)` — returns the **RESPONSE**, not an id |
| `_ranked_call` | `tests/test_board_auto_selection.py` | written by Task 4 |

**Importing both `_make_call`s into one module is ruff `F811` and silently changes what the file
can call.** Task 3 uses the qr_at_approval one; Tasks 4, 5 and 6 use the api_board_items one.
**Run the import before writing any body** — a missing or wrong helper is an ERROR, not a
FAILED, and this suite reads mass ERRORs with zero FAILED as a clean run.

**Ordering.** **Task 0 first** — it isolates a `schema.gen.ts` drift that is already on `master`,
so Tasks 2 and 6 regenerate onto a clean base. Then Task 1 (the schema). Task 2 before Task 5
(an auto row with a NULL approver must serialize before a job writes one). Task 3 before Task 4
(`BoardSuppression` is consumed by the selection). Task 4 before Task 5 (the job calls the
selection). Task 6 last: it exposes the switch, and nothing should be switchable until the job
behind it works.

**Things the dry run proved CORRECT. Do not re-derive them, and do not "fix" them.**

- `session.get(Board, id, with_for_update=True)` takes a real row lock (SQLAlchemy 2.0.49),
  proved with a `FOR UPDATE NOWAIT` probe **and** a plain-`get()` control that did not lock.
- `session.merge(BoardSuppression(...))` on the composite PK is a safe upsert: a second merge
  with a different `suppressed_by` does not raise, does UPDATE it, and **`suppressed_at`
  survives unchanged** — `Session.merge()` only overwrites attributes present on the source
  object. Task 3 needs no extra code to preserve it.
- The `0078` downgrade round trip works, including the `DELETE ... WHERE approved_by IS NULL`
  (verified by row count, not by absence of an exception).
- The preview arithmetic: 30 qualifying -> 24 displayed, 6 truncated, 3 pages, 90 s.
- `tests/architecture -q` really is `192 passed`.
- Gate 5, the NOWAIT lock probe, **does** discriminate: `FAILED` with the encode inline
  (`the encode ran under the board lock (3 of 3)`), green after the fix.
- The `monkeypatch.setattr(ap, "qr_for", ...)` seam works — the job calls the module-global.
- `board_candidate_parts` is a pure extraction; no queue test reddens.
- No scheduler test pins an exact job-id set (the existing check is `.issubset()`).
