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
5. **Tokens are written only `if done is not None`.** Same three runners — and review has the identical shape one level down, at the item write (`review/pass_runner.py:160`). An SDK error path that still consumed input tokens records nothing. Worse than the guard itself: `done` is bound only when the inner coroutine RETURNS, so an exception arriving after the done event throws the event away in all four (Task 4 Step 3b).
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
| `src/call_manager/proposal_assist/turns/processor.py` | Chat turn spend recorded from `TurnDoneEvent` on the success finalize |
| `src/call_manager/proposal_assist/turns/outcomes.py` | Chat turn spend recorded on every FAILED/CANCELLED terminal path — the choke point five other modules route through |
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
Expected: `8 passed`. Seven test functions, but `test_negative_tokens_are_refused`
is parametrized over `[-1, -1000]`, so pytest collects eight. (This line said `7`
until review: that was a function count, not a collection count.)

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
Expected: `11 passed`. Seven test functions, two of them parametrized:
`test_none_tokens_become_zero_and_STILL_record` over `[None, 0]` (2 cases) and
`test_ref_id_accepts_every_primary_key_shape` over a 4-element list (4 cases), so
5 + 2 + 4 = 11. (This line said `13` until review: that added the parametrized
cases on top of the function count instead of replacing it.)

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
- Modify: `src/call_manager/proposal_assist/turns/outcomes.py`
- Test: `tests/proposal_assist/test_spend_chat_turn.py`

**Interfaces:**
- Consumes: `record_spend` from Task 2.
- Produces: a `kind="chat_turn"` row per finished turn, `ref_id` = the
  **`TurnJob.id`** (stored as text).

**`ref_id` is the `TurnJob.id`, not a `ChatTurn.id` — decided, not open.** One
`TurnJob` row is exactly one hosted call. A turn produces up to two `ChatTurn`
rows (the user row and an assistant row), and the failure paths write a `system`
row instead of an assistant one, so there is no `ChatTurn.id` available at all
three recording points below. `turn_job_id` is a parameter of all three.

**Chat-turn spend has THREE recording points, not one.** Measured against the
tree:

| Function | Defined at | Terminal path it owns |
|---|---|---|
| `_finalize_success` | `turns/processor.py:559` | the turn completed AND won the conditional `running→completed` UPDATE |
| `record_terminal_outcome` | `turns/outcomes.py:41` | every FAILED / CANCELLED transition, called from five modules |
| `stamp_post_terminal_usage` | `turns/outcomes.py:154` | a turn terminalized mid-stream whose usage arrived afterwards |

**`turns/outcomes.py` is in scope because of what Task 7 does.** Today a failed
or cancelled turn's consumed tokens are stamped onto a `ChatTurn` row by
`record_terminal_outcome` (`outcomes.py:96, 113-114, 122, 130-131`) and the cap
sees them because the cap sums `ChatTurn.tokens_{in,out}`. After Task 7 the cap
reads ONLY `llm_spend`. So without a `record_spend` here, **every failed and
cancelled chat turn becomes invisible to the cap** — violation #4 from the list
above, reintroduced by the change meant to remove it.

`record_terminal_outcome` has SEVEN call sites across FIVE modules (measured with
`grep -rn "record_terminal_outcome" src/`), only two of which are in
`processor.py`:

- `src/call_manager/proposal_assist/turns/processor.py:268` (queued turn in a retired workspace) and `:686` (`_finalize_failed`)
- `src/call_manager/proposal_assist/turns/reaper.py:73` (`reap_stuck_turns`)
- `src/call_manager/proposal_assist/drafting/orphan_sweep.py:33`
- `src/call_manager/web/api/turns.py:381` (owner cancel route)
- `src/call_manager/workspaces.py:204` and `:227` (workspace soft-delete)

**Record INSIDE the two `outcomes.py` functions, NOT at the seven call sites.**
That is this plan's own "one writer" rule applied one level down: `outcomes.py`
is already the single choke point every terminal transition routes through (its
own module docstring, `outcomes.py:3-6`, says so), and five of the seven callers
live in modules no task in this plan otherwise touches.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_spend_chat_turn.py`:

```python
"""A finished chat turn leaves exactly one ledger row — including when it failed.

The old code stamped chat_turns.tokens_* on success and the cap summed that
table. A turn that errored after consuming input tokens was invisible.

`ref_id` is the TurnJob id: one TurnJob is one hosted call, and it is the only
identity in scope at all three recording points (_finalize_success,
record_terminal_outcome, stamp_post_terminal_usage).
"""

from sqlalchemy import select

from call_manager.db.models import ChatTurn, LlmSpend
from call_manager.proposal_assist.spend import sum_spend_today
from call_manager.proposal_assist.turns.outcomes import record_terminal_outcome
from call_manager.proposal_assist.turns.reaper import reap_stuck_turns


def _chat_rows(session):
    return (
        session.execute(select(LlmSpend).where(LlmSpend.kind == "chat_turn"))
        .scalars()
        .all()
    )


def test_a_completed_turn_records_its_spend(pg_session_factory, make_finished_turn):
    # `make_finished_turn` drives the real processor path to a terminal state.
    # If no such fixture exists, build one in this file from the processor's
    # public entry point — do NOT assert against a hand-written LlmSpend row,
    # which would test nothing but the ORM.
    ws_id, job_id, assistant_id = make_finished_turn(
        tokens_in=11, tokens_out=13, status="ok"
    )
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == "chat_turn")).scalar_one()
        assert row.ref_id == str(job_id)
        assert (row.tokens_in, row.tokens_out) == (11, 13)
        assert row.status == "ok"
        # The display cache still agrees with the ledger. On the success path the
        # SDK totals land on the ASSISTANT row and the user row's submit-time
        # pre-count is cleared (processor.py:583-585, :593-594).
        turn = s.get(ChatTurn, assistant_id)
        assert (turn.tokens_in, turn.tokens_out) == (11, 13)
        assert sum_spend_today(s, workspace_id=ws_id) == (11, 13)


def test_a_FAILED_turn_still_records(pg_session_factory, make_finished_turn):
    # THE regression. Spend happened; the old code recorded nothing in a ledger,
    # and Task 7 is about to stop the cap reading the column that carried it.
    ws_id, job_id, _ = make_finished_turn(tokens_in=7, tokens_out=0, status="failed")
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == "chat_turn")).scalar_one()
        assert row.ref_id == str(job_id)
        assert row.status == "failed"
        assert row.tokens_in == 7


def test_one_turn_writes_exactly_one_row(pg_session_factory, make_finished_turn):
    # A retry loop or a double-finalize would double-count against the cap.
    make_finished_turn(tokens_in=1, tokens_out=1, status="ok")
    with pg_session_factory() as s:
        assert len(_chat_rows(s)) == 1


def test_a_turn_reaped_before_finalize_still_records_exactly_one_row(
    pg_session_factory, make_finished_turn
):
    # _finalize_success's conditional running→completed UPDATE matches zero rows
    # when the reaper got there first; it then calls s.rollback() and returns
    # False (processor.py:621-630), which DISCARDS any record_spend written above
    # it. The caller reconciles through _reconcile_lost_usage →
    # stamp_post_terminal_usage (processor.py:496, :512, :532), and that is the
    # branch that must carry the row. One row — not zero, and not two.
    ws_id, job_id, _ = make_finished_turn(
        tokens_in=5, tokens_out=6, status="ok", reap_before_finalize=True
    )
    with pg_session_factory() as s:
        rows = _chat_rows(s)
        assert len(rows) == 1
        assert rows[0].ref_id == str(job_id)
        assert (rows[0].tokens_in, rows[0].tokens_out) == (5, 6)


def test_a_reaped_turn_records_its_consumed_tokens(
    pg_session_factory, make_running_turn_job
):
    # The REAPER path, not the processor path: reaper.py:73 is one of the five
    # record_terminal_outcome call sites outside processor.py. Before this task
    # its spend reached the cap only via ChatTurn.tokens_*, which Task 7 stops
    # reading.
    ws_id, job_id = make_running_turn_job(started_seconds_ago=3600)
    reap_stuck_turns(session_factory=pg_session_factory, ttl_seconds=60)
    with pg_session_factory() as s:
        rows = _chat_rows(s)
        assert len(rows) == 1
        assert rows[0].ref_id == str(job_id)
        assert rows[0].status == "failed"


def test_an_already_terminal_turn_records_nothing_twice(
    pg_session_factory, make_running_turn_job
):
    # record_terminal_outcome's conditional UPDATE returns no row when the job is
    # already terminal, and the function returns False having written nothing
    # (outcomes.py:78-79). record_spend must sit BELOW that guard: it is the only
    # thing standing between a double-finalize and a double-charge, because
    # llm_spend has no unique call-identity constraint.
    ws_id, job_id = make_running_turn_job(started_seconds_ago=0)
    for _ in range(2):
        with pg_session_factory() as s:
            record_terminal_outcome(
                s,
                turn_job_id=job_id,
                workspace_id=ws_id,
                outcome="failed",
                expected_status="running",
                raw_error="boom",
                emit_chat=True,
                tokens_in=4,
                tokens_out=2,
            )
            s.commit()
    with pg_session_factory() as s:
        rows = _chat_rows(s)
        assert len(rows) == 1
        assert (rows[0].tokens_in, rows[0].tokens_out) == (4, 2)
