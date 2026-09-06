# Board Auto-Publish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a corridor board publish ranked calls without a human ticking each one, per board, with an immediate takedown that survives the nightly job.

**Architecture:** One migration adds a flag, a suppression table, and makes `approved_by` nullable. The candidate filters are extracted from `board_queue` so the new selection cannot drift from the admin queue. A nightly job reconciles each auto board's items under the same row lock every other writer takes, encoding QRs outside the transaction. The settings screen gains the flag and a four-number preview.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, Alembic, PostgreSQL 16, APScheduler, pytest (+ testcontainers Postgres), React 19 + Vite + TanStack Query.

**Spec:** `docs/superpowers/specs/2026-09-06-board-auto-publish-design.md` **rev 2**. The spec is INPUT. If it is wrong, that is a `PLAN-DEFECTS.md` entry, not an edit.

## Global Constraints

- **Base:** `master` at `bf5a64a4`.
- **Measured baseline: `5428 passed, 1 skipped`.** Measured on master's exact tree
  (`git rev-parse master^{tree}` = `d219ca54`), not copied. Re-measure before asserting a total.
- **`1 skipped` stays exactly 1.** `0 skipped` means a live Anthropic call ran — stop and
  record it. Never export `ANTHROPIC_API_KEY`.
- **KNOWN BROKEN, NOT YOURS:**
  `tests/proposal_assist/test_turns_submit.py::test_concurrent_submits_cannot_overshoot_cap`
  fails deterministically when run alone or as a file (`got ['ok', 'ok']`), and passes under
  `-n 16` because the timing changes. Measured on master at `bf5a64a4` with none of this
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

## Fixtures — the verified list. Do not re-derive this.

All defined in the ROOT `tests/conftest.py`, so visible to every test under `tests/`:

- `pg_url` — `tests/conftest.py:117`
- `pg_session_factory` — `tests/conftest.py:151`
- `profile_factory` — `tests/conftest.py:184`, returns `(profile_id, owner_user_id)`

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
| `frontend/src/api/schema.gen.ts` | regenerated, never hand-edited | 2, 6 |

---

### Task 1: the migration and the models

**Files:**
- Create: `alembic/versions/0078_board_auto_publish.py`
- Modify: `src/grant_lab/db/models/boards.py`
- Modify: `docs/ARCHITECTURE.md` (generated)
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

- [ ] **Step 1: Write the failing test**

```python
"""Migration 0078: the auto-publish flag, the suppression table, and an
approver that may be absent."""

from __future__ import annotations

from sqlalchemy import inspect, select, text


def test_the_flag_defaults_off_for_every_existing_board(pg_session_factory, profile_factory):
    """Nothing that exists today changes behaviour."""
    pid, owner = profile_factory(name="0078 default profile")
    with pg_session_factory() as s:
        s.execute(
            text(
                "INSERT INTO boards (slug, title, profile_id, created_by, is_published, "
                "orientation, items_per_page, max_pages, page_seconds) "
                "VALUES ('a'*16, 't', :p, :u, false, 'landscape', 8, 3, 30)"
            ),
            {"p": pid, "u": owner},
        )
        s.commit()
        got = s.scalar(text("SELECT auto_publish FROM boards WHERE slug = 'a'*16"))
    assert got is False


def test_approved_by_accepts_null(pg_session_factory, profile_factory):
    """The whole point: an auto-published row has no approver."""
    pid, owner = profile_factory(name="0078 null approver profile")
    with pg_session_factory() as s:
        s.execute(
            text(
                "INSERT INTO boards (slug, title, profile_id, created_by, is_published, "
                "orientation, items_per_page, max_pages, page_seconds) "
                "VALUES ('b'*16, 't', :p, :u, false, 'landscape', 8, 3, 30)"
            ),
            {"p": pid, "u": owner},
        )
        bid = s.scalar(text("SELECT id FROM boards WHERE slug = 'b'*16"))
        src = s.scalar(text("SELECT id FROM sources WHERE slug = 'vinnova'"))
        s.execute(
            text(
                "INSERT INTO calls (source_id, external_id, url, title, deadline) "
                "VALUES (:s, 'x-0078', 'https://example.org/0078', 't', now() + interval '30 days')"
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

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_migration_0078_board_auto_publish.py -v`
Expected: all three FAIL — `auto_publish` and `board_suppressions` do not exist, and the NULL
insert violates a NOT NULL constraint. If they ERROR on a missing fixture instead, that is the
false-pass signature — check the fixture list above, do not "fix" it by inventing one.

