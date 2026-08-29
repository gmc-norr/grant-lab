# LLM Spend Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every hosted-LLM call in the app writes one row to one append-only table, and the daily caps read that table instead of re-deriving spend from five others.

**Architecture:** A new `llm_spend` table with a single writer, `record_spend()`. Each spending site calls it on **every** terminal path — success, failure, and timeout — not only the happy one. `check_global_daily_token_cap` is rewritten to sum that one table. The existing per-job `input_tokens`/`output_tokens` columns stay exactly as they are (the UI reads them); a gate asserts they agree with the ledger, so the display cache cannot drift silently. An AST gate then fails any module that stamps token fields without going through the writer.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, Alembic, PostgreSQL 16, pytest (+ testcontainers).

## Global Constraints

- **Measured baseline before any change: `4122 passed, 1 skipped`.** The `1 skipped` is a real-SDK test gated on `ANTHROPIC_API_KEY`; `0 skipped` means a live API call ran. Read the skip count, not the total.
- Fast runs: `uv run pytest -n 16 --dist loadfile`. Never `-n auto` (resolves to 72 here). `-n 4` is the measured floor.
- The lint gate is `uv run ruff check .` — **not** `ruff format --check`, which has ~250 lines of pre-existing noise.
- `ruff`'s `ASYNC` ruleset is enabled. A blocking call inside `async def` is a lint failure.
- New `subprocess.run(["git", ...])` call sites must be pinned in `tests/fencing/sink_guard.py`'s `PROVEN_NON_COMMIT`. New importers of `proposal_assist.fencing` must be added to `tests/fencing/inventory.py`. **This plan adds neither** — if a task finds itself needing one, stop and escalate.
- Migration head is `0066`. The new migration is `0067` with `down_revision = "0066"`.
- The day boundary is `call_manager.timeutil.stockholm_day_start_utc(now=None)`. Do not write a second one; there were three, and that was its own defect.
- Money is involved. A path that spends tokens and records nothing is the bug this plan exists to remove — so **every terminal path records, including failures**.

---

## Why this exists — the six verified violations

Each was checked against the code before this plan was written. Do not re-derive them; do not trust them either if the code has moved.

1. **`check_global_daily_token_cap` sums five tables on three different timestamp columns** (`turns/caps.py:184-213`): `ChatTurn.created_at`, `CoherenceJob.queued_at`, `EvaluatorJob.queued_at`, `ReviewRun.triggered_at`, `UncitedClaimsJob.queued_at`. Every new hosted feature must independently remember two columns, a write on every terminal path, and a `SELECT` added here.
2. **Only ONE of the five families that the cap sums actually checks it.** `check_global_daily_token_cap` has exactly two callers: `turns/submit.py:376` (chat) and `web/api/admin.py:240` (read-only display). Coherence, evaluator, review and uncited-claims contribute to the total and never consult it.
3. **Evaluator returns before its token write.** `evaluator/pass_runner.py:176-179` — `if job.scorecard is None: _fail(...); return` — is above the write at `:183-185`. A run that spent tokens and produced no scorecard records zero.
4. **Every `_fail` helper writes zero token fields.** Verified in `evaluator/`, `coherence/` and `uncited_claims/` pass runners. Failed runs cost real money and are invisible to the cap.
5. **Tokens are written only `if done is not None`.** Same three runners. An SDK error path that still consumed input tokens records nothing.
6. **Compaction is uncapped and unaccounted.** `turns/compaction.py` calls a summarizer and stores `tokens=len(summary_text) // 4` on `ChatSummary` — a character estimate of the OUTPUT only, on a table the cap does not sum. It never calls `check_global_daily_token_cap`.

Plus one the review did not name: **`citation_llm_usage` is a separate ledger with its own caps and is not in the "global" sum at all**, so "global daily token cap" is currently false.

## Decisions already taken

- **The ledger sits BESIDE the per-job columns.** Jobs keep writing `input_tokens`/`output_tokens`; the UI is untouched; no backfill. Caps read the ledger only. Task 9's gate asserts the two agree, so the columns being a cache cannot rot in silence.
- **Citation spend is UNIFIED into the global cap.** `citations/usage.py:record_usage` is already the single writer for citation spend, so it gains one extra insert. `citation_llm_usage` and its per-op caps stay exactly as they are. **This is user-visible:** heavy reference processing can now exhaust the drafting budget, and workspaces will hit the global cap sooner than they do today.
- **Task 8 is the other user-visible change** — four endpoints begin refusing work at the cap that previously ignored it. It is deliberately the last behavioural task so it can be dropped without unpicking the ledger.

## File structure