```

- [ ] **Step 2: Build the `make_finished_turn` and `make_running_turn_job` fixtures**

Put both in the same test file.

`make_finished_turn(*, tokens_in, tokens_out, status, reap_before_finalize=False)`
must drive the **real** processor path — `tests/proposal_assist/test_turns_processor.py`
shows how `process_turn_once` is invoked with a stub adapter. Copy that setup
rather than inventing one; a fixture that inserts rows by hand proves nothing
about the wiring. It returns `(workspace_id, turn_job_id, assistant_chat_turn_id)`
— **three** values, because the ledger keys on the `TurnJob` while the
display-cache assertion reads the assistant `ChatTurn`.

`reap_before_finalize=True` must flip the `turn_jobs` row to a terminal status
AFTER the stub adapter has yielded its `TurnDoneEvent` and BEFORE
`_finalize_success` runs, so the conditional UPDATE at `processor.py:616-620`
matches zero rows. The simplest way is to call `record_terminal_outcome(...,
outcome="failed", expected_status="running")` in its own session from inside the
stub's `send_turn` generator, immediately after it yields the `TurnDoneEvent`.

`make_running_turn_job(*, started_seconds_ago)` inserts a workspace and a
`turn_jobs` row with `status="running"` and `started_at = now(UTC) -
started_seconds_ago`, and returns `(workspace_id, turn_job_id)`. It does **not**
drive the processor: the last two tests exercise `reaper.py` and `outcomes.py`
directly, which is the point — those are the paths no processor test reaches.

- [ ] **Step 3: Run and watch it fail**

First derive the count instead of predicting it:
`uv run pytest tests/proposal_assist/test_spend_chat_turn.py --collect-only -q | tail -1`.
Do not hand-arithmetic it — two of this plan's originally-stated counts were
wrong that way (Tasks 1 and 2).

Then: `uv run pytest tests/proposal_assist/test_spend_chat_turn.py -q`
Expected: every collected test fails. The `.scalar_one()` tests fail with
`NoResultFound` (no `LlmSpend` row at all); the three `len(rows) == 1` tests fail
with `assert 0 == 1`.

- [ ] **Step 4: Record at the three choke points**

Import in **both** modified files:

```python
from call_manager.proposal_assist.spend import record_spend
```

**4a — `turns/processor.py`, `_finalize_success` (def at `:559`).** Place the
call inside the WON branch: after the `if (claimed.rowcount or 0) == 0:` block
(`:621-630`) and before `s.commit()` at `:662`. Every name it uses is already a
parameter of the function (`:562-571`).

```python
        # The conditional running→completed UPDATE above is this turn's
        # linearization point, so the record goes BELOW it. Recording above would
        # be discarded: the lost branch calls s.rollback() and returns False
        # (:622, :630), and the caller then reconciles through
        # stamp_post_terminal_usage (4c), which records instead. Exactly one row
        # on either branch.
        record_spend(
            s,
            workspace_id=workspace_id,
            kind="chat_turn",
            status="ok",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            ref_id=turn_job_id,
            sdk_session_id=session_id,
        )
```

**4b — `turns/outcomes.py`, `record_terminal_outcome` (def at `:41`).** Place the
call immediately before `return True` at `:146`, below the `WorkspaceEvent` add.

```python
    # Every FAILED/CANCELLED chat turn, from all seven call sites in five
    # modules. `outcome` is 'failed' | 'cancelled'; SpendStatus has no
    # 'cancelled', and a cancelled turn that consumed input cost exactly what a
    # failed one did, so both map to "failed".
    #
    # This sits BELOW the `if row is None: return False` guard at :78-79 on
    # purpose. That guard is the already-terminal case: the conditional UPDATE
    # matched no row, this call performed no transition, and a row here would be
    # the double-count the ledger has no unique constraint against.
    record_spend(
        session,
        workspace_id=workspace_id,
        kind="chat_turn",
        status="failed",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        ref_id=turn_job_id,
    )
    return True
```

`tokens_in`/`tokens_out` are already parameters (`:50-51`) defaulting to 0, so a
reap or a cancel that consumed nothing writes a truthful zero row rather than
nothing at all. **Do NOT gate the call on the `consumed` flag computed at `:81`**
— "we spent nothing" and "we never looked" are exactly the two facts this ledger
exists to tell apart.

**4c — `turns/outcomes.py`, `stamp_post_terminal_usage` (def at `:154`).** Place
the call immediately before `return True` at `:232`.

```python
    # A turn terminalized mid-stream whose real usage arrived afterwards. Its
    # record_terminal_outcome row (4b) carried the tokens known at the time —
    # zero on the usage_unknown soft-delete path (workspaces.py:227, which passes
    # no token counts) — and the ledger is append-only, so this row ADDS the
    # reported usage. The column write above REPLACES; the ledger SUMS; both end
    # at the same total.
    record_spend(
        session,
        workspace_id=workspace_id,
        kind="chat_turn",
        status="failed",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        model=model,
        ref_id=turn_job_id,
    )
    return True
```

This also sits below both early returns — `:184-185` (not terminal) and
`:194-195` (already reconciled) — which together are this function's hand-rolled
double-stamp guard, serialized on the `TurnJob` row with `SELECT ... FOR UPDATE`
because JSONB gives no uniqueness (`:174-177`). The ledger inherits that guard by
placement, not by constraint.

**Together these three cover every combination exactly once.** Success and won →
4a. Success but lost the row → 4a rolls back, `_reconcile_lost_usage`
(`processor.py:496, :512, :529-542`) fires only when `won` is False, → 4c. Failed
or cancelled → 4b. Failed/cancelled then late usage → 4b (zero or known tokens) +
4c (the reported usage), which sum to the truth.

**No `"timeout"` status for chat turns, and that is deliberate.** The chat-turn
time bound lives in `turns/drain.py:106` and is caught at `:110`, outside both
files this task modifies, and it reaches `processor.py` as a `CancelledError`
through the generic `except BaseException` at `:439`. No branch in either file
can distinguish a timeout from any other failure, so every non-success chat-turn
row is `"failed"`. That is stated in "Known limits, stated" rather than guessed
at here.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_spend_chat_turn.py -q`
Expected: the count collected in Step 3, all passing, `0 failed`.

- [ ] **Step 6: Run the turn suite for regressions**

Run: `uv run pytest tests/proposal_assist/ -q -k "turn or processor or reaper or outcome"`
and `uv run pytest tests/proposal_assist/test_api_workspaces_delete.py tests/proposal_assist/test_api_turns_cancel.py -q`.

The `-k` filter is widened because this task changes `turns/outcomes.py`, which
the soft-delete path (`workspaces.py:204`, `:227`), the cancel route
(`web/api/turns.py:381`), the reaper (`turns/reaper.py:73`) and the orphan sweep
(`drafting/orphan_sweep.py:33`) all call. `-k "turn or processor"` alone does not
reach those files' tests.

Expected: no new failures against the pre-task counts. Record before/after in the commit.

- [ ] **Step 7: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/turns/processor.py \
        src/call_manager/proposal_assist/turns/outcomes.py \
        tests/proposal_assist/test_spend_chat_turn.py
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
- Produces:
  - **coherence / evaluator / uncited_claims:** one row per run, `ref_id` = the job's id.
  - **review:** one row per `ReviewRunItem`, `ref_id` = the item's id. One row per
    run is **not** an option here. A review run makes up to SIX independent hosted
    calls — `REVIEW_KINDS` is a 6-tuple (`review/personas.py:14-21`), five
    concurrent wave-1 calls at `review/pass_runner.py:293-294` plus consolidation
    at `:303-312`, each building its own adapter (`review/agent.py`). A run-level
    row would put six calls behind one `status` and one `ref_id`, and a review
    run's own status can be `"partial"` (`review/pass_runner.py:372`), which is
    not a legal `SpendStatus` at all. See Step 5b.

**This is the task the review was really about.** The verified defects:

- `evaluator/pass_runner.py:177-180` returns on "no scorecard" **above** the token write at `:183-187`.
- Every `_fail` helper in all three of coherence/evaluator/uncited_claims writes **zero** token fields (`coherence:177-181`, `evaluator:31-35`, `uncited_claims:36-40`).
- All of them write tokens only `if done is not None` — and `done` is assigned only when the inner coroutine RETURNS (`coherence:113`, `evaluator:139`, `uncited_claims:154`, `review:131`), so an exception raised after the done event arrives discards it. Step 3b fixes that before the recording steps can rely on it.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_spend_pass_runners.py`:

```python
"""Every terminal path of every pass runner leaves a row.

Verified defects this pins:
  * evaluator returns on "no scorecard" ABOVE its token write, so a run that
    spent tokens and produced nothing recorded zero;
  * every _fail helper writes no token fields at all;
  * all of them write tokens only `if done is not None`, and `done` was bound
    only when the inner coroutine RETURNED, so an exception after the done event
    threw the event away (Step 3b);
  * `status` has three legal values and no recipe could produce the third.

All of them lose spend in the same direction: money out, ledger silent.

FAMILIES excludes review deliberately. Review is a six-call fan-out that records
one row per ReviewRunItem, so the `.scalar_one()` shape below cannot hold for it;
its tests are the second half of this file.
"""

import pytest
from sqlalchemy import select

from call_manager.db.models import (
    CoherenceJob,
    EvaluatorJob,
    LlmSpend,
    ReviewRunItem,
    UncitedClaimsJob,
)

FAMILIES = ["coherence", "evaluator", "uncited_claims"]

JOB_MODEL = {
    "coherence": CoherenceJob,
    "evaluator": EvaluatorJob,
    "uncited_claims": UncitedClaimsJob,
}


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
def test_a_TIMED_OUT_run_records_timeout(pg_session_factory, run_pass, family):
    # `status` is CHECK-constrained to ok|failed|timeout, and before this test no
    # line in the plan could ever produce the third — while the source computes
    # the distinction one branch earlier (`isinstance(exc, (asyncio.TimeoutError,
    # TimeoutError))` at coherence:118-121, evaluator:144-148,
    # uncited_claims:159-163) and then threw it away. Equality, not a
    # `status in ("failed", "timeout")` disjunction that passes on "failed".
    ws_id, job_id = run_pass(family, outcome="timeout", tokens_in=21, tokens_out=0)
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == family)).scalar_one()
        assert row.status == "timeout"
        assert row.tokens_in == 21