- [ ] **Step 3: Write the migration**

```python
"""board auto-publish: the flag, suppressions, and an optional approver

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
uv run pytest tests/test_migration_0078_board_auto_publish.py -v
uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head
uv run alembic heads
```

Expected: 3 passed; the round trip succeeds; `alembic heads` prints exactly `0078 (head)`.

- [ ] **Step 6: Regenerate the architecture map and commit**

```bash
uv run python scripts/gen_architecture.py
uv run pytest tests/architecture -q          # expect: 192 passed
uv run ruff check src tests alembic scripts
git add -A && git commit -m "feat(boards): a board can publish itself, and a card can be kept off one"
```

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

**Blast radius, measured.** `approved_by` appears in the frontend ONLY at
`frontend/src/api/schema.gen.ts:2821` (typed `number`) and in `BoardDetail.test.tsx` fixtures.
**No component renders it.** So this is a regeneration and a fixture update, not a UI change.

- [ ] **Step 1: Write the failing test**

Append to `tests/web/test_api_board_items.py`, in that module's own idiom (read the file first
and reuse whatever it already uses to build an app and a client):

```python
def test_an_item_with_no_approver_serializes(pg_session_factory, profile_factory, monkeypatch, pg_url):
    """GATE. An auto-published row has approved_by = NULL. Listing items must
    return it, not 500 in the response model."""
    from sqlalchemy import text

    # MEASURED at bf5a64a4: this module has `_setup` (:41), `_client_for` (:49),
    # `_create_board` (:72, via the API, needs a client) and `_make_call` (:84,
    # which DERIVES the url from external_id and takes NO url kwarg). It has no
    # `_make_board`. Use what is there.
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

Run: `uv run pytest tests/web/test_api_board_items.py -k null_approver -v`
Expected: FAIL with a pydantic `ValidationError` on `approved_by` — `Input should be a valid
integer`. A 500 in the response body is the same failure; either is the red you want. An ERROR
about a missing `_setup`/`_make_board` means you did not reuse this module's helpers.

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
uv run pytest tests/web/test_api_board_items.py -k null_approver -v
cd frontend && EMBEDDING_MODEL=bge-m3 pnpm run gen:api
git -C /home/parlar_ai/dev/grant-lab diff --stat -- frontend/src/api/schema.gen.ts
```

Expected: PASS; `schema.gen.ts` shows `approved_by: number | null`. If the OpenAPI script name
differs, find it with `ls scripts/ | grep -i openapi` and use the real one — do not skip the
regeneration, because a stale `schema.gen.ts` makes the frontend types lie.

- [ ] **Step 5: Update the frontend fixtures and commit**

`BoardDetail.test.tsx` sets `approved_by: 1` in four places (`:268`, `:285`, `:289`, `:293`).
Leave three as they are and change ONE to `approved_by: null`, so the fixtures cover both
shapes.

```bash
cd frontend && pnpm run lint && pnpm run test
cd /home/parlar_ai/dev/grant-lab
uv run ruff check src tests
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

from sqlalchemy import select

from grant_lab.db.models import BoardItem, BoardSuppression

from tests.test_boards_qr_at_approval import (
    _client_for,
    _make_board,
    _make_call,
    _setup,
)


def test_unapproving_records_a_suppression(monkeypatch, pg_url, pg_session_factory, profile_factory):
    """GATE 1. Deleting the row is not enough -- the 05:00 job would re-add it."""
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="suppression gate1")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="s" * 16)
    call_id = _make_call(pg_session_factory, external_id="sup-1", url="https://example.org/s1")

    client = _client_for(app, owner)
    with client:
        assert client.post(f"/api/boards/{board_id}/items", json={"call_ids": [call_id]}).status_code == 200
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


def test_a_suppression_is_scoped_to_one_board(monkeypatch, pg_url, pg_session_factory, profile_factory):
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
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_board_suppression.py -v`
Expected: gate 1 and gate 3 FAIL because no `BoardSuppression` row is written; gate 2 currently
PASSES (nothing to clear), which is exactly why it is a control and not a gate on its own — it
must keep passing after Step 3, not start passing.

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
uv run pytest tests/test_board_suppression.py -v          # expect 3 passed
```

Then run each mutation, restoring between, and record the ACTUAL result:

| Mutation | Must redden |
|---|---|
| delete the `session.merge(BoardSuppression(...))` line | gate 1 and gate 3 |
| delete the `delete(BoardSuppression)` block in `approve_items` | gate 2 |
| change `BoardSuppression.board_id == board.id` to a bare `True` in the approve clear | gate 3 |

A mutation that reddens nothing is a **finding**, not a pass. Judge on FAILED, never on ERROR.
Restore with `git checkout -- src/` and confirm `git status --porcelain` is empty; a mutation
script that dies leaves the mutant in the tree.

- [ ] **Step 6: Commit**

```bash
uv run pytest -n 16 --dist loadfile     # expect 5428 + 3 = 5431 passed, EXACTLY 1 skipped
uv run ruff check src tests
git add -A && git commit -m "feat(boards): removing a card from one board keeps it off that board"
```

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

In `src/grant_lab/boards/queue.py`, lift lines 50-62 into a function and have `board_queue` call
it:

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
    owner_id, filters, ranking_join = board_candidate_parts(session, board=board)
```