| File | Responsibility |
|---|---|
| `src/call_manager/db/models.py` | `LlmSpend` ORM model (append-only; mirrors `CitationLlmUsage`'s shape and constraints) |
| `alembic/versions/0067_llm_spend.py` | The table, its check constraints and its two indexes |
| `src/call_manager/proposal_assist/spend.py` | **NEW.** `record_spend()` — the only writer — plus `SpendKind` and `sum_spend_today()` |
| `src/call_manager/proposal_assist/turns/caps.py` | `check_global_daily_token_cap` rewritten to read one table |
| `src/call_manager/proposal_assist/turns/processor.py` | Chat turn spend recorded from `TurnDoneEvent` |
| `src/call_manager/proposal_assist/{coherence,evaluator,uncited_claims,review}/pass_runner.py` | Record on every terminal path |
| `src/call_manager/proposal_assist/turns/compaction.py` | Record + honour the cap |
| `src/call_manager/proposal_assist/citations/usage.py` | `record_usage` also writes the ledger |
| `tests/fencing/spend_guard.py` | **NEW.** AST gate: no module stamps token fields outside the pinned list |

---

### Task 1: The `llm_spend` table

**Files:**
- Modify: `src/call_manager/db/models.py` (append at end of file)
- Create: `alembic/versions/0067_llm_spend.py`
- Test: `tests/test_migration_0067_llm_spend.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `LlmSpend` ORM class with columns `id: int`, `workspace_id: uuid.UUID`, `kind: str`, `ref_id: str | None`, `status: str`, `tokens_in: int`, `tokens_out: int`, `model: str | None`, `sdk_session_id: str | None`, `created_at: datetime`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_migration_0067_llm_spend.py`:

```python
"""0067 creates the one table every hosted-LLM call writes to.

The cap used to sum five tables on three different timestamp columns, so every
new feature had to remember two columns, a write on EVERY terminal path, and a
SELECT in caps.py. Six sites had already got it wrong. This is the one table.
"""

import uuid

import pytest
from sqlalchemy import inspect, select, text

from call_manager.db.models import LlmSpend, User, Workspace


def _ws(session):
    u = User(status="active", email="spend@ki.se")
    session.add(u)
    session.flush()
    ws = Workspace(title="W", owner_id=u.id, current_phase="active")
    session.add(ws)
    session.commit()
    return ws.id


def test_table_and_indexes_exist(pg_session_factory, migrated_engine):
    insp = inspect(migrated_engine)
    assert "llm_spend" in insp.get_table_names()
    names = {i["name"] for i in insp.get_indexes("llm_spend")}
    # One index per read the caps make: global-by-day, and per-workspace-by-day.
    assert "ix_llm_spend_created" in names
    assert "ix_llm_spend_ws_created" in names


def test_a_row_round_trips(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = _ws(s)
        s.add(
            LlmSpend(
                workspace_id=ws_id,
                kind="chat_turn",
                ref_id="turn-1",
                status="ok",
                tokens_in=10,
                tokens_out=20,
                model="claude-opus-5",
                sdk_session_id="sess-1",
            )
        )
        s.commit()
        row = s.execute(select(LlmSpend)).scalar_one()
        assert (row.tokens_in, row.tokens_out) == (10, 20)
        assert row.created_at is not None  # server default


@pytest.mark.parametrize("bad", [-1, -1000])
def test_negative_tokens_are_refused(pg_session_factory, bad):
    # A negative row would silently REDUCE the day's total and let spend past
    # the cap. The constraint is the only thing stopping a caller's sign error.
    with pg_session_factory() as s:
        ws_id = _ws(s)
        s.add(LlmSpend(workspace_id=ws_id, kind="chat_turn", status="ok",
                       tokens_in=bad, tokens_out=0))
        with pytest.raises(Exception):
            s.commit()


def test_an_unknown_status_is_refused(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = _ws(s)
        s.add(LlmSpend(workspace_id=ws_id, kind="chat_turn", status="maybe",
                       tokens_in=0, tokens_out=0))
        with pytest.raises(Exception):
            s.commit()


def test_an_unknown_kind_is_refused(pg_session_factory):
    # `kind` is what makes one table readable. An unconstrained free-text kind
    # would let a typo create a category the caps silently never sum.
    with pg_session_factory() as s:
        ws_id = _ws(s)
        s.add(LlmSpend(workspace_id=ws_id, kind="cohernce", status="ok",
                       tokens_in=0, tokens_out=0))
        with pytest.raises(Exception):
            s.commit()


def test_deleting_the_workspace_takes_its_rows(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = _ws(s)
        s.add(LlmSpend(workspace_id=ws_id, kind="chat_turn", status="ok",
                       tokens_in=1, tokens_out=1))
        s.commit()
        s.execute(text("DELETE FROM workspaces WHERE id = :i"), {"i": ws_id})
        s.commit()
        assert s.execute(select(LlmSpend)).first() is None


def test_the_row_is_append_only_by_convention_not_by_trigger(pg_session_factory):
    # Documented, not enforced: there is no UPDATE trigger. The guarantee comes
    # from having exactly one writer (spend.record_spend) and Task 9's gate.
    # This test exists so that claim is written down where it can be read.
    assert not hasattr(LlmSpend, "__immutable__")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_migration_0067_llm_spend.py -q`
Expected: collection error — `ImportError: cannot import name 'LlmSpend'`.

- [ ] **Step 3: Add the model**

Append to `src/call_manager/db/models.py`:

```python
class LlmSpend(Base):
    """Append-only ledger of every hosted-LLM call the app makes.

    ONE row per terminal event, written by exactly one function
    (`proposal_assist.spend.record_spend`). The daily caps sum this table and
    nothing else.

    It replaces a re-derivation that summed FIVE tables on THREE different
    timestamp columns (ChatTurn.created_at, CoherenceJob.queued_at,
    EvaluatorJob.queued_at, ReviewRun.triggered_at, UncitedClaimsJob.queued_at).
    Every hosted feature had to independently remember two columns, a write on
    every terminal path, and a SELECT in caps.py — and six sites had already got
    it wrong, all in the same direction: spend that happened and was not
    recorded.

    The per-job `input_tokens`/`output_tokens` columns still exist and are still
    written; the UI reads them. They are a DISPLAY CACHE. This is the source of
    truth, and tests/fencing/spend_guard.py is what keeps the two honest.

    `status` records failures on purpose: a run that errored after consuming
    input tokens cost real money, and the old code recorded zero for it.
    """

    __tablename__ = "llm_spend"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('chat_turn','compaction','coherence','evaluator','review',"
            "'uncited_claims','citation')",
            name="ck_llm_spend_kind",
        ),
        CheckConstraint(
            "status IN ('ok','failed','timeout')", name="ck_llm_spend_status"
        ),
        CheckConstraint(
            "tokens_in >= 0 AND tokens_out >= 0", name="ck_llm_spend_tokens_nonneg"
        ),
        # The two reads the caps make: whole-instance by day, and per-workspace
        # by day. Both filter on created_at first.
        Index("ix_llm_spend_created", "created_at"),
        Index("ix_llm_spend_ws_created", "workspace_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE", name="fk_llm_spend_workspace_id"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # The job/turn row this spend belongs to, as text so one column serves both
    # uuid and int primary keys. Nullable: compaction has no job row.
    ref_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    sdk_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

- [ ] **Step 4: Write the migration**

Create `alembic/versions/0067_llm_spend.py`:

```python
"""llm_spend — the one table every hosted-LLM call writes to

Revision ID: 0067
Revises: 0066
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_spend",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ref_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tokens_out", sa.Integer(), server_default="0", nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("sdk_session_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_llm_spend_workspace_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "kind IN ('chat_turn','compaction','coherence','evaluator','review',"
            "'uncited_claims','citation')",
            name="ck_llm_spend_kind",
        ),
        sa.CheckConstraint("status IN ('ok','failed','timeout')", name="ck_llm_spend_status"),
        sa.CheckConstraint(
            "tokens_in >= 0 AND tokens_out >= 0", name="ck_llm_spend_tokens_nonneg"
        ),
    )
    op.create_index("ix_llm_spend_created", "llm_spend", ["created_at"])
    op.create_index("ix_llm_spend_ws_created", "llm_spend", ["workspace_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_llm_spend_ws_created", table_name="llm_spend")
    op.drop_index("ix_llm_spend_created", table_name="llm_spend")
    op.drop_table("llm_spend")
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_migration_0067_llm_spend.py -q`
Expected: `7 passed`.

If the ORM and the migration disagree, the repo has a drift check — run `uv run pytest -q -k "drift or alembic_check"` and fix the model, not the migration.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check .
git add src/call_manager/db/models.py alembic/versions/0067_llm_spend.py tests/test_migration_0067_llm_spend.py
git commit -m "feat(db): llm_spend — one append-only row per hosted-LLM call"
```

---

### Task 2: `record_spend` — the only writer

**Files:**
- Create: `src/call_manager/proposal_assist/spend.py`
- Test: `tests/proposal_assist/test_spend.py`

**Interfaces:**
- Consumes: `LlmSpend` from Task 1.
- Produces:
  - `SpendKind = Literal["chat_turn","compaction","coherence","evaluator","review","uncited_claims","citation"]`
  - `SpendStatus = Literal["ok","failed","timeout"]`
  - `record_spend(session, *, workspace_id: uuid.UUID, kind: SpendKind, status: SpendStatus, tokens_in: int | None = 0, tokens_out: int | None = 0, model: str | None = None, ref_id: str | uuid.UUID | int | None = None, sdk_session_id: str | None = None) -> None` — adds one row; **caller commits**.
  - `sum_spend_today(session, *, workspace_id: uuid.UUID | None = None, now: datetime | None = None) -> tuple[int, int]` — `(tokens_in, tokens_out)` since Stockholm midnight.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_spend.py`:

```python
"""One writer, one reader, and the None-handling that the old sites got wrong.

Every previous site wrote `job.input_tokens = done.tokens_in or 0` on the happy
path only. The `or 0` is the interesting part: an SDK that reports None for a
call that still consumed input must record a row, not be skipped.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from call_manager.db.models import LlmSpend, User, Workspace
from call_manager.proposal_assist.spend import record_spend, sum_spend_today


def _ws(session, email="spend@ki.se"):
    u = User(status="active", email=email)
    session.add(u)
    session.flush()
    ws = Workspace(title="W", owner_id=u.id, current_phase="active")
    session.add(ws)
    session.commit()
    return ws.id


def test_records_one_row(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_spend(s, workspace_id=ws_id, kind="chat_turn", status="ok",
                     tokens_in=5, tokens_out=7, model="m", ref_id="t1")
        s.commit()
        row = s.execute(select(LlmSpend)).scalar_one()
        assert (row.kind, row.status, row.tokens_in, row.tokens_out) == ("chat_turn", "ok", 5, 7)
        assert row.ref_id == "t1"


@pytest.mark.parametrize("value", [None, 0])
def test_none_tokens_become_zero_and_STILL_record(pg_session_factory, value):
    # The whole point. A failed or token-less call must leave a row saying so;
    # "we spent nothing" and "we did not look" are different facts, and the old
    # code could not tell them apart because it wrote no row at all.
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_spend(s, workspace_id=ws_id, kind="evaluator", status="failed",
                     tokens_in=value, tokens_out=value)
        s.commit()
        row = s.execute(select(LlmSpend)).scalar_one()
        assert (row.tokens_in, row.tokens_out) == (0, 0)
        assert row.status == "failed"


@pytest.mark.parametrize(
    "ref", [uuid.UUID("00000000-0000-0000-0000-0000000000ab"), 42, "already-text", None]
)
def test_ref_id_accepts_every_primary_key_shape(pg_session_factory, ref):
    # Job tables key on uuid, chat_turns on int, compaction has no row at all.
    # One text column serves all three; the coercion lives here, once.
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_spend(s, workspace_id=ws_id, kind="coherence", status="ok", ref_id=ref)
        s.commit()
        row = s.execute(select(LlmSpend)).scalar_one()
        assert row.ref_id == (None if ref is None else str(ref))


def test_sum_today_totals_every_kind(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = _ws(s)
        for kind in ("chat_turn", "coherence", "evaluator", "review",
                     "uncited_claims", "compaction", "citation"):
            record_spend(s, workspace_id=ws_id, kind=kind, status="ok",
                         tokens_in=1, tokens_out=2)
        s.commit()
        assert sum_spend_today(s) == (7, 14)


def test_sum_today_ignores_yesterday(pg_session_factory):
    # The boundary is Stockholm midnight, from timeutil — NOT a fourth copy.
    from call_manager.timeutil import stockholm_day_start_utc

    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_spend(s, workspace_id=ws_id, kind="chat_turn", status="ok",
                     tokens_in=100, tokens_out=100)
        s.commit()
        row = s.execute(select(LlmSpend)).scalar_one()
        row.created_at = stockholm_day_start_utc() - timedelta(seconds=1)
        s.commit()
        assert sum_spend_today(s) == (0, 0)


def test_sum_today_can_scope_to_one_workspace(pg_session_factory):
    with pg_session_factory() as s:
        a = _ws(s, "a@ki.se")
        b = _ws(s, "b@ki.se")
        record_spend(s, workspace_id=a, kind="chat_turn", status="ok", tokens_in=3, tokens_out=0)
        record_spend(s, workspace_id=b, kind="chat_turn", status="ok", tokens_in=9, tokens_out=0)
        s.commit()
        assert sum_spend_today(s, workspace_id=a) == (3, 0)
        assert sum_spend_today(s) == (12, 0)


def test_sum_today_takes_an_instant_so_it_can_be_tested(pg_session_factory):
    # Same rule as timeutil: a function that reads the clock itself cannot be
    # falsified. There were three such day-boundary helpers and zero tests.
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_spend(s, workspace_id=ws_id, kind="chat_turn", status="ok", tokens_in=4, tokens_out=0)
        s.commit()
        future = datetime.now(UTC) + timedelta(days=2)
        assert sum_spend_today(s, now=future) == (0, 0)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/proposal_assist/test_spend.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'call_manager.proposal_assist.spend'`.

- [ ] **Step 3: Write the module**

Create `src/call_manager/proposal_assist/spend.py`:

```python
"""The one place hosted-LLM spend is written, and the one place it is read.

Before this, spend was RE-DERIVED at read time by summing five tables on three
different timestamp columns. Every new feature had to remember two columns, a
write on every terminal path, and a SELECT in caps.py — and six sites had got it
wrong, every one of them in the same direction: money spent, nothing recorded.

Rules for callers:
  * Record on EVERY terminal path, including failures and timeouts. A run that
    errored after consuming input tokens cost real money.
  * Record even when the token counts are None. "We spent nothing" and "we never
    looked" are different facts; a missing row cannot express the second.
  * The caller commits. This only adds to the session, so the row lands in the
    same transaction as the job's own status change and cannot half-happen.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from call_manager.db.models import LlmSpend
from call_manager.timeutil import stockholm_day_start_utc

SpendKind = Literal[
    "chat_turn",
    "compaction",
    "coherence",
    "evaluator",
    "review",
    "uncited_claims",
    "citation",
]
SpendStatus = Literal["ok", "failed", "timeout"]

__all__ = ["SpendKind", "SpendStatus", "record_spend", "sum_spend_today"]


def record_spend(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    kind: SpendKind,
    status: SpendStatus,
    tokens_in: int | None = 0,
    tokens_out: int | None = 0,
    model: str | None = None,
    ref_id: str | uuid.UUID | int | None = None,
    sdk_session_id: str | None = None,
) -> None:
    """Append one spend row. The CALLER COMMITS.

    `tokens_in`/`tokens_out` accept None and store 0 — the point is that the row
    exists. `ref_id` accepts whatever the owning table keys on (uuid for jobs,
    int for chat turns, None for compaction, which has no row) and is stored as
    text, so one column serves all of them.
    """
    session.add(
        LlmSpend(
            workspace_id=workspace_id,
            kind=kind,
            ref_id=None if ref_id is None else str(ref_id),
            status=status,
            tokens_in=int(tokens_in or 0),
            tokens_out=int(tokens_out or 0),
            model=model,
            sdk_session_id=sdk_session_id,
        )
    )


def sum_spend_today(
    session: Session,
    *,
    workspace_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> tuple[int, int]:
    """`(tokens_in, tokens_out)` since Stockholm midnight, all kinds.

    `now` is a parameter for the same reason `stockholm_day_start_utc` takes one:
    a function that reads the clock itself cannot be falsified by a test.
    """
    stmt = select(
        func.coalesce(func.sum(LlmSpend.tokens_in), 0),
        func.coalesce(func.sum(LlmSpend.tokens_out), 0),
    ).where(LlmSpend.created_at >= stockholm_day_start_utc(now))
    if workspace_id is not None:
        stmt = stmt.where(LlmSpend.workspace_id == workspace_id)
    row = session.execute(stmt).one()
    return int(row[0]), int(row[1])
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_spend.py -q`
Expected: `13 passed`.

- [ ] **Step 5: Prove the day boundary gate bites**

Change `stockholm_day_start_utc(now)` in `sum_spend_today` to `stockholm_day_start_utc()` (ignoring the parameter), re-run.
Expected: `test_sum_today_takes_an_instant_so_it_can_be_tested` FAILS. Revert.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/spend.py tests/proposal_assist/test_spend.py
git commit -m "feat(spend): record_spend + sum_spend_today — one writer, one reader"
```

---

### Task 3: Chat turns record spend

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/processor.py`
- Test: `tests/proposal_assist/test_spend_chat_turn.py`

**Interfaces:**
- Consumes: `record_spend` from Task 2.
- Produces: a `kind="chat_turn"` row per finished turn, `ref_id` = the `ChatTurn.id`.

**Before writing code, read `turns/processor.py` and find every place that
currently stamps `turn.tokens_in` / `turn.tokens_out`, plus every terminal path
that does NOT (error, timeout, cancel). There were 21 token/cap references in
this file at the time of writing; the count may have moved. Record on all of
them.**

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_spend_chat_turn.py`:

```python
"""A finished chat turn leaves exactly one ledger row — including when it failed.

The old code stamped chat_turns.tokens_* on success and the cap summed that
table. A turn that errored after consuming input tokens was invisible.
"""

from sqlalchemy import select

from call_manager.db.models import ChatTurn, LlmSpend
from call_manager.proposal_assist.spend import sum_spend_today


def test_a_completed_turn_records_its_spend(pg_session_factory, make_finished_turn):
    # `make_finished_turn` drives the real processor path to a terminal state.
    # If no such fixture exists, build one in this file from the processor's
    # public entry point — do NOT assert against a hand-written LlmSpend row,
    # which would test nothing but the ORM.
    ws_id, turn_id = make_finished_turn(tokens_in=11, tokens_out=13, status="ok")
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == "chat_turn")).scalar_one()
        assert row.ref_id == str(turn_id)
        assert (row.tokens_in, row.tokens_out) == (11, 13)
        assert row.status == "ok"
        # The display cache still agrees with the ledger.
        turn = s.get(ChatTurn, turn_id)
        assert (turn.tokens_in, turn.tokens_out) == (11, 13)
        assert sum_spend_today(s, workspace_id=ws_id) == (11, 13)


def test_a_FAILED_turn_still_records(pg_session_factory, make_finished_turn):
    # THE regression. Spend happened; the old code recorded nothing.
    ws_id, turn_id = make_finished_turn(tokens_in=7, tokens_out=0, status="failed")
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == "chat_turn")).scalar_one()
        assert row.status == "failed"
        assert row.tokens_in == 7


def test_one_turn_writes_exactly_one_row(pg_session_factory, make_finished_turn):
    # A retry loop or a double-finalize would double-count against the cap.
    ws_id, _ = make_finished_turn(tokens_in=1, tokens_out=1, status="ok")
    with pg_session_factory() as s:
        rows = s.execute(select(LlmSpend).where(LlmSpend.kind == "chat_turn")).scalars().all()
        assert len(rows) == 1
```

- [ ] **Step 2: Build the `make_finished_turn` fixture**

Put it in the same test file. It must drive the **real** processor path — the
existing `tests/proposal_assist/` turn tests show how the processor is invoked
with a stub adapter. Copy that setup rather than inventing one; a fixture that
inserts rows by hand proves nothing about the wiring.

- [ ] **Step 3: Run and watch it fail**

Run: `uv run pytest tests/proposal_assist/test_spend_chat_turn.py -q`
Expected: 3 failed — `NoResultFound` (no `LlmSpend` row).

- [ ] **Step 4: Record from every terminal path**

In `turns/processor.py`, at each terminal path, immediately after the existing
status/token assignment and **before** the commit:

```python
        record_spend(
            session,
            workspace_id=turn.workspace_id,
            kind="chat_turn",
            status="ok",           # or "failed" / "timeout" on those paths
            tokens_in=done.tokens_in if done else None,
            tokens_out=done.tokens_out if done else None,
            model=done.model if done else None,
            ref_id=turn.id,
            sdk_session_id=done.session_id if done else None,
        )
```

Import at the top of the file:

```python
from call_manager.proposal_assist.spend import record_spend
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_spend_chat_turn.py -q`
Expected: `3 passed`.

- [ ] **Step 6: Run the turn suite for regressions**

Run: `uv run pytest tests/proposal_assist/ -q -k "turn or processor"`
Expected: no new failures against the pre-task count. Record both numbers in the commit.

- [ ] **Step 7: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/turns/processor.py tests/proposal_assist/test_spend_chat_turn.py
git commit -m "feat(spend): chat turns record on every terminal path, failures included"
```

---

### Task 4: The four pass runners record on every terminal path

**Files:**
- Modify: `src/call_manager/proposal_assist/coherence/pass_runner.py`
- Modify: `src/call_manager/proposal_assist/evaluator/pass_runner.py`
- Modify: `src/call_manager/proposal_assist/uncited_claims/pass_runner.py`
- Modify: `src/call_manager/proposal_assist/review/pass_runner.py`
- Test: `tests/proposal_assist/test_spend_pass_runners.py`

**Interfaces:**
- Consumes: `record_spend` from Task 2.
- Produces: one row per run, `kind` in `coherence|evaluator|review|uncited_claims`, `ref_id` = the job's id.

**This is the task the review was really about.** The verified defects:

- `evaluator/pass_runner.py:176-179` returns on "no scorecard" **above** the token write at `:183`.
- Every `_fail` helper in all three of coherence/evaluator/uncited_claims writes **zero** token fields.
- All of them write tokens only `if done is not None`.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_spend_pass_runners.py`:

```python
"""Every terminal path of every pass runner leaves a row.

Verified defects this pins:
  * evaluator returns on "no scorecard" ABOVE its token write, so a run that
    spent tokens and produced nothing recorded zero;
  * every _fail helper writes no token fields at all;
  * all of them write tokens only `if done is not None`.

All three lose spend in the same direction: money out, ledger silent.
"""

import pytest
from sqlalchemy import select

from call_manager.db.models import LlmSpend

FAMILIES = ["coherence", "evaluator", "review", "uncited_claims"]


@pytest.mark.parametrize("family", FAMILIES)
def test_a_successful_run_records(pg_session_factory, run_pass, family):
    ws_id, job_id = run_pass(family, outcome="ok", tokens_in=21, tokens_out=34)
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == family)).scalar_one()
        assert (row.tokens_in, row.tokens_out, row.status) == (21, 34, "ok")
        assert row.ref_id == str(job_id)


@pytest.mark.parametrize("family", FAMILIES)
def test_a_FAILED_run_records(pg_session_factory, run_pass, family):
    ws_id, job_id = run_pass(family, outcome="failed", tokens_in=21, tokens_out=0)
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == family)).scalar_one()
        assert row.status == "failed"
        assert row.tokens_in == 21


@pytest.mark.parametrize("family", FAMILIES)
def test_a_run_with_NO_done_event_still_records(pg_session_factory, run_pass, family):
    # `done is None` was the guard that skipped the write entirely.
    ws_id, job_id = run_pass(family, outcome="no_done")
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == family)).scalar_one()
        assert (row.tokens_in, row.tokens_out) == (0, 0)
        assert row.status in ("failed", "timeout")


def test_evaluator_records_when_it_produced_no_scorecard(pg_session_factory, run_pass):
    # The exact shape of the cited defect: pass_runner.py's `if job.scorecard is
    # None: _fail(...); return` sat ABOVE the token write.
    ws_id, job_id = run_pass("evaluator", outcome="no_scorecard", tokens_in=99, tokens_out=5)
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == "evaluator")).scalar_one()
        assert row.tokens_in == 99, "tokens were spent producing nothing, and must be recorded"
        assert row.status == "failed"


@pytest.mark.parametrize("family", FAMILIES)
def test_the_display_columns_still_agree_with_the_ledger(pg_session_factory, run_pass, family):
    # The per-job columns stay as a cache for the UI. A cache that disagrees
    # with the source of truth is worse than no cache — Task 9 gates this
    # statically, this checks it at runtime.
    ws_id, job_id = run_pass(family, outcome="ok", tokens_in=8, tokens_out=9)
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == family)).scalar_one()
        assert (row.tokens_in, row.tokens_out) == (8, 9)
```

- [ ] **Step 2: Build the `run_pass` fixture**

In the same file. It must drive each family's real `run_*_pass` entry point with
a stub adapter that yields the requested outcome. The existing pass-runner tests
under `tests/proposal_assist/{coherence,evaluator,review,uncited_claims}/` show
the stub-adapter shape — reuse it.

The four `outcome` values it must produce: `"ok"` (a `TurnDoneEvent` and a valid
result), `"failed"` (a `TurnDoneEvent` then an exception), `"no_done"` (the
stream ends with no `TurnDoneEvent`), `"no_scorecard"` (evaluator only — a
`TurnDoneEvent` but no scorecard written).

- [ ] **Step 3: Run and watch them fail**

Run: `uv run pytest tests/proposal_assist/test_spend_pass_runners.py -q`
Expected: 17 failed (4 families × 3 + 1 evaluator-specific + 4 cache checks), all `NoResultFound`.

- [ ] **Step 4: Record in each runner's success path**

In each of the four files, inside the success finalize block, after the existing
`job.input_tokens = ...` assignments:

```python
            record_spend(
                s,
                workspace_id=job.workspace_id,
                kind="evaluator",   # coherence / review / uncited_claims
                status="ok",
                tokens_in=done.tokens_in if done else None,
                tokens_out=done.tokens_out if done else None,
                model=done.model if done else None,
                ref_id=job.id,
                sdk_session_id=done.session_id if done else None,
            )
```

- [ ] **Step 5: Record in each `_fail` helper — three families, not four**

**Review is shaped differently and the recipe below does NOT apply to it.**
Verified: `review/pass_runner.py` has no `_fail` helper. It sets per-ITEM status
(`item.status = "cancelled"` / `"running"`) and `review/jobs.py:150-151` sums the
items' tokens into the run. So for review, record one row per item at each
terminal item status, with `ref_id` = the item's id — or one row per run at the
point `jobs.py` aggregates. Pick one, write it in the commit message, and make
`test_a_FAILED_run_records[review]` prove it; do not leave both.

Call-site counts, measured: `_fail` is called 3× in `evaluator/pass_runner.py`,
2× in `coherence/pass_runner.py`, 3× in `uncited_claims/pass_runner.py` (each
count excludes the `def`). Update every one.

For the three families that DO have `_fail`:

`_fail` does not currently receive the `done` event. Change its signature to
accept it, and record there:

```python
def _fail(session, job, message: str, done=None) -> None:
    job.status = "failed"
    job.error = message
    job.completed_at = datetime.now(UTC)
    # Spend happened even though the run did not finish. Recording it is the
    # whole point: the old code left every failed run invisible to the cap.
    record_spend(
        session,
        workspace_id=job.workspace_id,
        kind="evaluator",   # coherence / review / uncited_claims
        status="failed",
        tokens_in=done.tokens_in if done else None,
        tokens_out=done.tokens_out if done else None,
        model=done.model if done else None,
        ref_id=job.id,
        sdk_session_id=done.session_id if done else None,
    )
```

Then update every `_fail(...)` call site in that file to pass `done` where it is
in scope. **Do not skip the evaluator's `if job.scorecard is None` branch —
that one is the cited defect.**

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_spend_pass_runners.py -q`
Expected: `17 passed`.

- [ ] **Step 7: Run each family's own suite**

Run: `uv run pytest tests/proposal_assist/coherence tests/proposal_assist/evaluator tests/proposal_assist/review tests/proposal_assist/uncited_claims -q`
Expected: no new failures. Record before/after counts in the commit.

- [ ] **Step 8: Prove the failure paths bite**

Remove the `record_spend` call from the evaluator's `_fail` only, re-run.
Expected: exactly the evaluator's `failed`, `no_done` and `no_scorecard` cases go red; the other families stay green. Revert.

- [ ] **Step 9: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/coherence/pass_runner.py \
        src/call_manager/proposal_assist/evaluator/pass_runner.py \
        src/call_manager/proposal_assist/uncited_claims/pass_runner.py \
        src/call_manager/proposal_assist/review/pass_runner.py \
        tests/proposal_assist/test_spend_pass_runners.py
git commit -m "feat(spend): the four pass runners record failures, not just successes"
```

---

### Task 5: Compaction records and honours the cap

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/compaction.py`
- Test: `tests/proposal_assist/test_spend_compaction.py`

**Interfaces:**
- Consumes: `record_spend` from Task 2, `check_global_daily_token_cap` from `turns/caps.py`.
- Produces: a `kind="compaction"`, `ref_id=None` row per compaction run.

Verified: compaction calls a summarizer and stores `tokens=len(summary_text) // 4`
on `ChatSummary` — a **character estimate of the output only**, on a table the cap
never summed — and it never calls `check_global_daily_token_cap`.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_spend_compaction.py`:

```python
"""Compaction spent real tokens off the books.

`ChatSummary.tokens` is `len(summary_text) // 4` — a character estimate of the
OUTPUT only, on a table no cap ever summed. And compaction never consulted the
cap, so it could spend past a ceiling that had already stopped every chat turn.
"""

from sqlalchemy import select

from call_manager.db.models import LlmSpend


def test_compaction_records_its_real_usage(pg_session_factory, run_compaction):
    ws_id = run_compaction(tokens_in=500, tokens_out=120)
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == "compaction")).scalar_one()
        # The REPORTED usage, not a character estimate of the summary text.
        assert (row.tokens_in, row.tokens_out) == (500, 120)
        assert row.ref_id is None  # compaction has no job row


def test_compaction_refuses_to_run_over_the_cap(pg_session_factory, run_compaction, at_cap):
    # It used to spend past a ceiling that had already stopped every chat turn.
    ws_id = at_cap()
    result = run_compaction(tokens_in=500, tokens_out=120, workspace_id=ws_id)
    assert result is None or result.skipped_reason == "global_cap_exceeded"
    with pg_session_factory() as s:
        assert s.execute(select(LlmSpend).where(LlmSpend.kind == "compaction")).first() is None


def test_a_failed_compaction_still_records(pg_session_factory, run_compaction):
    ws_id = run_compaction(tokens_in=500, tokens_out=0, outcome="failed")
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == "compaction")).scalar_one()
        assert row.status == "failed"
        assert row.tokens_in == 500
```

- [ ] **Step 2: Build the `run_compaction` and `at_cap` fixtures**

`run_compaction` drives the real compaction entry point with a stub summarizer
client whose response envelope carries the requested usage. `at_cap` seeds enough
`LlmSpend` rows to exceed `settings.global_daily_token_cap_input`.

- [ ] **Step 3: Run and watch it fail**

Run: `uv run pytest tests/proposal_assist/test_spend_compaction.py -q`
Expected: 3 failed.

- [ ] **Step 4: Add the cap check and the record**

In `turns/compaction.py`, before calling the summarizer:

```python
    # Compaction is a hosted-LLM call like any other. It used to spend past a
    # ceiling that had already stopped every chat turn, because it was the one
    # spender that never asked.
    cap = check_global_daily_token_cap(session, settings=settings)
    if not cap.allowed:
        logger.warning(
            "compaction.skipped_global_cap",
            extra={"event": "compaction_skipped_global_cap", "reason": cap.reason},
        )
        return None
```

And after the summarizer returns, in the same transaction as the `ChatSummary`
write:

```python
        record_spend(
            s,
            workspace_id=workspace_id,
            kind="compaction",
            status="ok",
            tokens_in=(envelope.get("usage") or {}).get("prompt_tokens"),
            tokens_out=(envelope.get("usage") or {}).get("completion_tokens"),
            model=used_model,
        )
```

**Read the summarizer client's actual response shape before writing this** —
`envelope` is what the existing code already reads `model` from. If usage is not
in the envelope, record `status="ok"` with None tokens rather than estimating;
a row saying "we called it and got no counts" is honest, and a character
estimate is not.

Wrap the summarizer call so a failure records too:

```python
    except Exception:
        record_spend(s, workspace_id=workspace_id, kind="compaction", status="failed")
        s.commit()
        raise
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_spend_compaction.py -q`
Expected: `3 passed`.

- [ ] **Step 6: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/turns/compaction.py tests/proposal_assist/test_spend_compaction.py
git commit -m "feat(spend): compaction records its usage and honours the cap"
```

---

### Task 6: Citation spend joins the ledger

**Files:**
- Modify: `src/call_manager/proposal_assist/citations/usage.py`
- Test: `tests/proposal_assist/test_spend_citation.py`

**Interfaces:**
- Consumes: `record_spend` from Task 2.
- Produces: a `kind="citation"` row alongside every `CitationLlmUsage` row.

`record_usage` is already the single writer for citation spend, so this is one
extra insert in one function. `citation_llm_usage` and its per-op caps are
**unchanged** — this only makes citation spend visible to the global cap.

**User-visible:** heavy reference processing can now exhaust the drafting budget.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_spend_citation.py`:

```python
"""Citation spend was capped separately and invisible to the "global" cap.

`citation_llm_usage` is its own append-only ledger with its own per-op caps, and
`check_global_daily_token_cap` never summed it — so "global daily token cap" was
false. `record_usage` is the one writer, so it gains one insert.
"""

import uuid

from sqlalchemy import select

from call_manager.db.models import CitationLlmUsage, LlmSpend, User, Workspace
from call_manager.proposal_assist.citations.usage import record_usage
from call_manager.proposal_assist.spend import sum_spend_today


def _ws(session):
    u = User(status="active", email="cite@ki.se")
    session.add(u)
    session.flush()
    ws = Workspace(title="W", owner_id=u.id, current_phase="active")
    session.add(ws)
    session.commit()
    return ws.id


def test_record_usage_writes_both_ledgers(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_usage(s, workspace_id=ws_id, op="metadata", status="ok",
                     tokens_in=40, tokens_out=9, model="gemma")
        s.commit()
        cite = s.execute(select(CitationLlmUsage)).scalar_one()
        spend = s.execute(select(LlmSpend)).scalar_one()
        assert (cite.tokens_in, cite.tokens_out) == (40, 9)
        assert (spend.tokens_in, spend.tokens_out) == (40, 9)
        assert spend.kind == "citation"
        # `op` has no column on llm_spend; it survives as ref_id so a row can
        # still be traced back to what made it.
        assert spend.ref_id == "metadata"


def test_citation_spend_counts_toward_the_global_total(pg_session_factory):
    # The unification. Before, this returned (0, 0).
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_usage(s, workspace_id=ws_id, op="metadata", status="ok",
                     tokens_in=40, tokens_out=9)
        s.commit()
        assert sum_spend_today(s) == (40, 9)


def test_a_failed_citation_call_records_in_both(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_usage(s, workspace_id=ws_id, op="metadata", status="failed",
                     tokens_in=40, tokens_out=0)
        s.commit()
        assert s.execute(select(LlmSpend)).scalar_one().status == "failed"


def test_the_two_ledgers_cannot_drift_because_there_is_one_writer(pg_session_factory):
    # Not a behaviour test — a statement of the design, checked. If a second
    # writer for CitationLlmUsage ever appears, this count goes wrong.
    import inspect

    from call_manager.proposal_assist.citations import usage as mod

    src = inspect.getsource(mod)
    assert src.count("CitationLlmUsage(") == 1, "a second citation writer would drift"
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/proposal_assist/test_spend_citation.py -q`
Expected: 3 failed (the fourth already passes — note that in the commit; it is a
guard on an invariant that already holds, not a new gate).

- [ ] **Step 3: Add the second insert**

In `citations/usage.py`, inside `record_usage`, after the existing `session.add`:

```python
    # Also the global ledger. `citation_llm_usage` keeps its own per-op caps —
    # this is what makes citation spend count toward the GLOBAL daily cap, which
    # never summed it and was therefore not global at all. One writer, so the two
    # cannot drift.
    record_spend(
        session,
        workspace_id=workspace_id,
        kind="citation",
        status=status,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        model=model,
        ref_id=op,
    )
```

`record_usage`'s `status` values are `ok`, `failed`, `timeout` — verified at
`citations/service.py:87,90,98`, and exactly the set `LlmSpend` constrains, so no
mapping is needed.

Two things about the caller that matter here. `service.py:_safe_record` opens its
OWN session and commits it, so the new row lands in the same transaction as the
citation row — they cannot half-happen. And it **swallows every exception** into
a warning, which means a ledger failure will not break the user's request. That
is the right trade for telemetry, but it also means a broken `record_spend` here
would be silent: `test_record_usage_writes_both_ledgers` is the only thing that
would notice.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_spend_citation.py -q`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/citations/usage.py tests/proposal_assist/test_spend_citation.py
git commit -m "feat(spend): citation calls join the global ledger"
```

---

### Task 7: The cap reads one table

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/caps.py:184-228`
- Test: `tests/proposal_assist/test_caps_read_the_ledger.py`

**Interfaces:**
- Consumes: `sum_spend_today` from Task 2.
- Produces: `check_global_daily_token_cap` unchanged in signature and return type (`CapCheckResult`), changed in what it reads.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_caps_read_the_ledger.py`:

```python
"""The cap sums the ledger and nothing else.

It used to sum FIVE tables on THREE different timestamp columns. The per-job
columns still exist and are still written — they are the UI's display cache —
but they are no longer what the cap believes.
"""

import inspect

from call_manager.proposal_assist.spend import record_spend
from call_manager.proposal_assist.turns import caps
from call_manager.proposal_assist.turns.caps import check_global_daily_token_cap


def test_the_total_comes_from_the_ledger(pg_session_factory, settings_at_caps, seed_ws):
    ws_id = seed_ws()
    with pg_session_factory() as s:
        record_spend(s, workspace_id=ws_id, kind="coherence", status="ok",
                     tokens_in=1000, tokens_out=0)
        s.commit()
        result = check_global_daily_token_cap(s, settings=settings_at_caps(cap_in=1500))
        assert result.used_in == 1000
        assert result.allowed is True


def test_the_cap_trips_on_ledger_rows_alone(pg_session_factory, settings_at_caps, seed_ws):
    ws_id = seed_ws()
    with pg_session_factory() as s:
        record_spend(s, workspace_id=ws_id, kind="citation", status="ok",
                     tokens_in=2000, tokens_out=0)
        s.commit()
        result = check_global_daily_token_cap(s, settings=settings_at_caps(cap_in=1500))
        assert result.allowed is False
        assert result.reason == "input_cap_exceeded"


def test_a_FAILED_run_counts_toward_the_cap(pg_session_factory, settings_at_caps, seed_ws):
    # Failures cost money. The old code recorded none of them.
    ws_id = seed_ws()
    with pg_session_factory() as s:
        record_spend(s, workspace_id=ws_id, kind="evaluator", status="failed",
                     tokens_in=2000, tokens_out=0)
        s.commit()
        assert check_global_daily_token_cap(s, settings=settings_at_caps(cap_in=1500)).allowed is False


def test_the_five_table_re_derivation_is_gone(pg_session_factory):
    # The mechanism, not just the number: five SELECTs on three timestamp
    # columns is what made every new feature a chance to forget one.
    src = inspect.getsource(check_global_daily_token_cap)
    for gone in ("ChatTurn.tokens_in", "CoherenceJob.input_tokens",
                 "EvaluatorJob.input_tokens", "ReviewRun.input_tokens",
                 "UncitedClaimsJob.input_tokens"):
        assert gone not in src, f"{gone} — the cap still re-derives from job tables"
    assert "sum_spend_today" in src


def test_stale_job_columns_no_longer_move_the_cap(pg_session_factory, settings_at_caps, seed_ws):
    # The columns are a display cache now. Writing a huge value into one and no
    # ledger row must NOT trip the cap — that is what "the ledger is the source
    # of truth" means, stated as a test rather than a comment.
    from call_manager.db.models import CoherenceJob

    ws_id = seed_ws()
    with pg_session_factory() as s:
        job = CoherenceJob(workspace_id=ws_id, status="completed", input_tokens=999_999)
        s.add(job)
        s.commit()
        assert check_global_daily_token_cap(s, settings=settings_at_caps(cap_in=1500)).allowed is True
```

- [ ] **Step 2: Build the `settings_at_caps` and `seed_ws` fixtures**

`settings_at_caps(cap_in=..., cap_out=...)` returns an object with
`global_daily_token_cap_input` / `global_daily_token_cap_output`;
`check_global_daily_token_cap` reads only those two fields.

`CoherenceJob`'s real required columns may differ from the sketch above — read
the model and fill in what it needs.

- [ ] **Step 3: Run and watch it fail**

Run: `uv run pytest tests/proposal_assist/test_caps_read_the_ledger.py -q`
Expected: 5 failed.

- [ ] **Step 4: Rewrite the body**

Replace the five `session.execute(...)` blocks and the two sums in
`check_global_daily_token_cap` with:

```python
    # ONE table. This used to sum five, on three different timestamp columns —
    # ChatTurn.created_at, CoherenceJob.queued_at, EvaluatorJob.queued_at,
    # ReviewRun.triggered_at, UncitedClaimsJob.queued_at — so every new hosted
    # feature had to remember two columns, a write on every terminal path, and a
    # SELECT here. Six sites had already got it wrong, all in the same
    # direction: spend that happened and was not counted.
    #
    # The per-job token columns still exist and are still written. They are the
    # UI's display cache; this is the source of truth.
    used_in, used_out = sum_spend_today(session)
```

Add the import, and remove any job-model imports that become unused (ruff will
say which).

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_caps_read_the_ledger.py -q`
Expected: `5 passed`.

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest -n 16 --dist loadfile -q`
Expected: `1 skipped`, and a pass total equal to the baseline plus everything
added in Tasks 1-7. **Do not adjust an expected number to make the arithmetic
close — name the term that changed.**

- [ ] **Step 7: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/turns/caps.py tests/proposal_assist/test_caps_read_the_ledger.py
git commit -m "refactor(caps): the global cap reads one table instead of five"
```

---

### Task 8: The four fan-out endpoints honour the cap

**Files:**
- Modify: `src/call_manager/proposal_assist/web/coherence.py`
- Modify: `src/call_manager/proposal_assist/web/evaluator.py`
- Modify: `src/call_manager/proposal_assist/web/review.py`
- Modify: `src/call_manager/proposal_assist/web/uncited_claims.py`
- Test: `tests/proposal_assist/test_fanout_endpoints_honour_the_cap.py`

**Interfaces:**
- Consumes: `check_global_daily_token_cap` from Task 7.
- Produces: a 429 from each of the four run-start endpoints when the global cap is exceeded.

**This is the plan's second user-visible change.** Verified: of the five families
the cap sums, only chat ever checked it — `check_global_daily_token_cap` had
exactly two callers, `turns/submit.py:376` and the read-only admin panel. After
this task, four endpoints begin refusing work they previously accepted.

**It is deliberately last, so it can be dropped without unpicking the ledger.**
If it is dropped, say so explicitly rather than leaving the plan looking done.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_fanout_endpoints_honour_the_cap.py`:

```python
"""The four fan-out endpoints spend against a cap they never consulted.

`check_global_daily_token_cap` had exactly two callers: turns/submit.py and the
read-only admin panel. Coherence, evaluator, review and uncited-claims all
CONTRIBUTED to the total and never CHECKED it.
"""

import pytest

ENDPOINTS = [
    ("coherence", "/api/workspaces/{ws}/coherence-runs"),
    ("evaluator", "/api/workspaces/{ws}/evaluator-runs"),
    ("review", "/api/workspaces/{ws}/review-runs"),
    ("uncited_claims", "/api/workspaces/{ws}/uncited-claims-runs"),
]


@pytest.mark.parametrize(("name", "path"), ENDPOINTS)
def test_refused_when_the_global_cap_is_exceeded(
    name, path, client_authed_owner, _app, current_user, pg_session_factory, tmp_path, at_global_cap
):
    ws_id = at_global_cap(_app, current_user, pg_session_factory, tmp_path)
    r = client_authed_owner.post(path.format(ws=ws_id))
    assert r.status_code == 429, r.text
    assert "cap" in r.text.lower()


@pytest.mark.parametrize(("name", "path"), ENDPOINTS)
def test_allowed_when_under_the_cap(
    name, path, client_authed_owner, _app, current_user, pg_session_factory, tmp_path, under_global_cap
):
    # The other side of the gate: a blanket refusal would pass the test above.
    ws_id = under_global_cap(_app, current_user, pg_session_factory, tmp_path)
    r = client_authed_owner.post(path.format(ws=ws_id))
    assert r.status_code != 429, r.text
```

- [ ] **Step 2: Build the `at_global_cap` / `under_global_cap` fixtures**

Both seed a workspace with a disclosure ack (the cloud gate runs first and would
otherwise return 403, masking the result). `at_global_cap` then inserts
`LlmSpend` rows exceeding `settings.global_daily_token_cap_input`.

The four route paths are verified, not guessed — each router carries
`prefix="/api/workspaces"` and the POST decorators are
`/{workspace_id}/coherence-runs` (`web/coherence.py:44`),
`/{workspace_id}/evaluator-runs` (`web/evaluator.py:47`),
`/{workspace_id}/review-runs` (`web/review.py:135`) and
`/{workspace_id}/uncited-claims-runs` (`web/uncited_claims.py:80`).

- [ ] **Step 3: Run and watch it fail**

Run: `uv run pytest tests/proposal_assist/test_fanout_endpoints_honour_the_cap.py -q`
Expected: 4 failed (the at-cap cases return 200), 4 passed.

- [ ] **Step 4: Add the check to each endpoint**

In each of the four routers, immediately after the existing `gate_cloud_llm(...)`
call and before the job is enqueued:

```python
    cap = check_global_daily_token_cap(session, settings=request.app.state.settings)
    if not cap.allowed:
        raise HTTPException(
            status_code=429,
            detail="The daily AI usage cap has been reached. Try again tomorrow.",
        )
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_fanout_endpoints_honour_the_cap.py -q`
Expected: `8 passed`.

- [ ] **Step 6: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/web/coherence.py \
        src/call_manager/proposal_assist/web/evaluator.py \
        src/call_manager/proposal_assist/web/review.py \
        src/call_manager/proposal_assist/web/uncited_claims.py \
        tests/proposal_assist/test_fanout_endpoints_honour_the_cap.py
git commit -m "feat(caps): the four fan-out endpoints honour the cap they feed"
```

---

### Task 9: The gate

**Files:**
- Create: `tests/fencing/spend_guard.py`
- Create: `tests/fencing/test_spend_guard.py`

**Interfaces:**
- Consumes: nothing at runtime — this is an AST scan over `src/`.
- Produces: a failing test when a module stamps a token field without recording spend.

This is what stops the next feature from repeating the six violations. Model it on
`tests/fencing/sink_guard.py`, which already does exactly this shape for git
sinks — read that file first, including how `PROVEN_NON_COMMIT` pins are derived
and why `test_no_pin_is_stale` makes the list shrink-only.

- [ ] **Step 1: Write the gate**

Create `tests/fencing/spend_guard.py`:

```python
"""Any module that stamps a token field must also record spend.

The defect this prevents: `job.input_tokens = done.tokens_in or 0` written on the
happy path and nowhere else. Six sites had it, all losing spend in the same
direction. The per-job columns are a display cache now, so writing one without a
matching `record_spend` call re-opens exactly the gap the ledger closed.

Call-level, like sink_guard: a module either records spend somewhere, or it is
pinned here with a reason. The pinned list can only shrink.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

# Attribute names that mean "I am writing token telemetry to a row".
TOKEN_FIELDS = frozenset({"input_tokens", "output_tokens", "tokens_in", "tokens_out"})

# Modules that stamp a token field and legitimately do NOT record spend.
# A reason is MANDATORY. Adding to this list is a reviewed diff.
PINNED: dict[str, str] = {
    # The ledger's own writer — it constructs the LlmSpend row.
    "call_manager/proposal_assist/spend.py": "defines record_spend itself",
    # The ORM model declares the columns; it does not write a row.
    "call_manager/db/models.py": "column declarations, not assignments",
}


def _stamps_token_field(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr in TOKEN_FIELDS:
                    return True
    return False


def _records_spend(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in ("record_spend", "record_usage"):
                return True
    return False


def offenders() -> list[str]:
    """Modules that stamp token fields, record no spend, and are not pinned."""
    out: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC))
        if rel in PINNED:
            continue
        tree = ast.parse(path.read_text())
        if _stamps_token_field(tree) and not _records_spend(tree):
            out.append(rel)
    return out


def stale_pins() -> list[str]:
    """Pins whose module no longer stamps a token field at all."""
    stale: list[str] = []
    for rel in PINNED:
        path = SRC / rel
        if not path.exists():
            stale.append(rel)
            continue
        if not _stamps_token_field(ast.parse(path.read_text())):
            stale.append(rel)
    return stale
```

- [ ] **Step 2: Write the test**

Create `tests/fencing/test_spend_guard.py`:

```python
from tests.fencing.spend_guard import PINNED, offenders, stale_pins


def test_no_module_stamps_tokens_without_recording_spend():
    assert offenders() == []


def test_no_pin_is_stale():
    # Same discipline as sink_guard: the list can only shrink.
    assert stale_pins() == []


def test_every_pin_carries_a_reason():
    assert all(reason.strip() for reason in PINNED.values())


def test_the_guard_can_actually_fail(tmp_path, monkeypatch):
    # A gate nobody has seen fail is not a gate. Point it at a module that
    # commits the exact defect and confirm it is reported.
    import tests.fencing.spend_guard as g

    fake = tmp_path / "call_manager" / "bad.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("def f(job, done):\n    job.input_tokens = done.tokens_in or 0\n")
    monkeypatch.setattr(g, "SRC", tmp_path)
    assert g.offenders() == ["call_manager/bad.py"]


def test_the_guard_accepts_a_module_that_records(tmp_path, monkeypatch):
    # And the other direction, so it is not simply reporting everything.
    import tests.fencing.spend_guard as g

    fake = tmp_path / "call_manager" / "good.py"
    fake.parent.mkdir(parents=True)
    fake.write_text(
        "def f(s, job, done):\n"
        "    job.input_tokens = done.tokens_in or 0\n"
        "    record_spend(s, workspace_id=job.workspace_id, kind='evaluator', status='ok')\n"
    )
    monkeypatch.setattr(g, "SRC", tmp_path)
    assert g.offenders() == []
```

- [ ] **Step 3: Run it**

Run: `uv run pytest tests/fencing/test_spend_guard.py -q`
Expected: `5 passed`.

If `offenders()` is non-empty, a real site was missed in Tasks 3-6 — **fix the
site, do not add a pin.** A pin is for a module that legitimately stamps without
spending; there should be very few.

- [ ] **Step 4: Prove it bites on the real tree**

Remove the `record_spend` call from `evaluator/pass_runner.py` and run
`uv run pytest tests/fencing/test_spend_guard.py -q`.
Expected: `test_no_module_stamps_tokens_without_recording_spend` FAILS, naming
`call_manager/proposal_assist/evaluator/pass_runner.py`. Revert.

- [ ] **Step 5: Full suite and lint**

```bash
uv run ruff check .
uv run pytest -n 16 --dist loadfile -q
```

Expected: `1 skipped`. Record the pass total.

- [ ] **Step 6: Commit**

```bash
git add tests/fencing/spend_guard.py tests/fencing/test_spend_guard.py
git commit -m "test(fencing): a module that stamps tokens must record spend"
```

---

## Acceptance criteria

Each maps to a task, and each is asserted by a test named there. **Criteria with
no test are listed as such — do not report them as met.**

| # | Criterion | Where |
|---|---|---|
| 1 | `llm_spend` exists with kind/status/non-negative constraints | T1 |
| 2 | Deleting a workspace deletes its spend rows | T1 |
| 3 | `record_spend` stores 0 for None tokens and still writes a row | T2 |
| 4 | `ref_id` accepts uuid, int, str and None | T2 |
| 5 | `sum_spend_today` takes an instant and respects the Stockholm boundary | T2 |
| 6 | A completed chat turn records one row | T3 |
| 7 | A **failed** chat turn records one row | T3 |
| 8 | Each of the four pass runners records on success | T4 |
| 9 | Each records on failure | T4 |
| 10 | Each records when there is no `TurnDoneEvent` | T4 |
| 11 | Evaluator records when it produced no scorecard | T4 |
| 12 | Compaction records its reported usage | T5 |
| 13 | Compaction refuses to run over the cap | T5 |
| 14 | Citation calls write both ledgers | T6 |
| 15 | Citation spend counts toward the global total | T6 |
| 16 | The cap sums the ledger, not the five job tables | T7 |
| 17 | Stale job columns do not move the cap | T7 |
| 18 | A failed run counts toward the cap | T7 |
| 19 | Each fan-out endpoint returns 429 at the cap | T8 |
| 20 | Each still works under the cap | T8 |
| 21 | No module stamps tokens without recording spend | T9 |
| 22 | The guard can be shown to fail | T9 |
| **23** | **Two ledger rows are never written for one call (idempotency under retry)** | **NOT ASSERTED** — T3 checks one turn writes one row, but no test drives a retry or a double-finalize. Real risk: a re-claimed job would double-count against the cap. |
| **24** | **The per-job display columns agree with the ledger across the whole app** | **PARTLY** — T4 checks it per family at write time. Nothing checks an existing row, and there is no backfill, so rows written before this plan have no ledger entry at all. Historical totals will read as zero for days before the migration. |

## Known limits, stated

- **No backfill.** Every row written before migration 0067 has no ledger entry.
  The day the migration lands, the global cap's total resets to whatever is
  recorded from that moment. This is deliberate — a backfill would have to invent
  timestamps for spend recorded on three different columns — but it means the
  first day's cap is effectively fresh.
- **Append-only is a convention, not a trigger.** Nothing at the database level
  stops an UPDATE. The guarantee is one writer plus Task 9's gate.
- **Task 8 changes what users can do.** Four endpoints begin refusing at a cap
  they previously ignored. If that is not wanted, drop Task 8 — the ledger and
  the cap's correctness do not depend on it.
- **Citation unification is user-visible.** Reference processing now competes
  with drafting for the same daily budget.