@pytest.mark.parametrize("family", FAMILIES)
def test_a_run_with_NO_done_event_still_records(pg_session_factory, run_pass, family):
    # `done is None` was the guard that skipped the write entirely. The pass
    # itself SUCCEEDS here (see the fixture spec) — all three families reach
    # their success finalize and complete — so the row is an honest "we called
    # the model and it reported no counts", which is a different fact from "we
    # spent nothing" and a different fact again from no row at all.
    ws_id, job_id = run_pass(family, outcome="no_done")
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == family)).scalar_one()
        assert (row.tokens_in, row.tokens_out) == (0, 0)
        assert row.status == "ok"


def test_evaluator_records_when_it_produced_no_scorecard(pg_session_factory, run_pass):
    # The exact shape of the cited defect: pass_runner.py's `if job.scorecard is
    # None: _fail(...); return` (:177-180) sat ABOVE the token write (:183-187).
    ws_id, job_id = run_pass("evaluator", outcome="no_scorecard", tokens_in=99, tokens_out=5)
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == "evaluator")).scalar_one()
        assert row.tokens_in == 99, "tokens were spent producing nothing, and must be recorded"
        assert row.status == "failed"


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("outcome", ["ok", "failed"])
def test_the_display_columns_still_agree_with_the_ledger(
    pg_session_factory, run_pass, family, outcome
):
    # The per-job columns stay as a cache for the UI. A cache that disagrees with
    # the source of truth is worse than no cache — Task 9 gates this statically,
    # this checks it at runtime. It must LOAD the job row: the version this
    # replaced only re-read the ledger and could not go red on its own.
    #
    # The `failed` case is the one that bites. The only column writers in all
    # three families sit on the success path (coherence:171-172,
    # evaluator:184-185, uncited_claims:191-192), and those columns are NOT NULL
    # with server_default "0" (models.py:988-989, :1044-1045, :1096-1097), so a
    # _fail that records spend and writes no column reads 21 in the ledger and 0
    # in the cache. Step 5 adds the column write that makes this pass.
    ws_id, job_id = run_pass(family, outcome=outcome, tokens_in=8, tokens_out=9)
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == family)).scalar_one()
        job = s.get(JOB_MODEL[family], job_id)
        assert (job.input_tokens, job.output_tokens) == (row.tokens_in, row.tokens_out)


# --- review: one row per sub-call, not one per run -------------------------


def test_review_records_one_row_per_sub_call(pg_session_factory, run_review):
    # Up to SIX hosted calls per run, each with its own adapter and its own
    # terminal status. A single run-level row would put all six behind one
    # status and one ref_id.
    n_calls, item_ids, ws_id, run_id = run_review(outcome="ok", tokens_in=21, tokens_out=0)
    with pg_session_factory() as s:
        rows = s.execute(select(LlmSpend).where(LlmSpend.kind == "review")).scalars().all()
        assert len(rows) == n_calls
        assert {r.ref_id for r in rows} == {str(i) for i in item_ids}
        assert sum(r.tokens_in for r in rows) == 21 * n_calls
        assert {r.status for r in rows} == {"ok"}


def test_a_FAILED_review_sub_call_records(pg_session_factory, run_review):
    n_calls, item_ids, ws_id, run_id = run_review(outcome="failed", tokens_in=21, tokens_out=0)
    with pg_session_factory() as s:
        rows = s.execute(select(LlmSpend).where(LlmSpend.kind == "review")).scalars().all()
        assert len(rows) == n_calls
        assert {r.status for r in rows} == {"failed"}
        assert all(r.tokens_in == 21 for r in rows)


def test_a_TIMED_OUT_review_sub_call_records_timeout(pg_session_factory, run_review):
    n_calls, item_ids, ws_id, run_id = run_review(outcome="timeout", tokens_in=21, tokens_out=0)
    with pg_session_factory() as s:
        rows = s.execute(select(LlmSpend).where(LlmSpend.kind == "review")).scalars().all()
        assert len(rows) == n_calls
        assert {r.status for r in rows} == {"timeout"}


def test_the_review_item_columns_still_agree_with_the_ledger(pg_session_factory, run_review):
    n_calls, item_ids, ws_id, run_id = run_review(outcome="ok", tokens_in=8, tokens_out=9)
    with pg_session_factory() as s:
        rows = s.execute(select(LlmSpend).where(LlmSpend.kind == "review")).scalars().all()
        by_ref = {r.ref_id: r for r in rows}
        for item_id in item_ids:
            item = s.get(ReviewRunItem, item_id)
            row = by_ref[str(item_id)]
            assert (item.input_tokens, item.output_tokens) == (row.tokens_in, row.tokens_out)


def test_the_run_rollup_writes_no_ledger_row(pg_session_factory, run_review):
    # review/jobs.py:rollup_run_totals (:142-155) re-derives the run totals from
    # the items and has THREE call sites: pass_runner.py:174, pass_runner.py:364
    # and jobs.py:184. A reaped-then-late-finalized run reaches it twice, so an
    # append-only row written there would double-count against the very cap this
    # ledger exists to make correct.
    n_calls, item_ids, ws_id, run_id = run_review(outcome="ok", tokens_in=8, tokens_out=9)
    with pg_session_factory() as s:
        rows = s.execute(select(LlmSpend).where(LlmSpend.kind == "review")).scalars().all()
        # Count first. `all(...)` over an empty list is True, so without this the
        # assertion below would pass before any code is written — the exact
        # hollow-gate shape this plan keeps catching.
        assert len(rows) == n_calls
        assert all(r.ref_id != str(run_id) for r in rows), "no run-level review row"
```

- [ ] **Step 2: Build the `run_pass` and `run_review` fixtures**

In the same file. They must drive each family's real entry point —
`run_coherence_pass` (`coherence/pass_runner.py:31`), `run_evaluator_pass`
(`evaluator/pass_runner.py:38`), `run_uncited_claims_pass`
(`uncited_claims/pass_runner.py:74`), `run_review_pass`
(`review/pass_runner.py:205`) — with a stub adapter that yields the requested
outcome. The existing pass-runner tests under
`tests/proposal_assist/{coherence,evaluator,review,uncited_claims}/` show the
stub-adapter shape; reuse it.

`run_pass(family, *, outcome, tokens_in=0, tokens_out=0)` covers the three
single-call families and returns `(workspace_id, job_id)`. The five `outcome`
values it must produce, each pinned to a terminal path in the source so the
executor is not guessing:

| `outcome` | The stub does | Path reached | Recorded status |
|---|---|---|---|
| `"ok"` | a `TurnDoneEvent` and a valid result/scorecard/claims | the success finalize | `"ok"` |
| `"failed"` | a `TurnDoneEvent`, then raises a plain `RuntimeError` | `except Exception as exc` → `_fail` (coherence:128 / evaluator:154 / uncited_claims:167) | `"failed"` |
| `"timeout"` | a `TurnDoneEvent`, then raises `asyncio.TimeoutError` | the same `except`, timeout branch | `"timeout"` |
| `"no_done"` | **no** `TurnDoneEvent`, but a valid result/scorecard/claims | the success finalize, with `done is None` | `"ok"`, tokens `(0, 0)` |
| `"no_scorecard"` | a `TurnDoneEvent` and no scorecard (evaluator only) | `evaluator/pass_runner.py:177-180` | `"failed"` |

`"no_done"` must still produce a valid result. Without one, the three families
diverge — coherence completes anyway (`:167-169`, it has no no-result `_fail`),
while evaluator (`:177-180`) and uncited_claims (`:200-203`) fail — and a single
parametrized assertion could not hold for all three. Making the pass otherwise
succeed isolates the one variable the test is about: the `if done is not None`
guard.

`run_review(*, outcome, tokens_in, tokens_out)` drives `run_review_pass` with a
stub `adapter_builder` that gives EVERY sub-call the requested outcome, and
returns `(n_calls, item_ids, workspace_id, run_id)` where `n_calls` is the number
of sub-calls actually driven and `item_ids` are the `ReviewRunItem` ids for
exactly those calls. **`n_calls` is not a constant:** with `outcome="ok"` all six
`REVIEW_KINDS` run (five wave-1 + consolidation); with `"failed"` or `"timeout"`
every persona fails, so `persona_md` is empty and consolidation is SKIPPED at
`review/pass_runner.py:313-324` — five calls, not six. Return the measured count
rather than asserting a literal.

- [ ] **Step 3: Run and watch them fail**

First derive the count: `uv run pytest tests/proposal_assist/test_spend_pass_runners.py --collect-only -q | tail -1`
and use that number. Do not hand-arithmetic it — two of this plan's stated counts
were already wrong that way (Tasks 1 and 2).

Then: `uv run pytest tests/proposal_assist/test_spend_pass_runners.py -q`
Expected: every collected test fails. The `.scalar_one()` tests raise
`NoResultFound`; the review row-count tests fail on `assert 0 == n_calls`; and
`test_the_review_item_columns_still_agree_with_the_ledger` fails with a
`KeyError` on the empty `by_ref` map.

- [ ] **Step 3b: Hoist `done` so an exception cannot discard it**

**Do this before Steps 4-6; they depend on it.** In all four runners the
`TurnDoneEvent` is held in an inner local (`last`) and the outer `done` is
assigned only when the inner coroutine returns normally — `coherence:107-113`,
`evaluator:133-139`, `uncited_claims:148-156`, `review:125-131`. Any exception
raised after the done event arrives (an SDK error mid-stream, or `wait_for`'s
cancellation landing between the event and `return last`) leaves `done is None`
in the `except` block. Step 5's `_fail(..., done)` would then record
`tokens_in=None` → stored 0, and `test_a_FAILED_run_records` would fail 4/4 with
`0 != 21`. It also silently degrades the timeout path: a run that reported its
usage and was then cancelled by `wait_for` would record 0 against the cap — which
is violation #5 (`Tokens are written only if done is not None`) surviving the fix
meant to close it.

Bind the outer variable as each event arrives instead of on return. Shown for
coherence (`:98-113`); the other three are the same transformation:

```python
        done: TurnDoneEvent | None = None
        try:

            async def _run() -> None:                 # was: -> TurnDoneEvent | None
                nonlocal handle, done                 # was: nonlocal handle
                # launch is INSIDE the wait_for so a hung SDK connect is bounded too — not
                # just the streaming turn. wait_for cancels this coroutine on timeout,
                # raising CancelledError at whichever await is live (launch or the loop).
                handle = await adapter.launch(LaunchContext(workspace_id=workspace_id))
                async for event in adapter.send_turn(handle, instruction):
                    if isinstance(event, TurnDoneEvent):
                        done = event                  # outer; survives a later raise/cancel

            await asyncio.wait_for(_run(), timeout=settings.coherence_pass_timeout_seconds)