`owner_id` stays bound because the rest of the function may use it; leave everything else in
`board_queue` exactly as it is.

- [ ] **Step 2: Prove the extraction changed nothing**

Run: `uv run pytest tests/ -k board_queue -v` and any file that exercises the queue
(`ls tests | grep -i queue` to find them).
Expected: the same passes as before the edit. A pure move that reddens a test is a move that was
not pure — fix it before continuing.

- [ ] **Step 3: Write the failing selection tests**

```python
"""What an auto board would publish, before anything writes it."""

from __future__ import annotations

from grant_lab.boards.auto_publish import board_selection
from grant_lab.db.models import Board, BoardSuppression

from tests.test_boards_qr_at_approval import _make_board, _make_call


def _board(pg_session_factory, board_id) -> Board:
    with pg_session_factory() as s:
        return s.get(Board, board_id)


def test_the_threshold_is_the_floor(pg_session_factory, profile_factory, ranked_call_factory):
    """A call below min_score never appears, however empty the board is."""
    ...


def test_the_capacity_is_the_ceiling(pg_session_factory, profile_factory, ranked_call_factory):
    """items_per_page * max_pages, best score first. With the defaults that is
    8 * 3 = 24."""
    ...


def test_a_suppressed_call_is_excluded(pg_session_factory, profile_factory, ranked_call_factory):
    """GATE -- this is what makes a takedown stick."""
    ...


def test_an_expired_call_is_excluded(pg_session_factory, profile_factory, ranked_call_factory):
    """The deadline filter, inherited from board_candidate_parts."""
    ...
```

**Most of this helper already exists — do not write it from scratch.** Measured at
`bf5a64a4`, `tests/web/test_api_board_items.py` carries `_make_call(pg_session_factory, *,
external_id, deadline=FUTURE)` at `:84` (it DERIVES the url as
`https://example.org/{external_id}` and takes no `url` kwarg) and `_rank(pg_session_factory, *,
call_id, profile_id, score)` at `:99`, which already fills `rationale`, `model` and
`prompt_version` — the three NOT NULL columns on `CallRanking` that have no usable default
(`db/models/ranking.py:126-133`). Omitting them raises `IntegrityError`, which pytest reports as
an ERROR: this suite's false-pass signature, not a failing test.

So the whole helper is a two-line wrapper, at the top of
`tests/test_board_auto_selection.py`:

```python
from tests.web.test_api_board_items import _make_call, _rank


def _ranked_call(pg_session_factory, *, external_id, profile_id, score):
    """A Call plus its CallRanking against ONE profile. The ranking is what
    `board_candidate_parts` joins on, so a call without one never qualifies.
    The url is derived by `_make_call` as https://example.org/<external_id>."""
    call_id = _make_call(pg_session_factory, external_id=external_id)
    _rank(pg_session_factory, call_id=call_id, profile_id=profile_id, score=score)
    return call_id
```

Verify that import resolves before writing any test body
(`uv run python -c "from tests.web.test_api_board_items import _make_call, _rank"`). If either
helper has moved, find the real one — do not reimplement it, because a second copy of the
`CallRanking` kwargs is how these two files drift apart under a column nobody re-tested.

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

from sqlalchemy import select
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
    from sqlalchemy import func

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