```

Four mechanical changes per file, and nothing else: add `done` to the `nonlocal`;
change `last = event` to `done = event`; delete the `last = None` line and the
`return last` line; change `done = await asyncio.wait_for(...)` to
`await asyncio.wait_for(...)` and the inner annotation to `-> None`. **Keep every
existing comment** in those blocks. The timeout settings are unchanged and differ
per file: `settings.coherence_pass_timeout_seconds` (`:113`),
`settings.evaluator_pass_timeout_seconds` (`:139`),
`settings.uncited_claims_pass_timeout_seconds` (`:154-156`, a two-line call), and
`settings.review_persona_timeout_seconds` (`:131`).

Two per-file details:

- In **review** the inner coroutine is `_drive` (`:122`), not `_run`.
- In **evaluator** (`:125-128`) and **uncited_claims** (`:143-146`) the inner
  coroutine has a pre-launch live-status recheck that does `return None` before
  the launch. Make it a bare `return` — the annotation is now `-> None` and the
  caller no longer reads a return value. Keep the recheck and its comment: it is
  what stops a deleted workspace's content being sent, and it is the reason
  `done` is legitimately `None` on that path.

The success blocks that read `done` (`coherence:170`, `evaluator:183`,
`uncited_claims:190`, `review:160`) are unchanged.

**Review is covered by this step even though it is exempt from Step 5's `_fail`
recipe**, and the consequence is intended: with `done` hoisted, review's
failed/cancelled items now stamp `item.input_tokens`/`output_tokens`/
`estimated_dollars` at `:161-165`, so `rollup_run_totals` and
`review_dollars_today` begin counting late and failed sub-calls that previously
vanished. That is the same class of undercount this plan exists to remove; note
it in the commit message.

- [ ] **Step 4: Record in each runner's success path — three families**

The call itself, in all three files (review is handled entirely in Step 5b — do
**not** add a run-level row for it here):

```python
            record_spend(
                s,
                workspace_id=job.workspace_id,
                kind="evaluator",   # coherence / uncited_claims
                status="ok",
                tokens_in=done.tokens_in if done else None,
                tokens_out=done.tokens_out if done else None,
                model=done.model if done else None,
                ref_id=job.id,
                sdk_session_id=done.session_id if done else None,
            )
```

It always goes **outside** the `if done is not None:` guard, at that `if`'s own
indentation — a run whose stream reported no counts must still leave a row.
**Placement is per-family and is not "after the token write" in all three**; the
three finalize blocks are not the same shape, and getting it wrong writes two
rows for one call:

| File | Put it | Why not elsewhere |
|---|---|---|
| `coherence/pass_runner.py` | after `job.sdk_session_id = ...` (`:174`), before `s.commit()` (`:175`) | this branch is the only terminal exit below the lock |
| `evaluator/pass_runner.py` | after `job.sdk_session_id = ...` (`:187`), before `s.commit()` (`:188`) | the `if job.scorecard is None: _fail(...)` branch at `:177-180` already returns, and Step 5 makes `_fail` record |
| `uncited_claims/pass_runner.py` | after `job.completed_at = ...` (`:206`), before `s.commit()` (`:207`) — **not** after the token write at `:194` | `:194` sits ABOVE both the `if job.status != "running"` return (`:195-199`) and the `if job.claims is None: _fail(...)` return (`:200-203`). A record at `:194` plus `_fail`'s record at `:201` is TWO rows for one call. |

`uncited_claims` needs one extra record, because its already-terminal branch is
the only one of the three that stamps token columns and then returns
(`:190-194`, then `:195-199`) — coherence (`:154-156`) and evaluator
(`:174-176`) take that return *above* their token writes. Add it inside that
branch, before the existing `s.commit()`:

```python
            if job.status != "running":
                # The reaper or a cancel finalized the job mid-pass. The sub-call
                # still ran and still reported usage — the comment above says to
                # keep it — so the ledger must carry it too, or Task 7's cap will
                # not see spend the display columns just recorded.
                record_spend(
                    s,
                    workspace_id=job.workspace_id,
                    kind="uncited_claims",
                    status="failed",
                    tokens_in=done.tokens_in if done else None,
                    tokens_out=done.tokens_out if done else None,
                    model=done.model if done else None,
                    ref_id=job.id,
                    sdk_session_id=done.session_id if done else None,
                )
                s.commit()
                return
```

**Known gap, not closed by this task:** coherence `:154-156` and evaluator
`:174-176` return when the job is no longer `running` *before* reaching their
token writes, so a pass that completed and reported usage against a reaped job
records nothing — in the columns today and in the ledger after this plan. That
is the same class as violation #3 and it is out of scope here; it is listed under
"Known limits, stated" so nobody reads Task 4 as having closed it.

- [ ] **Step 5: Record in each `_fail` helper — three families, not four**

**Review has no `_fail` helper and this recipe does not apply to it.** It is
handled in Step 5b.

Call-site counts, measured: `_fail` is called 2× in `coherence/pass_runner.py`
(`:80`, `:128`), 3× in `evaluator/pass_runner.py` (`:86`, `:154`, `:178`), 3× in
`uncited_claims/pass_runner.py` (`:113`, `:167`, `:201`) — each count excludes
the `def`. Update every one.

`_fail` currently receives neither the `done` event nor a status, and writes no
token columns (`coherence:177-181`, `evaluator:31-35`, `uncited_claims:36-40`).
Give it both, and make it keep the display cache true:

```python
def _fail(session, job, message: str, done=None, status: str = "failed") -> None:
    job.status = "failed"
    job.error = message
    job.completed_at = datetime.now(UTC)
    # Keep the display cache honest on the failure path too. The only column
    # writers in each family sit on the SUCCESS path, and these columns are NOT
    # NULL with server_default "0" — so without this the ledger says 21 and the
    # UI says 0 for every failed run, which is the drift Task 9 exists to
    # prevent.
    job.input_tokens = (done.tokens_in or 0) if done is not None else 0
    job.output_tokens = (done.tokens_out or 0) if done is not None else 0
    # Spend happened even though the run did not finish. Recording it is the
    # whole point: the old code left every failed run invisible to the cap.
    record_spend(
        session,
        workspace_id=job.workspace_id,
        kind="evaluator",   # coherence / uncited_claims
        status=status,
        tokens_in=done.tokens_in if done else None,
        tokens_out=done.tokens_out if done else None,
        model=done.model if done else None,
        ref_id=job.id,
        sdk_session_id=done.session_id if done else None,
    )
    session.flush()
```

Keep each file's existing `job:` annotation — `CoherenceJob`, `EvaluatorJob`,
`UncitedClaimsJob` — rather than pasting the untyped `job` above; only the two
new parameters and the two new bodies' lines are added. The existing
`session.flush()` stays last, so the new assignments and the new row land in the
same flush as the status change.

Then update every `_fail(...)` call site in that file:

- **The one site inside the timeout-aware handler** — `coherence:128`,
  `evaluator:154`, `uncited_claims:167` — passes both `done` and the status the
  source already computed but threw away. `exc` is bound by the enclosing
  `except Exception as exc:` (`coherence:114`, `evaluator:140`,
  `uncited_claims:157`), so it is in scope at the call:

  ```python
                      _fail(
                          s,
                          job,
                          message,
                          done=done,
                          status="timeout"
                          if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                          else "failed",
                      )
  ```

  Without this, `status` is CHECK-constrained to `ok|failed|timeout` and no line
  in the whole plan can ever produce the third value, while the distinction sits
  7-10 lines above the call site (`coherence:118-121`, `evaluator:144-148`,
  `uncited_claims:159-163`).

- **The budget-reached sites** — `coherence:80`, `evaluator:86`,
  `uncited_claims:113` — pass neither: no hosted call was made, so `done` is
  legitimately absent and the status is `"failed"`. Leave them as
  `_fail(s, job, "...")`.

- **The no-result sites** — `evaluator:178` (`"evaluator produced no scorecard"`)
  and `uncited_claims:201` (`"uncited-claims pass produced no result"`) — pass
  `done=done` and leave the status at its `"failed"` default. **Do not skip
  `evaluator:178`; that one is the cited defect** (violation #3: the `return`
  above the token write).

After Step 3b, `done` in the `except` block holds the event whenever one arrived.
A `_fail` that still receives `None` means no `TurnDoneEvent` was ever emitted —
a real case, recorded as `(0, 0)`, not a bug in the plumbing.

- [ ] **Step 5b: Review — one row per `ReviewRunItem`**

`review/pass_runner.py` has no `_fail`. Each sub-call finalizes its own item in
the `with session_factory() as s:` block at `:149-191`, and `review/jobs.py`
sums the items into the run afterwards. **Record one row per `ReviewRunItem`,
inside that block, alongside the existing `item.input_tokens` write at `:161`.**
This is a mandate, not a choice: see the Interfaces note above for why a
run-level row cannot express a six-call fan-out.

Add it immediately before `s.commit()` at `:191`, after the status branches at
`:166-190` have run, so `item.status` is settled:

```python
            # One row per sub-call. `status` reads the item's settled terminal
            # state, plus the timeout the except block at :132-138 already
            # distinguished and stored in `error`.
            record_spend(
                s,
                workspace_id=workspace_id,
                kind="review",
                status=(
                    "timeout"
                    if error == "timed out"
                    else "ok"
                    if item.status == "done"
                    else "failed"
                ),
                tokens_in=done.tokens_in if done else None,
                tokens_out=done.tokens_out if done else None,
                model=done.model if done else None,
                ref_id=item.id,
            )
            s.commit()
```

The status map in full: `done` → `"ok"`; `failed` and `cancelled` → `"failed"`;
`error == "timed out"` (set at `:134-138`) → `"timeout"`, which wins over the
item status because it describes the call. Note the consequence and accept it: a
sub-call that finished cleanly but whose RUN was finalized elsewhere takes the
`:171` `"cancelled"` branch and is recorded `"failed"` even though the call
succeeded. `sum_spend_today` has no status predicate, so this costs observability
and not one token of cap accuracy.

**Two places that must NOT get a row**, both because no hosted call was made:

- `review/pass_runner.py:96-100`, the pre-launch recheck that sets
  `item.status = "cancelled"` before any adapter is built.
- `review/pass_runner.py:313-324`, which sets the consolidation item to
  `"skipped"` when every persona failed. (`"skipped"` therefore never reaches the
  map above.)

**`review/jobs.py:rollup_run_totals` must NOT write to the ledger.** It is pure
re-derivation from the items (`:142-155`) and it has THREE call sites —
`pass_runner.py:174`, `pass_runner.py:364`, `jobs.py:184`. A reaped run
(`jobs.py:184`) followed by a late sub-call finalize (`pass_runner.py:174`)
reaches it twice, and in an append-only table that is two run-total rows for one
run: double-counting against the cap this plan exists to fix.
`test_the_run_rollup_writes_no_ledger_row` is the assertion. Task 9 pins the
function instead.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_spend_pass_runners.py -q`
Expected: the count collected in Step 3, all passing, `0 failed`.

- [ ] **Step 7: Run each family's own suite**

Run: `uv run pytest tests/proposal_assist/coherence tests/proposal_assist/evaluator tests/proposal_assist/review tests/proposal_assist/uncited_claims -q`
Expected: no new failures. Record before/after counts in the commit.

- [ ] **Step 8: Prove the failure paths bite**

Three mutations, each reverted before the next.

1. Remove the `record_spend` call from the **evaluator's `_fail` only**, re-run.
   Expected: `test_a_FAILED_run_records[evaluator]`,
   `test_a_TIMED_OUT_run_records_timeout[evaluator]`,
   `test_evaluator_records_when_it_produced_no_scorecard` and
   `test_the_display_columns_still_agree_with_the_ledger[failed-evaluator]` go
   red; every other family stays green. Note that `no_done` is NOT in this list
   any more — after Step 3b's fixture spec, `no_done` reaches the SUCCESS
   finalize, so it is Step 4's record that covers it, not `_fail`'s. Revert.
2. Replace the timeout-aware `status=` argument at `evaluator:154` with a bare
   `status="failed"`, re-run. Expected:
   `test_a_TIMED_OUT_run_records_timeout[evaluator]` goes red **on its own**.
   This is the mutation the old `assert row.status in ("failed", "timeout")`
   could not fail. Revert.
3. Undo Step 3b in `evaluator/pass_runner.py` only (restore `last` and
   `done = await asyncio.wait_for(...)`), re-run. Expected:
   `test_a_FAILED_run_records[evaluator]` goes red with `0 != 21` — the event
   arrived and the exception discarded it. Revert.

- [ ] **Step 9: Commit**

```bash
uv run ruff check .
git add src/call_manager/proposal_assist/coherence/pass_runner.py \
        src/call_manager/proposal_assist/evaluator/pass_runner.py \
        src/call_manager/proposal_assist/uncited_claims/pass_runner.py \
        src/call_manager/proposal_assist/review/pass_runner.py \
        tests/proposal_assist/test_spend_pass_runners.py
git commit -m "feat(spend): the four pass runners record failures, not just successes

- done is hoisted out of the inner coroutine in all four runners, so an
  exception after the done event no longer discards the usage it reported
- review records one row per ReviewRunItem (up to six per run);
  review/jobs.py:rollup_run_totals deliberately records nothing
- _fail now writes the display columns too, so failed runs count toward the
  per-family daily caps that sum them"
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

import pytest
from sqlalchemy import select

from call_manager.db.models import LlmSpend
from call_manager.proposal_assist.turns.caps import GlobalTokenCapExceeded


def test_compaction_records_its_real_usage(pg_session_factory, run_compaction):
    ws_id = run_compaction(tokens_in=500, tokens_out=120)
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == "compaction")).scalar_one()
        # The REPORTED usage, not a character estimate of the summary text.
        assert (row.tokens_in, row.tokens_out) == (500, 120)
        assert row.ref_id is None  # compaction has no job row


def test_compaction_refuses_to_run_over_the_cap(pg_session_factory, run_compaction, at_cap):
    # It used to spend past a ceiling that had already stopped every chat turn.
    #
    # This asserts a RAISE, not a None. `compact_workspace` already returns None
    # for "there were no turns to summarize" (compaction.py:144-145), so
    # `assert result is None` would be satisfied by a compaction that never even
    # looked at the cap — and `ChatSummary` has no `skipped_reason` attribute
    # (models.py:1100-1133), so an `or result.skipped_reason == ...` second
    # disjunct is unreachable. The cap skip needs an outcome of its own.
    ws_id = at_cap()
    with pytest.raises(GlobalTokenCapExceeded):
        run_compaction(tokens_in=500, tokens_out=120, workspace_id=ws_id)
    with pg_session_factory() as s:
        assert s.execute(select(LlmSpend).where(LlmSpend.kind == "compaction")).first() is None


def test_the_tick_stops_at_the_cap_without_raising(pg_session_factory, run_compaction_tick, at_cap):
    # run_compaction_tick's generic `except Exception` (compaction.py:227) would
    # otherwise turn every capped workspace into a logged stack trace. The cap is
    # org-wide, so once it trips there is nothing left to try.
    at_cap()
    assert run_compaction_tick() == 0
    with pg_session_factory() as s:
        assert s.execute(select(LlmSpend).where(LlmSpend.kind == "compaction")).first() is None


def test_a_failed_compaction_still_records(pg_session_factory, run_compaction):
    # The summarizer raised, so there is no envelope and no usage to report. The
    # row's EXISTENCE is the fact being recorded: "we called it and it threw" is
    # a different fact from "we never called it", and the old code could express
    # neither. Do not invent a token count here — inventing one is the
    # character-estimate mistake this task removes.
    with pytest.raises(RuntimeError):
        run_compaction(outcome="failed")
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == "compaction")).scalar_one()
        assert row.status == "failed"
        assert (row.tokens_in, row.tokens_out) == (0, 0)
```

- [ ] **Step 2: Build the `run_compaction`, `run_compaction_tick` and `at_cap` fixtures**

`run_compaction(*, tokens_in=0, tokens_out=0, workspace_id=None, outcome="ok")`
drives the real `compact_workspace` (`turns/compaction.py:120`) with a stub
summarizer client. It must seed enough un-summarized `ChatTurn` rows that
`compact_workspace` gets past its `if not turns: return None` guard at `:144-145`
— otherwise every assertion below is vacuous. With `outcome="ok"` the stub's
`chat()` returns an envelope carrying the requested usage; with
`outcome="failed"` it raises `RuntimeError`, which the recipe in Step 4 records
and then re-raises.

`run_compaction_tick()` drives `run_compaction_tick` (`turns/compaction.py:194`)
over a workspace that needs compaction, and returns its int result.

`at_cap()` seeds enough `LlmSpend` rows to exceed
`settings.global_daily_token_cap_input` and returns the workspace id.

- [ ] **Step 3: Run and watch it fail**

First derive the count:
`uv run pytest tests/proposal_assist/test_spend_compaction.py --collect-only -q | tail -1`.

Then: `uv run pytest tests/proposal_assist/test_spend_compaction.py -q`
Expected: every collected test fails — `NoResultFound` for the two recording
tests, `DID NOT RAISE` for the cap test, and a stack trace out of the tick for
the tick test.

- [ ] **Step 4: Add the cap check and the record**

Add both imports to `turns/compaction.py` — neither is there today, and
`uv run ruff check .` (Step 6) fails on the missing one with `F821`:

```python
from call_manager.proposal_assist.spend import record_spend
from call_manager.proposal_assist.turns.caps import (
    GlobalTokenCapExceeded,
    check_global_daily_token_cap,
)
```

**`compact_workspace` takes a `session_factory`, not a `session`** (`:120-127`).
The name `session` does not exist anywhere in its scope. Every block below opens
its own.

The cap check goes AFTER the `if not turns: return None` guard at `:144-145` and
BEFORE the summarizer call at `:152`, so a no-op compaction does not burn a cap
read:

```python
    # Compaction is a hosted-LLM call like any other. It used to spend past a
    # ceiling that had already stopped every chat turn, because it was the one
    # spender that never asked. The read session opened at :137 is already closed
    # by here, so open our own.
    with session_factory() as cap_s:
        cap = check_global_daily_token_cap(cap_s, settings=settings)
    if not cap.allowed:
        logger.warning(
            "compaction.skipped_global_cap",
            extra={"event": "compaction_skipped_global_cap", "reason": cap.reason},
        )
        raise GlobalTokenCapExceeded(
            used_in=cap.used_in,
            used_out=cap.used_out,
            cap_in=cap.cap_in,
            cap_out=cap.cap_out,
            reason=cap.reason or "cap_exceeded",
        )
```

**It raises rather than returning `None` on purpose.** `compact_workspace` is
annotated `-> ChatSummary | None` and already returns `None` for "there were no
turns to summarize" (`:144-145`); a second `None` would make those two outcomes
indistinguishable to every caller and to the test. `GlobalTokenCapExceeded` is
already defined at `turns/caps.py:65` and carries the numbers.