Write each body using `_ranked_call`. The capacity test must insert **more than 24** ranked
calls and assert `len(board_selection(...)) == 24` AND that the 24 returned are the
highest-scoring — asserting only the length passes an implementation that returns the WORST 24.

```bash
uv run pytest tests/test_board_auto_selection.py -v      # expect 4 passed
```

Mutations, restoring between:

| Mutation | Must redden |
|---|---|
| drop `.limit(cap)` | the capacity test |
| drop `Call.id.notin_(suppressed)` | the suppression test |
| change `.desc()` to `.asc()` on the score order | the capacity test's "highest-scoring" assertion |

- [ ] **Step 6: Commit**

```bash
uv run ruff check src tests
git add -A && git commit -m "feat(boards): the cards an auto board should carry, as a query"
```

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

```python
"""The 05:00 job: what it publishes, what it refuses to publish, and what it
does not hold while it works."""

from __future__ import annotations

from sqlalchemy import select, text

from grant_lab.boards.auto_publish import reconcile_auto_boards
from grant_lab.db.models import Board, BoardItem, BoardSuppression

from tests.test_board_auto_selection import _ranked_call
from tests.test_boards_qr_at_approval import _make_board


def _set_auto(pg_session_factory, board_id, on=True):
    with pg_session_factory() as s:
        s.execute(
            text("UPDATE boards SET auto_publish = :v WHERE id = :b"),
            {"v": on, "b": board_id},
        )
        s.commit()


def test_a_manual_board_is_untouched(pg_session_factory, profile_factory):
    """GATE 1 -- the control. Every board that exists when this ships is manual,
    and this job must be invisible to all of them."""
    pid, owner = profile_factory(name="job manual")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="m" * 16)
    _ranked_call(pg_session_factory, external_id="job-m1",
                 profile_id=pid, score=90)
    reconcile_auto_boards(session_factory=pg_session_factory)
    with pg_session_factory() as s:
        assert s.scalars(select(BoardItem).where(BoardItem.board_id == bid)).all() == []


def test_an_auto_board_publishes_qualifying_calls(pg_session_factory, profile_factory):
    """GATE 2. And the row records that no human approved it."""
    pid, owner = profile_factory(name="job auto")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="w" * 16)
    _set_auto(pg_session_factory, bid)
    cid = _ranked_call(pg_session_factory, external_id="job-a1",
                 profile_id=pid, score=90)
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
    cid = _ranked_call(pg_session_factory, external_id="job-s1",
                 profile_id=pid, score=95)
    with pg_session_factory() as s:
        s.add(BoardSuppression(board_id=bid, call_id=cid, suppressed_by=owner))
        s.commit()
    reconcile_auto_boards(session_factory=pg_session_factory)
    with pg_session_factory() as s:
        assert s.get(BoardItem, (bid, cid)) is None, "the 05:00 job undid a takedown"


def test_a_call_that_stops_qualifying_leaves(pg_session_factory, profile_factory):
    """GATE 4. The board is the current best, not an accumulating pile."""
    pid, owner = profile_factory(name="job leaves")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="y" * 16)
    _set_auto(pg_session_factory, bid)
    cid = _ranked_call(pg_session_factory, external_id="job-l1",
                 profile_id=pid, score=90)
    reconcile_auto_boards(session_factory=pg_session_factory)
    with pg_session_factory() as s:
        assert s.get(BoardItem, (bid, cid)) is not None
        s.execute(text("UPDATE boards SET min_score = 95 WHERE id = :b"), {"b": bid})
        s.commit()
    reconcile_auto_boards(session_factory=pg_session_factory)
    with pg_session_factory() as s:
        assert s.get(BoardItem, (bid, cid)) is None


def test_the_encode_does_not_run_under_the_board_lock(pg_session_factory, profile_factory, monkeypatch):
    """GATE 5. Asserting the QR is CORRECT passes with the encode inside the
    write transaction, which is the defect. `FOR UPDATE NOWAIT` from a second
    connection answers 'was the row locked' without a timeout. Same shape as
    tests/test_boards_qr_at_approval.py's gate 12."""
    from sqlalchemy.exc import DBAPIError, OperationalError

    from grant_lab.boards import auto_publish as ap

    pid, owner = profile_factory(name="job lock")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="z" * 16)
    _set_auto(pg_session_factory, bid)
    for i in range(3):
        _ranked_call(pg_session_factory, external_id=f"job-k{i}",
                 profile_id=pid, score=90 - i)

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
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_board_auto_publish_job.py -v`
Expected: all five FAIL on `ImportError: cannot import name 'reconcile_auto_boards'`. That is an
ERROR, not a FAILED — it is the correct red for a function that does not exist yet, but do not
mistake it for a passing gate later. Once the function exists, re-read this list: every one must
be a genuine FAILED before Step 3.