Then catch it in `run_compaction_tick` (`:194`). The new arm must go **above**
the existing `except Exception` at `:227`, or every capped workspace logs a stack
trace once per tick. Replace `:219-231` with:

```python
        try:
            compact_workspace(
                session_factory=session_factory,
                workspace_id=wsid,
                summarizer_client=client,
                settings=settings,
            )
            compacted += 1
        except GlobalTokenCapExceeded:
            logger.warning(
                "compaction_tick: global daily token cap reached; stopping this tick",
                extra={"event": "compaction_tick_global_cap"},
            )
            break
        except Exception:
            logger.exception(
                "compaction_tick: compact_workspace failed for %s — continuing",
                wsid,
            )
```

`break`, not `continue`: the cap is org-wide, so once it trips every remaining
workspace in the loop would trip it too. `compacted` is not incremented, so the
tick returns the number actually compacted before the ceiling — which is what
`test_the_tick_stops_at_the_cap_without_raising` asserts.

After the summarizer returns, in the same transaction as the `ChatSummary` write
(the `with session_factory() as s:` at `:158`), before `s.commit()` at `:189`:

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
`envelope` is what the existing code already reads `model` from at `:156`. If
usage is not in the envelope, record `status="ok"` with None tokens rather than
estimating; a row saying "we called it and got no counts" is honest, and a
character estimate is not.

Wrap the summarizer call at `:152-154` so a failure records too:

```python
    try:
        envelope = summarizer_client.chat(
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception:
        with session_factory() as fail_s:
            record_spend(
                fail_s, workspace_id=workspace_id, kind="compaction", status="failed"
            )
            fail_s.commit()
        raise
```

**Do NOT write `record_spend(s, ...)` in that handler.** `s` is still bound at
this point — Python's `with` does not scope its target — but it is the session
closed back at `:142`, and SQLAlchemy 2.0 happily writes through a closed
`Session` (measured). The mistake would therefore PASS the test instead of
failing it, and read as a bug to every future reader.

No token counts are passed on the failure path because there are none: the
summarizer raised, so there is no envelope. `record_spend` stores 0.

**Compaction has no timeout branch.** `grep -i timeout turns/compaction.py`
returns zero hits and the `summarizer_client.chat(...)` call at `:152` has no
`wait_for` and no handler of its own, so `status="timeout"` is not reachable here
and must not be invented. This is listed under "Known limits, stated".

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/proposal_assist/test_spend_compaction.py -q`
Expected: the count collected in Step 3, all passing, `0 failed`.

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
    #
    # `!= 429` is not that assertion. It is satisfied by 403 (no disclosure ack),
    # 409 (workspace still in setup), 503 (drafting disabled) and 404/403 from
    # require_member — every refusal shape except the one being tested. Assert
    # the success contract instead. None of the four POST decorators sets
    # `status_code` (web/coherence.py:44, web/evaluator.py:47, web/review.py:135,
    # web/uncited_claims.py:80), so the success status is 200, and all four
    # responses carry `id` and `status`.
    ws_id = under_global_cap(_app, current_user, pg_session_factory, tmp_path)
    r = client_authed_owner.post(path.format(ws=ws_id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"]
    assert body["status"] in ("queued", "running")
```

- [ ] **Step 2: Build the `at_global_cap` / `under_global_cap` fixtures**

Both seed a workspace with a disclosure ack (the cloud gate runs first and would
otherwise return 403, masking the result). `at_global_cap` then inserts
`LlmSpend` rows exceeding `settings.global_daily_token_cap_input`.

Build them on `tests/proposal_assist/conftest.py:230`'s `make_workspace`, whose
`current_phase` already defaults to `"active"` and whose `ack_disclosure`
defaults to `False` — so pass `ack_disclosure=True` and leave `current_phase`
alone. If you build the `Workspace` row directly instead, you must set
`current_phase="active"` yourself: the column's default is `"setup"`
(`db/models.py:705`, CHECK at `:626-627`) and `gate_cloud_llm` raises 409 on it
(`web/_cloud_gate.py:72-76`) — which the old `!= 429` assertion would have
swallowed, leaving both halves of this file green over a workspace that never
reached the cap check at all.

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
- Produces: a failing test when a **function** stamps a token field without
  recording spend in its own body, and a second failing test when anything but
  `spend.py` constructs an `LlmSpend` row.

This is what stops the next feature from repeating the six violations. Model it on
`tests/fencing/sink_guard.py`, which already does exactly this shape for git
sinks — read that file first, including how `PROVEN_NON_COMMIT` pins are derived
(`sink_guard.py:325-340`), how `_collect_functions` (`:292`) walks qualnames, and
why `test_no_pin_is_stale` (`:14-17`) makes the list shrink-only.

- [ ] **Step 1: Write the gate**

Create `tests/fencing/spend_guard.py`. **It is FUNCTION-level, not module-level.**
A per-file rule cannot work here: after Task 4, `evaluator/pass_runner.py` has a
`record_spend` on the success path AND one in `_fail`, and after Task 3
`turns/outcomes.py` has two independent terminal writers. Under a per-file rule
one call anywhere in the file — including one in a dead branch or in an unrelated
helper — exempts every token assignment in it, permanently. Measured: with the
module-level version, a probe that stamps in `_fail` and records only in
`success` passes the gate silently, and so does a `record_spend` sitting beside a
stamping function it never calls.

The module below was executed against the real `src/` tree before being written
down; the results it produces are stated in Step 3.

```python
"""Any FUNCTION that stamps a token field must also record spend.

The defect this prevents: `job.input_tokens = done.tokens_in or 0` written on the
happy path and nowhere else. Six sites had it, all losing spend in the same
direction. The per-job columns are a display cache now, so writing one without a
matching `record_spend` call re-opens exactly the gap the ledger closed.

FUNCTION-level, modelled on `tests/fencing/sink_guard.py`'s call-level pins: a
function either records spend in its OWN body, or its `(path, qualname)` is
pinned here with a reason. The pinned list can only shrink.

Module scope would not work. `turns/outcomes.py` has two independent terminal
writers and `evaluator/pass_runner.py` has a success path and a `_fail`; under a
per-file rule one `record_spend` anywhere in the file — in a dead branch, in an
unrelated helper, on the other terminal path — exempts every token assignment in
it, permanently.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

# Attribute names that mean "I am writing token telemetry to a row".
TOKEN_FIELDS = frozenset({"input_tokens", "output_tokens", "tokens_in", "tokens_out"})

# Names that mean "I am recording spend", before import aliasing.
SPEND_WRITERS = frozenset({"record_spend", "record_usage"})

# Functions that stamp a token field and legitimately do NOT record spend.
# Keyed by (path relative to src/, dotted qualname). A reason is MANDATORY.
# Adding to this list is a reviewed diff.
#
# Keyed by FUNCTION, not by module, on purpose: a module-level pin would bless
# every sibling function in the same file forever.
PINNED: dict[tuple[str, str], str] = {
    (
        "call_manager/proposal_assist/review/jobs.py",
        "rollup_run_totals",
    ): (
        "pure re-derivation of run totals from items that each already recorded "
        "their own ledger row; it has three call sites (pass_runner.py:174, "
        "pass_runner.py:364, jobs.py:184) and a reaped-then-late-finalized run "
        "reaches it twice, so an append-only row here would double-count"
    ),
}


def _collect_functions(
    tree: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    prefix: str,
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Return all function defs under tree with their dotted qualname.

    Copied from tests/fencing/sink_guard.py:292 — same traversal, same qualnames.
    """
    results: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for stmt in getattr(tree, "body", []):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qname = f"{prefix}.{stmt.name}" if prefix else stmt.name
            results.append((qname, stmt))
            results.extend(_collect_functions(stmt, qname))
        elif isinstance(stmt, ast.ClassDef):
            qname = f"{prefix}.{stmt.name}" if prefix else stmt.name
            results.extend(_collect_functions(stmt, qname))
        else:
            for child in ast.walk(stmt):
                if child is stmt:
                    continue
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qname = f"{prefix}.{child.name}" if prefix else child.name
                    results.append((qname, child))
                    results.extend(_collect_functions(child, qname))
                elif isinstance(child, ast.ClassDef):
                    qname = f"{prefix}.{child.name}" if prefix else child.name
                    results.extend(_collect_functions(child, qname))
    return results


def _own_nodes(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Nodes belonging to `func` itself, excluding nested def/class BODIES.

    Nested functions are collected separately by _collect_functions and judged in
    their own right, so a nested closure that records must not bless its parent.
    """
    out: list[ast.AST] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        out.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return out


def _spend_writer_names(tree: ast.AST) -> frozenset[str]:
    """Local names that refer to a spend writer, honouring `as` aliases.

    Without this, `from ...spend import record_spend as rs` is a false POSITIVE
    on correct code — reproduced before this was added.
    """
    names = set(SPEND_WRITERS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in SPEND_WRITERS and alias.asname:
                    names.add(alias.asname)
    return frozenset(names)


def _stamp_lines(nodes: list[ast.AST]) -> list[int]:
    lines: list[int] = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr in TOKEN_FIELDS:
                    lines.append(node.lineno)
    return sorted(lines)


def _records_spend(nodes: list[ast.AST], writer_names: frozenset[str]) -> bool:
    for node in nodes:
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in writer_names:
                return True
    return False


def offenders() -> list[str]:
    """`path::qualname` for every function that stamps, records nothing, is unpinned."""
    out: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC))
        tree = ast.parse(path.read_text())
        writer_names = _spend_writer_names(tree)
        for qname, func in _collect_functions(tree, ""):
            if (rel, qname) in PINNED:
                continue
            nodes = _own_nodes(func)
            if _stamp_lines(nodes) and not _records_spend(nodes, writer_names):
                out.append(f"{rel}::{qname}")
    return sorted(out)


def stale_pins() -> list[str]:
    """Pins whose function no longer exists, or no longer stamps a token field."""
    stale: list[str] = []
    for rel, qname in PINNED:
        path = SRC / rel
        if not path.exists():
            stale.append(f"{rel}::{qname}")
            continue
        tree = ast.parse(path.read_text())
        funcs = dict(_collect_functions(tree, ""))
        func = funcs.get(qname)
        if func is None or not _stamp_lines(_own_nodes(func)):
            stale.append(f"{rel}::{qname}")
    return sorted(stale)


def llm_spend_constructors() -> list[str]:
    """Modules under src/ that CONSTRUCT an `LlmSpend` row.

    The plan's central claim is "exactly one writer". Nothing checked it: the
    stamps/records rule above would be satisfied by a second module that both
    constructed a row and called record_spend. This is the claim, asserted.
    """
    out: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "LlmSpend":
                    out.append(str(path.relative_to(SRC)))
                    break
    return sorted(out)
```