- [ ] **Step 3: Write the job**

Append to `src/grant_lab/boards/auto_publish.py`:

```python
@dataclass
class AutoPublishSummary:
    boards_seen: int = 0
    rows_added: int = 0
    rows_removed: int = 0


def reconcile_auto_boards(*, session_factory) -> AutoPublishSummary:
    """Make every auto board's items match what `board_selection` says.

    Two passes per board. The encode is CPU-bound and must not run under the
    board row lock -- see this module's docstring and the shape
    `approve_items` uses since 5e8329dd.
    """
    summary = AutoPublishSummary()
    with session_factory() as session:
        board_ids = list(
            session.scalars(select(Board.id).where(Board.auto_publish.is_(True))).all()
        )

    for board_id in board_ids:
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

        # PASS 2 -- under the lock, and everything is decided from the LOCKED
        # row. A settings request may have turned auto_publish off, or moved
        # min_score, between the passes; pass 1's answer is advisory only.
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
                            # NULL: nobody approved this. See BoardItem's own
                            # comment on the column.
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
    return summary
```

Add the imports this needs at the top of the module: `dataclass` is already there; add
`Board`, `BoardItem` to the models import and `from grant_lab.boards.qr import QrCode, qr_for`.

**`wanted.values()` is ordered** because `board_selection` returns score order and Python dicts
preserve insertion order — that is what makes `position` the score rank. If you rewrite this
loop, keep that property or `position` becomes arbitrary.

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
        summary = reconcile_auto_boards(session_factory=session_factory)
        log.info(
            "boards.auto_publish.tick.done",
            extra={
                "event": "boards_auto_publish_tick_done",
                "boards_seen": summary.boards_seen,
                "rows_added": summary.rows_added,
                "rows_removed": summary.rows_removed,
            },
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

**Read the surrounding function first.** `session_factory` and `log` must be the names actually
in scope there — copy them from the neighbouring `_reconcile_rankings_tick`, do not assume.
Import `reconcile_auto_boards` at the top of `scheduler.py`.

- [ ] **Step 5: Run the tests and prove the gates bite**

```bash
uv run pytest tests/test_board_auto_publish_job.py -v     # expect 5 passed
```

Mutations, restoring between, judged on FAILED:

| Mutation | Must redden |
|---|---|
| drop `if board is None or not board.auto_publish: continue` in pass 2 | gate 1 |
| in pass 2, use `likely` instead of recomputing under the lock | gate 4 (a settings change between passes is ignored) |
| encode inline: `qr = qr_for(sel.url) if sel.url else None` | gate 5 |
| force `prefetch = {}` before pass 2 | gate 5 |
| drop `Call.id.notin_(suppressed)` in `board_selection` | gate 3 **and** Task 4's suppression test |

- [ ] **Step 6: Commit**

```bash
uv run pytest -n 16 --dist loadfile    # expect 5431 + 5 = 5436 passed, EXACTLY 1 skipped
uv run ruff check src tests alembic scripts
git add -A && git commit -m "feat(boards): a self-publishing board rebuilds itself at 05:00"
```

---

### Task 6: the switch, and the four numbers

**Files:**
- Modify: `src/grant_lab/web/api/boards.py` (`BoardSettingsIn`, `BoardOut`, new preview route)
- Modify: `frontend/src/routes/BoardDetail.tsx:115-140` (the settings form)
- Modify: `frontend/src/api/schema.gen.ts` (regenerated)
- Test: `tests/web/test_api_board_auto_settings.py`, `frontend/src/routes/BoardDetail.test.tsx`

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

- [ ] **Step 1: Write the failing backend tests**

```python
"""The auto-publish switch, and the preview that stops the cap being silent."""

from __future__ import annotations

from tests.test_board_auto_selection import _ranked_call
from tests.test_boards_qr_at_approval import _client_for, _make_board, _setup


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
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/web/test_api_board_auto_settings.py -v`
Expected: gates 1 and 2 FAIL because pydantic's `extra="ignore"` drops the unknown field and the
response still shows the old value; gate 3 FAILS with 404, the route not existing.

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

Add `auto_publish` and `max_pages` to `BoardOut` if they are not already there — read it first.
Then check how `update_board` applies fields; if it uses an explicit allow-list rather than
`model_dump(exclude_unset=True)`, add both names to it, or the fields are accepted and silently
dropped.

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
uv run pytest tests/web/test_api_board_auto_settings.py -v    # expect 3 passed
cd frontend && EMBEDDING_MODEL=bge-m3 pnpm run gen:api
```

In `frontend/src/routes/BoardDetail.tsx`, beside the existing `minScore` / `pageSeconds` state
(`:115-128`), add `autoPublish` and `maxPages` state seeded from `board`, include them in the
save payload (`:134-137`), and render:

- a checkbox **"Publish new calls automatically"**
- when it is on, the preview line, fetched from `/auto-preview` with TanStack Query:
  **"142 qualify · 24 shown · 118 not shown · 3 pages · 90s loop"**
- when the box is being turned ON and the board has items, a warning before save:
  **"This replaces the N cards on this board at the next nightly run."**

Follow this file's existing idiom for state, mutation and error handling — read `:110-205`
first. Do not introduce a new data-fetching pattern.

- [ ] **Step 6: Frontend tests and commit**

Add to `BoardDetail.test.tsx`: turning the checkbox on sends `auto_publish: true`, and the
preview line renders all four numbers. Then:

```bash
cd frontend && pnpm run lint && pnpm run test
cd /home/parlar_ai/dev/grant-lab
uv run pytest -n 16 --dist loadfile     # expect 5436 + 3 = 5439 passed, EXACTLY 1 skipped
uv run ruff check src tests alembic scripts
uv run python scripts/gen_architecture.py && git diff --stat -- docs/ARCHITECTURE.md
git add -A && git commit -m "feat(boards): the auto-publish switch, and the numbers behind it"
```

---

## Self-review notes

**Spec coverage.** Every section of rev 2 maps to a task: the switch (1, 6); what an auto board
shows (4); threshold-floor / capacity-backstop (4); the four-number preview (6); taking a call
down (1, 3); who writes the rows and the two-pass lock discipline (5); `approved_by` in both the
column and the response model (1, 2); `max_pages` becoming settable (6); manual boards untouched
(gate 1 of Task 5); publishing stays separate (nothing in this plan touches `is_published`).

**Not covered, deliberately.** The spec's own three open items — churn measurement, position
stability, and whether the remove control should also offer archiving — are open questions, not
requirements, and none has an answer to implement. The churn measurement should happen after
Task 5 lands, against real data.

**Test-count chain.** `5428` measured -> `+3` (Task 3) -> `+4` (Task 4) -> `+5` (Task 5) -> `+3`
(Task 6) = **`5439`**, plus whatever Tasks 1 and 2 add (3 and 1, so `5443`). **These are
PREDICTED (not run).** Measure at each task and, if a total differs, name the term that changed
rather than adjusting the number.

**Type consistency.** `Selected(call_id, url, score)` is defined in Task 4 and consumed in Task
5. `AutoPublishSummary(boards_seen, rows_added, rows_removed)` is defined in Task 5 and used in
the scheduler log in the same task. `board_candidate_parts` returns
`(owner_id, filters, ranking_join)` in Task 4 and is unpacked that way in both callers.
`qualifying_count` is defined in Task 4 and consumed in Task 6.

**Fixture risk, the known trap.** Tasks 3-6 import `_setup`, `_client_for`, `_make_board` and
`_make_call` from `tests/test_boards_qr_at_approval.py`, and `_ranked_call` from
`tests/test_board_auto_selection.py` (Task 4 writes it). **Verify each import resolves before
writing a test body** — a missing helper is an ERROR, not a FAILED, and this suite's false-pass
signature. `_ranked_call`'s `CallRanking` kwargs were measured at `bf5a64a4`, not guessed.

**Ordering.** Task 1 before everything (the schema). Task 2 before Task 5 (an auto row with a
NULL approver must serialize before a job writes one). Task 4 before Task 5 (the job calls the
selection). Task 3 is independent of 4-6 but its `BoardSuppression` is consumed by Task 4, so
run it before 4. Task 6 last: it exposes the switch, and nothing should be switchable until the
job behind it works.