Three things to know about it, each measured rather than assumed:

- **`db/models.py` and `spend.py` need no pin, and must not get one.**
  `_stamp_lines` matches only `ast.Assign` to an `ast.Attribute`. `db/models.py`
  declares all 14 of its token columns as `AnnAssign` to a `Name`
  (`tokens_in: Mapped[int] = mapped_column(...)`, at lines 866-867, 988-989,
  1044-1045, 1096-1097, 1584-1585, 1642-1643, 1809-1810 — zero
  `Assign`-to-`Attribute`), and Task 1's `LlmSpend` adds two more of the same
  shape. `spend.py` passes `tokens_in=`/`tokens_out=` as `LlmSpend(...)`
  constructor keywords (`ast.keyword`). Neither trips the predicate. **Do not
  "fix" a failure here by widening the predicate to `AnnAssign`** — that makes
  every ORM module an offender and forces a pin for `models.py` that it does not
  otherwise need.
- **Exactly one pin, and it is a function, not a file.**
  `review/jobs.py::rollup_run_totals` re-derives run totals from items that each
  already recorded (Task 4 Step 5b). A module-keyed pin would also bless
  `reap_stuck_review_runs` in the same file forever.
- **`_spend_writer_names` is load-bearing.** Without alias resolution,
  `from ...spend import record_spend as rs` is a false POSITIVE on correct code —
  reproduced. `sink_guard.py:89` does the same job for subprocess imports; its
  `_resolve_aliases` is subprocess-specific and cannot be reused as-is.

- [ ] **Step 2: Write the test**

Create `tests/fencing/test_spend_guard.py`:

```python
import textwrap

from tests.fencing.spend_guard import (
    PINNED,
    llm_spend_constructors,
    offenders,
    stale_pins,
)


def _tree(tmp_path, monkeypatch, name, source):
    import tests.fencing.spend_guard as g

    fake = tmp_path / "call_manager" / name
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text(textwrap.dedent(source))
    monkeypatch.setattr(g, "SRC", tmp_path)
    monkeypatch.setattr(g, "PINNED", {})
    return g


def test_no_function_stamps_tokens_without_recording_spend():
    assert offenders() == []


def test_no_pin_is_stale():
    # Same discipline as sink_guard: the list can only shrink.
    assert stale_pins() == []


def test_every_pin_carries_a_reason():
    assert all(reason.strip() for reason in PINNED.values())


def test_stale_pin_detection_can_actually_fail(monkeypatch):
    # PINNED has one entry, so the two tests above are nearly vacuous. This is
    # the one that proves stale_pins() works, by pinning a function that stamps
    # nothing.
    import tests.fencing.spend_guard as g

    monkeypatch.setattr(
        g, "PINNED", {("call_manager/proposal_assist/spend.py", "record_spend"): "x"}
    )
    assert g.stale_pins() == ["call_manager/proposal_assist/spend.py::record_spend"]


def test_the_guard_can_actually_fail(tmp_path, monkeypatch):
    # A gate nobody has seen fail is not a gate. Point it at a function that
    # commits the exact defect and confirm it is reported.
    g = _tree(
        tmp_path,
        monkeypatch,
        "bad.py",
        """
        def f(job, done):
            job.input_tokens = done.tokens_in or 0
        """,
    )
    assert g.offenders() == ["call_manager/bad.py::f"]


def test_the_guard_accepts_a_function_that_records(tmp_path, monkeypatch):
    # And the other direction, so it is not simply reporting everything.
    g = _tree(
        tmp_path,
        monkeypatch,
        "good.py",
        """
        def f(s, job, done):
            job.input_tokens = done.tokens_in or 0
            record_spend(s, workspace_id=job.workspace_id, kind='evaluator', status='ok')
        """,
    )
    assert g.offenders() == []


def test_a_sibling_that_records_does_not_bless_this_function(tmp_path, monkeypatch):
    # THE bypass a module-level guard has. This is the real shape of
    # evaluator/pass_runner.py after Task 4: a success path that records and a
    # _fail that must record too.
    g = _tree(
        tmp_path,
        monkeypatch,
        "two.py",
        """
        def _fail(session, job, done):
            job.input_tokens = done.tokens_in or 0

        def success(session, job, done):
            job.input_tokens = done.tokens_in or 0
            record_spend(session, workspace_id=job.workspace_id, kind='evaluator', status='ok')
        """,
    )
    assert g.offenders() == ["call_manager/two.py::_fail"]


def test_an_unrelated_record_usage_does_not_bless_a_stamping_function(tmp_path, monkeypatch):
    # record_usage is the CITATION ledger's writer (citations/usage.py), a
    # different ledger from the one this plan creates. Name-matching it must not
    # excuse a function that stamps and never records.
    g = _tree(
        tmp_path,
        monkeypatch,
        "unrelated.py",
        """
        def stamps(job, done):
            job.output_tokens = done.tokens_out or 0

        def telemetry(session):
            record_usage(session)
        """,
    )
    assert g.offenders() == ["call_manager/unrelated.py::stamps"]


def test_an_aliased_import_writer_is_not_reported(tmp_path, monkeypatch):
    # Correct code, aliased. Without _spend_writer_names this is a false
    # positive — reproduced before the helper was added.
    g = _tree(
        tmp_path,
        monkeypatch,
        "aliased.py",
        """
        from call_manager.proposal_assist.spend import record_spend as rs

        def f(session, job, done):
            job.input_tokens = done.tokens_in or 0
            rs(session, workspace_id=job.workspace_id, kind='evaluator', status='ok')
        """,
    )
    assert g.offenders() == []


def test_a_dead_record_spend_still_satisfies_the_guard_KNOWN_LIMIT(tmp_path, monkeypatch):
    # Measured, not aspirational: an AST scan cannot do reachability, so a
    # record_spend under `if False:` passes. What catches that is the RUNTIME
    # tests in Tasks 3-6, which drive the real path and assert a row exists.
    # Written down so nobody mistakes this gate for more than it is. If someone
    # later teaches the guard reachability, this test goes red — update it then.
    g = _tree(
        tmp_path,
        monkeypatch,
        "dead.py",
        """
        def never_records(session, job, done):
            job.input_tokens = done.tokens_in or 0
            if False:
                record_spend(session)
        """,
    )
    assert g.offenders() == []


def test_the_ledger_has_exactly_one_writer():
    # The plan's central claim, asserted for the first time. The stamps/records
    # rule above would happily accept a second module that both constructed an
    # LlmSpend row and called record_spend.
    assert llm_spend_constructors() == ["call_manager/proposal_assist/spend.py"]
```

- [ ] **Step 3: Run it**

First derive the count:
`uv run pytest tests/fencing/test_spend_guard.py --collect-only -q | tail -1`.

Then: `uv run pytest tests/fencing/test_spend_guard.py -q`
Expected: the collected count, all passing, `0 failed`.

If `offenders()` is non-empty, a real site was missed in Tasks 3-5 — **fix the
site, do not add a pin.** A pin is for a function that legitimately stamps
without spending; there is exactly one.

For reference, running this exact guard against the tree **before** Tasks 3-5
reports these seven functions, each of which a task above converts:

```
call_manager/proposal_assist/coherence/pass_runner.py::run_coherence_pass          (T4 Step 4)
call_manager/proposal_assist/evaluator/pass_runner.py::run_evaluator_pass          (T4 Step 4)
call_manager/proposal_assist/review/pass_runner.py::_run_one_call                  (T4 Step 5b)
call_manager/proposal_assist/turns/outcomes.py::record_terminal_outcome            (T3 Step 4b)
call_manager/proposal_assist/turns/outcomes.py::stamp_post_terminal_usage          (T3 Step 4c)
call_manager/proposal_assist/turns/processor.py::_finalize_success                 (T3 Step 4a)
call_manager/proposal_assist/uncited_claims/pass_runner.py::run_uncited_claims_pass (T4 Step 4)
```

`review/jobs.py::rollup_run_totals` is the eighth stamping function and is the
one `PINNED` entry. Because the guard is per-function, `record_terminal_outcome`
and `stamp_post_terminal_usage` each record in their own body after Task 3, so
**neither needs a pin** — which is exactly what a module-level guard could not
have given you: one writer added to either would have blessed the other forever.

- [ ] **Step 4: Prove it bites on the real tree**

Remove the `record_spend` call from **`run_evaluator_pass`'s success finalize
block only**, leaving `_fail`'s in place, and run
`uv run pytest tests/fencing/test_spend_guard.py -q`.
Expected: `test_no_function_stamps_tokens_without_recording_spend` FAILS, naming
`call_manager/proposal_assist/evaluator/pass_runner.py::run_evaluator_pass`.
Revert.

The qualification matters. "Remove the `record_spend` call from
`evaluator/pass_runner.py`" is ambiguous after Task 4 — that file has two — and
under a module-level guard removing only one of them leaves the gate green, so
the step would have proved nothing.

- [ ] **Step 5: Full suite and lint**

```bash
uv run ruff check .
uv run pytest -n 16 --dist loadfile -q
```

Expected: `1 skipped`. Record the pass total.

- [ ] **Step 6: Commit**

```bash
git add tests/fencing/spend_guard.py tests/fencing/test_spend_guard.py
git commit -m "test(fencing): a function that stamps tokens must record spend"
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
| 6b | A **reaped or cancelled** chat turn records one row, from the reaper path | T3 (`test_a_reaped_turn_records_its_consumed_tokens`) |
| 6c | A turn reaped **between** its done event and its finalize records exactly one row | T3 (`test_a_turn_reaped_before_finalize_still_records_exactly_one_row`) |
| 8 | coherence / evaluator / uncited_claims record on success; review records per sub-call | T4 |
| 9 | Each records on failure | T4 |
| 10 | Each records when there is no `TurnDoneEvent` | T4 |
| 10b | Each records `status="timeout"` on a timeout, by equality not disjunction | T4 (`test_a_TIMED_OUT_run_records_timeout`) |
| 11 | Evaluator records when it produced no scorecard | T4 |
| 11b | A review run writes one row per `ReviewRunItem` and none for the run | T4 (`test_review_records_one_row_per_sub_call`, `test_the_run_rollup_writes_no_ledger_row`) |
| 12 | Compaction records its reported usage | T5 |
| 13 | Compaction refuses to run over the cap, with an outcome distinguishable from "no turns" | T5 |
| 14 | Citation calls write both ledgers | T6 |
| 15 | Citation spend counts toward the global total | T6 |
| 16 | The cap sums the ledger, not the five job tables | T7 |
| 17 | Stale job columns do not move the cap | T7 |
| 18 | A failed run counts toward the cap | T7 |
| 19 | Each fan-out endpoint returns 429 at the cap | T8 |
| 20 | Each still **works** under the cap — 200 with a job id and a live status, not merely "not 429" | T8 |
| 21 | No **function** stamps tokens without recording spend in its own body | T9 |
| 22 | The guard can be shown to fail, including on the sibling-function bypass a per-module rule would miss | T9 |
| 22b | `spend.py` is the only module that constructs an `LlmSpend` row | T9 (`test_the_ledger_has_exactly_one_writer`) |
| **23** | **Two ledger rows are never written for one call (idempotency under retry)** | **ASSERTED FOR THE DOUBLE-FINALIZE, NOT BY CONSTRAINT.** There is no unique call-identity constraint, and there must not be one keyed on `ref_id`: Task 6 stores an op NAME there (`assert spend.ref_id == "metadata"`), so a uniqueness constraint over `(workspace_id, kind, ref_id)` would reject truthful rows and let real spend past the cap. What holds instead is placement, tested: every `record_spend` sits BELOW its path's existing exactly-once guard — `_finalize_success`'s conditional UPDATE (`processor.py:616-630`), `record_terminal_outcome`'s `if row is None: return False` (`outcomes.py:78-79`), `stamp_post_terminal_usage`'s `FOR UPDATE` + already-reconciled checks (`outcomes.py:184-195`). T3's `test_one_turn_writes_exactly_one_row`, `test_an_already_terminal_turn_records_nothing_twice` and `test_a_turn_reaped_before_finalize_still_records_exactly_one_row` are the assertions. **Re-claim is not a risk for six of the seven kinds, measured:** every crash-recovery reaper terminalizes rather than requeues — `coherence/jobs.py:195`, `evaluator/jobs.py:143`, `uncited_claims/jobs.py:145`, `review/jobs.py:187`+`:193`, `turns/reaper.py:77`, `drafting/orphan_sweep.py:37` all set `failed`; compaction has no job row. The one `running→queued` requeue among the ledger kinds is `analysis/queue.py:392` (`kind="citation"`), and a retried attempt there makes NEW calls — cache hits never reach `complete_json` (`citations/audit.py:659-668`) and `record_usage` fires only on a real call (`citations/service.py:117`) — so a second row is a second real charge and is CORRECT. |
| **24** | **The per-job display columns agree with the ledger across the whole app** | **PARTLY** — T4 compares the job columns to the ledger on the `ok` AND `failed` paths for coherence/evaluator/uncited_claims (`test_the_display_columns_still_agree_with_the_ledger`, which loads the job row), and the item columns for review (`test_the_review_item_columns_still_agree_with_the_ledger`). Not checked: any row written before this plan (there is no backfill, so historical totals read as zero for days before the migration), and the `no_done` / already-terminal paths. |

## Known limits, stated

- **No backfill, and the cost is one day of one ceiling.** Rows written before
  migration 0067 get no ledger entry, so historical totals read as zero for days
  before the migration, and on deployment day the org-wide cap stops binding for
  the rest of that day.

  The earlier version of this bullet justified that with "a backfill would have
  to invent timestamps". That is **false** for the current day — the timestamps
  are the same columns the old reader queried (`ChatTurn.created_at`,
  `CoherenceJob.queued_at`, `EvaluatorJob.queued_at`, `ReviewRun.triggered_at`,
  `UncitedClaimsJob.queued_at`). A same-day seed is cheap and was considered.
  The accepted cost, stated honestly instead:

  - What stops binding is the **org-wide** ceiling only, and only for the rest of
    deployment day. `check_global_daily_token_cap` has exactly two callers,
    `turns/submit.py:376` and the read-only `web/api/admin.py:240`, so in
    practice the lost protection is **chat submission**. The four fan-out gates
    (Task 8) and compaction's gate (Task 5) do not exist today, so their fresh
    budget is not a regression against current behaviour.
  - **Every per-workspace cap still binds, untouched by this plan.**
    `check_daily_token_cap` (`caps.py:94`, sums `ChatTurn`), plus the per-family
    readers at `coherence/jobs.py:169-182`, `evaluator/jobs.py:117-130` and
    `uncited_claims/jobs.py:118-133`, and review's runs/day and dollars/day
    recheck at `review/pass_runner.py:258-263`. A workspace that has already
    burned its own daily budget is still refused after the cutover.
  - **The admin panel will not reconcile on any day where the ledger and the job
    columns disagree.** `web/api/admin.py:240` reads the global total through the
    ledger after Task 7, while `admin.py:243-250` keeps summing `ChatTurn`
    per workspace. On deploy day the global figure will be smaller than the sum
    of the per-workspace rows beneath it. `admin.py` is deliberately not in this
    plan's File-structure table.
- **`status="timeout"` is reachable for the four pass runners and for citation,
  and for nothing else.** `review`, `coherence`, `evaluator` and
  `uncited_claims` distinguish it in their own `except` blocks (Task 4 Step 5),
  and `citations/service.py:87` already records it. Chat turns cannot: the time
  bound is in `turns/drain.py:106`, caught at `:110`, and reaches
  `processor.py:439` as a `CancelledError` through a generic `except
  BaseException`, so every non-success chat-turn row is `"failed"`. Compaction
  cannot either — `grep -i timeout turns/compaction.py` returns zero hits and the
  summarizer call at `:152` has no `wait_for`. Neither is faked.
- **Two pass-runner paths still record nothing, and this plan does not close
  them.** `coherence/pass_runner.py:154-156` and `evaluator/pass_runner.py:174-176`
  return when the job is no longer `running` — above their token writes — so a
  pass that completed and reported usage against a job the reaper already failed
  records nothing, in the columns today and in the ledger after this plan. Task 9's
  guard cannot see it (those functions do record on their other branch).
  `uncited_claims` is the one family whose equivalent branch is below the token
  write, and Task 4 Step 4 adds a record there.
- **Phase-0 workspace setup spends hosted tokens that no ledger and no cap sees.**
  `phases/setup.py:713,740` calls an Anthropic client, `requeue_stuck_setup_jobs`
  (`:214`) genuinely puts a job back to `queued` and `:698` retries in-band up to
  `MAX_SETUP_ATTEMPTS`, so a retry really does repeat a hosted call. It stamps no
  token column at all, so Task 9's guard will never flag it, and `setup` is not
  one of the seven `kind` values. It was invisible before this plan and is
  invisible after it. Recorded here so nobody reads "every hosted-LLM call in the
  app writes one row" as already true of setup.
- **Failed runs now count toward the PER-FAMILY daily caps too.** Task 4 Step 5
  makes `_fail` write `job.input_tokens`/`output_tokens`, and
  `coherence_cap_exceeded` (`coherence/jobs.py:174-182`),
  `evaluator_cap_exceeded` (`evaluator/jobs.py:122-130`) and
  `uncited_claims_cap_exceeded` (`uncited_claims/jobs.py:123-133`) each sum
  exactly those two columns. So a workspace whose passes keep failing will now
  reach its own per-family ceiling, where before it could retry indefinitely for
  free. That is the correct behaviour — the tokens were spent — but it is a
  user-visible change that Task 4 makes as a side effect of keeping the display
  cache true, and it is not asserted by any test in this plan.
- **Review's late and failed sub-calls now count where they did not.** Task 4
  Step 3b hoists `done`, so `review/pass_runner.py:161-165` stamps
  `input_tokens`/`output_tokens`/`estimated_dollars` on failed and cancelled
  items too. `rollup_run_totals` and therefore `review_dollars_today` — the
  per-workspace dollar cap — begin counting spend that previously vanished. That
  is the intended correction, but it is a behaviour change on a cap users see.
- **Append-only is a convention, not a trigger.** Nothing at the database level
  stops an UPDATE. The guarantee is one writer plus Task 9's gate — including
  `test_the_ledger_has_exactly_one_writer`, which is what actually checks the
  "one writer" claim.
- **Task 8 changes what users can do.** Four endpoints begin refusing at a cap
  they previously ignored. If that is not wanted, drop Task 8 — the ledger and
  the cap's correctness do not depend on it.
- **Citation unification is user-visible.** Reference processing now competes
  with drafting for the same daily budget.
