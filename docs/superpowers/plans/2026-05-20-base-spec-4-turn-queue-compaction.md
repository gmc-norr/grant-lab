# Base Spec B4 — Turn Queue + Chat Machinery + Compaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the turn submission flow, FIFO drain, per-event chat-turn persistence, compaction, daily-token-cap enforcement, and the B3 native-resume handoff (capture `TurnDoneEvent.session_id`, persist per workspace, populate `LaunchContext.resume_session_id` on next launch). End state: a test (and eventually B6's POST endpoint) calls `submit_user_turn(...)` → atomic insert of `chat_turns(role=user)` + `turn_jobs(status=queued)`. The scheduler drains queued jobs one-at-a-time per workspace via `WorkspaceController.send_turn`. Each `StreamEvent` writes one `chat_turns` row. `TurnDoneEvent` finalizes the job, stamps tokens, and persists `session_id` on the workspace for next launch's native SDK resume. Compaction tick runs periodically; when a workspace's un-summarized turns exceed 100 or its un-summarized input tokens exceed 200K, a summarizer LLM rolls them up into a `chat_summaries` row.

**Architecture:** B4 is a service layer that sits between the (future) B6 web layer and the B3 drafting machinery. New package `src/call_monitor/proposal_assist/turns/` owns submit + drain + processor + compaction + caps. No FastAPI endpoints in this slice — B6 adds those. The drain scheduler tick is the production trigger; the integration test invokes the same code paths synchronously via `submit_user_turn` + `process_turn_once` to keep tests deterministic.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x (`SELECT FOR UPDATE SKIP LOCKED` for atomic claim), Alembic (one migration for new workspace columns), APScheduler for the drain + compaction ticks, the existing `AnthropicClient` for compaction summarization, `pytest` + testcontainers for the real-Postgres integration test.

**Spec:** [docs/superpowers/specs/2026-04-18-call-manager-proposal-assist-design.md](../specs/2026-04-18-call-manager-proposal-assist-design.md), §Data model (lines 86–203), §Chat mechanics (lines 302–308), §Rate/spend guardrails (lines 354–358).

**Overview:** [docs/superpowers/plans/2026-05-19-base-spec-overview.md](2026-05-19-base-spec-overview.md), §B4.

---

## Prerequisites and scope honesty

**Already on master:**
- B1: 9 base-spec tables + models + `create_workspace()`.
- Slice A Part 2: phased columns on `workspaces`, `workspace_setup_jobs`, Phase 0 worker, scheduler tick for Phase 0.
- B3: `WorkspaceController` (launch/send_turn/terminate/sweep_idle), `SubprocessRegistry`, advisory locking, orphan sweep, `DraftingAgent` Protocol, `ClaudeAgentSDKAdapter`, `FakeAgentAdapter`, factory + DI, idle-sweep scheduler tick, `LaunchContext.resume_session_id` field + `TurnDoneEvent.session_id` field (B4 wires the persistence).

**What B4 does NOT do:**
- POST `/workspaces/<id>/turns` endpoint, SSE bridge, "queued" UI position indicator — **B6**.
- First-time disclosure modal, per-turn banner UI, `disclosure_acknowledged` event writing — **B6 + B8** (disclosure UX lives in B6; the per-turn `cloud_turn_sent` audit event IS in B4 scope — see Task 5).
- Tool implementations (`edit_proposal`, etc.) and tool routing inside the subprocess — **B5**.
- Figure/attachment handling in chat — **B7**.
- Daily-cap UX surfacing (banner, admin override, "paused until midnight" status) — **B8**. B4 enforces the cap (rejects pre-enqueue when exceeded); only the rejection mechanism is in scope.
- Global kill switch (`DRAFTING_ENABLED` env var + admin toggle) — **B8**.
- Per-workspace cost dashboards in `/admin` — **B8**.

B4 produces a fully-functional turn pipeline tested end-to-end against `FakeAgentAdapter`. B5 plugs real tools into it. B6 plugs the web layer into it. B8 layers in compliance / spend visibility.

---

## Conventions reference

- Testcontainers + `pg_session_factory` infrastructure as before.
- Migrations chain off `0028`.
- New code under `src/call_monitor/proposal_assist/turns/`.
- Don't sweep up pre-existing model/format violations.
- Tests under `tests/proposal_assist/test_turns_*.py`.
- The `_settings_env` fixture supplies baseline env.

---

## File map

**Created:**
- `alembic/versions/0029_workspace_b4_columns.py` — adds `workspaces.last_sdk_session_id` + per-workspace daily-cap override columns.
- `src/call_monitor/proposal_assist/turns/__init__.py` — package + public re-exports (submit + caps + processor entry point).
- `src/call_monitor/proposal_assist/turns/caps.py` — daily-token-cap check + `TokenCapExceeded` exception.
- `src/call_monitor/proposal_assist/turns/submit.py` — `submit_user_turn(session, ...)` atomic insert.
- `src/call_monitor/proposal_assist/turns/processor.py` — `process_turn_once(...)` (claim + dispatch + write rows + finalize + handle errors).
- `src/call_monitor/proposal_assist/turns/drain.py` — `drain_turn_queue_tick(...)` scheduler-driven driver that finds workspaces with queued turns and dispatches one per workspace.
- `src/call_monitor/proposal_assist/turns/compaction.py` — `compact_workspace(...)` + `run_compaction_tick(...)`.
- Tests at `tests/proposal_assist/test_turns_caps.py`, `test_turns_submit.py`, `test_turns_processor.py`, `test_turns_drain.py`, `test_turns_compaction.py`, `test_turns_session_id_persistence.py`, `test_turns_end_to_end.py`, `test_turns_scheduler_tick.py`.

**Modified:**
- `src/call_monitor/db/models.py` — add `Workspace.last_sdk_session_id`, `daily_token_cap_in_override`, `daily_token_cap_out_override` columns.
- `src/call_monitor/config.py` — add `daily_token_cap_input`, `daily_token_cap_output`, `compaction_turn_threshold`, `compaction_token_threshold`, `compaction_tick_seconds`, `turn_drain_tick_seconds` settings.
- `src/call_monitor/proposal_assist/drafting/controller.py` — `launch()` reads `workspace.last_sdk_session_id` and populates `LaunchContext.resume_session_id`.
- `src/call_monitor/scheduler.py` — register `turn_drain` tick + `compaction_tick`.
- `src/call_monitor/main.py` — pass any new construction args through to `build_scheduler`.

---

## Revision history

- **rev1 (2026-05-20)** — initial plan.

---

## Task 1: schema additions for B4

Adds three columns to `workspaces`:
- `last_sdk_session_id: text | null` — populated by the turn processor from `TurnDoneEvent.session_id`; consumed by the controller on next launch for native SDK resume.
- `daily_token_cap_in_override: integer | null` — admin-set per-workspace override of the global input cap.
- `daily_token_cap_out_override: integer | null` — same for output.

**Files:**
- Create: `alembic/versions/0029_workspace_b4_columns.py`
- Modify: `src/call_monitor/db/models.py`
- Test: `tests/proposal_assist/test_models_workspace_b4_columns.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_models_workspace_b4_columns.py`:

```python
import uuid

from sqlalchemy import inspect, select

from call_monitor.db.models import Workspace
from tests.proposal_assist.conftest import make_user, make_workspace


def test_workspace_has_last_sdk_session_id_column():
    cols = {c.name for c in inspect(Workspace).columns}
    assert "last_sdk_session_id" in cols
    assert "daily_token_cap_in_override" in cols
    assert "daily_token_cap_out_override" in cols


def test_workspace_defaults_b4_columns_to_null(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    with pg_session_factory() as s:
        row = s.scalar(select(Workspace).where(Workspace.id == wsid))
        assert row.last_sdk_session_id is None
        assert row.daily_token_cap_in_override is None
        assert row.daily_token_cap_out_override is None


def test_workspace_can_set_last_sdk_session_id(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    with pg_session_factory() as s:
        row = s.scalar(select(Workspace).where(Workspace.id == wsid))
        row.last_sdk_session_id = "abc-123"
        s.commit()
    with pg_session_factory() as s:
        row = s.scalar(select(Workspace).where(Workspace.id == wsid))
        assert row.last_sdk_session_id == "abc-123"
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_models_workspace_b4_columns.py -v
```

Expected: AttributeError / column not in inspector results.

- [ ] **Step 3: Write the migration**

`alembic/versions/0029_workspace_b4_columns.py`:

```python
"""workspace B4 columns: last_sdk_session_id + daily cap overrides

Revision ID: 0029_workspace_b4_columns
Revises: 0028_chat_turns_position
Create Date: 2026-05-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0029_workspace_b4_columns"
down_revision = "0028_chat_turns_position"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("last_sdk_session_id", sa.Text, nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("daily_token_cap_in_override", sa.Integer, nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("daily_token_cap_out_override", sa.Integer, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "daily_token_cap_out_override")
    op.drop_column("workspaces", "daily_token_cap_in_override")
    op.drop_column("workspaces", "last_sdk_session_id")
```

- [ ] **Step 4: Add the columns to the Workspace model**

In `src/call_monitor/db/models.py`, add to `Workspace` (alongside the other nullable columns):

```python
    last_sdk_session_id: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )
    daily_token_cap_in_override: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    daily_token_cap_out_override: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
```

- [ ] **Step 5: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_models_workspace_b4_columns.py -v
# expect 3 passing
uv run ruff format alembic/versions/0029_workspace_b4_columns.py src/call_monitor/db/models.py tests/proposal_assist/test_models_workspace_b4_columns.py
git add alembic/versions/0029_workspace_b4_columns.py src/call_monitor/db/models.py tests/proposal_assist/test_models_workspace_b4_columns.py
git commit -m "feat(b4): workspace columns — last_sdk_session_id + daily cap overrides"
```

---

## Task 2: settings for caps + compaction + drain cadence

Six new settings:

```python
daily_token_cap_input: int = 500_000           # alias DAILY_TOKEN_CAP_INPUT
daily_token_cap_output: int = 100_000          # alias DAILY_TOKEN_CAP_OUTPUT
compaction_turn_threshold: int = 100           # alias COMPACTION_TURN_THRESHOLD
compaction_token_threshold: int = 200_000      # alias COMPACTION_TOKEN_THRESHOLD
compaction_tick_seconds: int = 300             # alias COMPACTION_TICK_SECONDS
turn_drain_tick_seconds: int = 5               # alias TURN_DRAIN_TICK_SECONDS
```

**Files:**
- Modify: `src/call_monitor/config.py`
- Test: extend an existing settings test if there is one, or skip — these are mechanical Pydantic Fields.

- [ ] **Step 1: Add to `Settings`**

Place near `workspace_idle_sweep_seconds` (the closest related block):

```python
    daily_token_cap_input: int = Field(
        default=500_000,
        alias="DAILY_TOKEN_CAP_INPUT",
        description=(
            "Per-workspace default daily input-token cap (Europe/Stockholm "
            "day). Overridable per-workspace via "
            "workspaces.daily_token_cap_in_override. Enforced pre-enqueue "
            "by submit_user_turn(); B8 will surface the UX."
        ),
    )
    daily_token_cap_output: int = Field(
        default=100_000,
        alias="DAILY_TOKEN_CAP_OUTPUT",
        description="Per-workspace default daily output-token cap.",
    )
    compaction_turn_threshold: int = Field(
        default=100,
        alias="COMPACTION_TURN_THRESHOLD",
        description=(
            "Number of un-summarized chat_turns since the latest "
            "chat_summaries row that triggers compaction."
        ),
    )
    compaction_token_threshold: int = Field(
        default=200_000,
        alias="COMPACTION_TOKEN_THRESHOLD",
        description=(
            "Sum of input-tokens across un-summarized chat_turns that "
            "triggers compaction (whichever threshold trips first)."
        ),
    )
    compaction_tick_seconds: int = Field(
        default=300,
        alias="COMPACTION_TICK_SECONDS",
        description="How often the compaction tick scans for workspaces over threshold.",
    )
    turn_drain_tick_seconds: int = Field(
        default=5,
        alias="TURN_DRAIN_TICK_SECONDS",
        description=(
            "How often the turn-drain tick picks one queued turn per "
            "workspace and dispatches it. Lower = snappier perceived "
            "latency; higher = less polling overhead. B6's SSE bridge "
            "may add its own zero-latency wakeup so this is a floor, "
            "not the only mechanism."
        ),
    )
```

- [ ] **Step 2: Smoke test + commit**

```bash
uv run python -c "from call_monitor.config import Settings; s = Settings(); print(s.daily_token_cap_input, s.compaction_turn_threshold, s.turn_drain_tick_seconds)"
# expect: 500000 100 5

uv run ruff format src/call_monitor/config.py
git add src/call_monitor/config.py
git commit -m "feat(b4): cap + compaction + drain-cadence settings"
```

---

## Task 3: `check_daily_token_cap` + `TokenCapExceeded`

The pre-enqueue gate. Sums today's `chat_turns.tokens_in` and `chat_turns.tokens_out` for the workspace (Europe/Stockholm day boundary), compares against the per-workspace override OR the setting default. Returns a structured result that submit_user_turn turns into an exception when exceeded.

**Files:**
- Create: `src/call_monitor/proposal_assist/turns/__init__.py` (empty for now)
- Create: `src/call_monitor/proposal_assist/turns/caps.py`
- Test: `tests/proposal_assist/test_turns_caps.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_turns_caps.py`:

```python
from datetime import UTC, datetime, timedelta

from call_monitor.proposal_assist.turns.caps import (
    TokenCapExceeded,
    check_daily_token_cap,
)
from tests.proposal_assist.conftest import (
    make_chat_turn,
    make_workspace,
)


def test_check_daily_token_cap_under_limit_returns_allowed(
    pg_session_factory, _settings_env,
):
    """Workspace with zero token usage today is allowed."""
    from call_monitor.config import Settings
    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        result = check_daily_token_cap(s, ws.id, settings=settings)
    assert result.allowed is True
    assert result.used_in == 0
    assert result.used_out == 0
    assert result.cap_in == settings.daily_token_cap_input
    assert result.cap_out == settings.daily_token_cap_output


def test_check_daily_token_cap_over_input_limit_returns_not_allowed(
    pg_session_factory, _settings_env, monkeypatch,
):
    """A workspace whose summed input tokens today >= cap is not allowed."""
    monkeypatch.setenv("DAILY_TOKEN_CAP_INPUT", "100")
    monkeypatch.setenv("DAILY_TOKEN_CAP_OUTPUT", "1000000")
    from call_monitor.config import Settings
    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        make_chat_turn(s, ws, role="assistant",
                       content={"type": "text", "text": "x"},
                       tokens_in=60)
        make_chat_turn(s, ws, role="assistant",
                       content={"type": "text", "text": "y"},
                       tokens_in=50)
        s.commit()
        result = check_daily_token_cap(s, ws.id, settings=settings)
    assert result.allowed is False
    assert result.used_in == 110
    assert result.reason == "input_cap_exceeded"


def test_check_daily_token_cap_respects_per_workspace_override(
    pg_session_factory, _settings_env, monkeypatch,
):
    """The override on workspaces.daily_token_cap_in_override takes
    precedence over the setting default."""
    monkeypatch.setenv("DAILY_TOKEN_CAP_INPUT", "100")
    from call_monitor.config import Settings
    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.daily_token_cap_in_override = 10_000  # generous override
        make_chat_turn(s, ws, role="assistant",
                       content={"type": "text", "text": "x"},
                       tokens_in=200)
        s.commit()
        result = check_daily_token_cap(s, ws.id, settings=settings)
    assert result.allowed is True
    assert result.cap_in == 10_000


def test_check_daily_token_cap_only_counts_today(
    pg_session_factory, _settings_env, monkeypatch,
):
    """Yesterday's turns don't count toward today's cap (Stockholm day boundary)."""
    monkeypatch.setenv("DAILY_TOKEN_CAP_INPUT", "100")
    from call_monitor.config import Settings
    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        # Backdate a turn by 2 days.
        old_turn = make_chat_turn(s, ws, role="assistant",
                                  content={"type": "text", "text": "x"},
                                  tokens_in=200)
        old_turn.created_at = datetime.now(UTC) - timedelta(days=2)
        s.commit()
        result = check_daily_token_cap(s, ws.id, settings=settings)
    assert result.allowed is True
    assert result.used_in == 0


def test_token_cap_exceeded_carries_diagnostic_fields():
    exc = TokenCapExceeded(
        workspace_id=__import__("uuid").uuid4(),
        used_in=200, used_out=50,
        cap_in=100, cap_out=1000,
        reason="input_cap_exceeded",
    )
    assert "200" in str(exc)
    assert exc.reason == "input_cap_exceeded"
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_turns_caps.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`src/call_monitor/proposal_assist/turns/__init__.py`:

```python
"""Proposal-assist turn machinery: submit, drain, process, compact."""
```

`src/call_monitor/proposal_assist/turns/caps.py`:

```python
"""Per-workspace daily-token-cap enforcement.

Stockholm-day boundary: tokens count toward "today" if their chat_turn's
created_at falls within the current Europe/Stockholm day. Cap defaults
live on Settings; per-workspace overrides live on
workspaces.daily_token_cap_{in,out}_override.

This module is enforcement-only. B8 owns the user-facing "you've hit the
cap" UX (banner, admin override widget, "paused until midnight" status).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from call_monitor.config import Settings
from call_monitor.db.models import ChatTurn, Workspace

_STOCKHOLM = ZoneInfo("Europe/Stockholm")


@dataclass
class CapCheckResult:
    allowed: bool
    used_in: int
    used_out: int
    cap_in: int
    cap_out: int
    reason: str | None = None  # 'input_cap_exceeded' | 'output_cap_exceeded' | None


class TokenCapExceeded(RuntimeError):
    def __init__(
        self,
        workspace_id: uuid.UUID,
        *,
        used_in: int,
        used_out: int,
        cap_in: int,
        cap_out: int,
        reason: str,
    ) -> None:
        super().__init__(
            f"workspace {workspace_id} {reason}: "
            f"used_in={used_in} used_out={used_out} cap_in={cap_in} cap_out={cap_out}"
        )
        self.workspace_id = workspace_id
        self.used_in = used_in
        self.used_out = used_out
        self.cap_in = cap_in
        self.cap_out = cap_out
        self.reason = reason


def _stockholm_day_start_utc() -> datetime:
    """Return UTC datetime corresponding to today's 00:00 in Europe/Stockholm."""
    now_se = datetime.now(_STOCKHOLM)
    midnight_se = now_se.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_se.astimezone(tz=ZoneInfo("UTC"))


def check_daily_token_cap(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    settings: Settings,
) -> CapCheckResult:
    """Sum tokens used today against the effective cap. Read-only — does
    not raise; caller (submit_user_turn) decides how to react."""
    day_start_utc = _stockholm_day_start_utc()

    sums = session.execute(
        select(
            func.coalesce(func.sum(ChatTurn.tokens_in), 0),
            func.coalesce(func.sum(ChatTurn.tokens_out), 0),
        ).where(
            ChatTurn.workspace_id == workspace_id,
            ChatTurn.created_at >= day_start_utc,
        )
    ).one()
    used_in, used_out = int(sums[0]), int(sums[1])

    ws = session.scalar(select(Workspace).where(Workspace.id == workspace_id))
    cap_in = (
        ws.daily_token_cap_in_override
        if ws is not None and ws.daily_token_cap_in_override is not None
        else settings.daily_token_cap_input
    )
    cap_out = (
        ws.daily_token_cap_out_override
        if ws is not None and ws.daily_token_cap_out_override is not None
        else settings.daily_token_cap_output
    )

    reason: str | None = None
    if used_in >= cap_in:
        reason = "input_cap_exceeded"
    elif used_out >= cap_out:
        reason = "output_cap_exceeded"

    return CapCheckResult(
        allowed=reason is None,
        used_in=used_in,
        used_out=used_out,
        cap_in=cap_in,
        cap_out=cap_out,
        reason=reason,
    )
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_turns_caps.py -v
# expect 5 passing
uv run ruff format src/call_monitor/proposal_assist/turns/__init__.py src/call_monitor/proposal_assist/turns/caps.py tests/proposal_assist/test_turns_caps.py
git add src/call_monitor/proposal_assist/turns/__init__.py src/call_monitor/proposal_assist/turns/caps.py tests/proposal_assist/test_turns_caps.py
git commit -m "feat(b4): daily-token-cap enforcement helper + TokenCapExceeded"
```

---

## Task 4: `submit_user_turn` — atomic insert

The entry point B6 will call from POST /workspaces/<id>/turns. Atomically inserts:
1. One `chat_turns` row with `role='user'`, `content=<user message in SDK-native shape>`, no token counts (user-side tokens aren't billed by us).
2. One `turn_jobs` row with `status='queued'`, `user_turn_id=<the chat_turn just written>`.

Pre-enqueue: calls `check_daily_token_cap`. If not allowed, raises `TokenCapExceeded` and rolls back (no rows written).

**Files:**
- Create: `src/call_monitor/proposal_assist/turns/submit.py`
- Test: `tests/proposal_assist/test_turns_submit.py`

- [ ] **Step 1: Write failing test**

```python
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from call_monitor.db.models import ChatTurn, TurnJob
from call_monitor.proposal_assist.turns.caps import TokenCapExceeded
from call_monitor.proposal_assist.turns.submit import submit_user_turn
from tests.proposal_assist.conftest import (
    make_chat_turn,
    make_workspace,
)


def test_submit_user_turn_writes_chat_turn_and_turn_job(
    pg_session_factory, _settings_env,
):
    from call_monitor.config import Settings
    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    with pg_session_factory() as s:
        job = submit_user_turn(
            session=s,
            workspace_id=wsid,
            content={"type": "text", "text": "Hello agent."},
            settings=settings,
        )
        s.commit()
        job_id = job.id

    with pg_session_factory() as s:
        turn_rows = list(s.scalars(
            select(ChatTurn).where(ChatTurn.workspace_id == wsid)
        ))
        job_row = s.scalar(select(TurnJob).where(TurnJob.id == job_id))

    assert len(turn_rows) == 1
    assert turn_rows[0].role == "user"
    assert turn_rows[0].content == {"type": "text", "text": "Hello agent."}
    assert job_row.status == "queued"
    assert job_row.user_turn_id == turn_rows[0].id
    assert job_row.queued_at is not None


def test_submit_user_turn_raises_when_input_cap_exceeded(
    pg_session_factory, _settings_env, monkeypatch,
):
    monkeypatch.setenv("DAILY_TOKEN_CAP_INPUT", "100")
    from call_monitor.config import Settings
    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        make_chat_turn(s, ws, role="assistant",
                       content={"type": "text", "text": "x"},
                       tokens_in=200)
        s.commit()
        wsid = ws.id

    with pg_session_factory() as s:
        with pytest.raises(TokenCapExceeded) as exc_info:
            submit_user_turn(
                session=s,
                workspace_id=wsid,
                content={"type": "text", "text": "hi"},
                settings=settings,
            )
        s.rollback()

    assert exc_info.value.reason == "input_cap_exceeded"

    # No rows written.
    with pg_session_factory() as s:
        user_rows = list(s.scalars(
            select(ChatTurn).where(
                ChatTurn.workspace_id == wsid, ChatTurn.role == "user",
            )
        ))
        jobs = list(s.scalars(
            select(TurnJob).where(TurnJob.workspace_id == wsid)
        ))
    assert user_rows == []
    assert jobs == []
```

- [ ] **Step 2: Implement**

`src/call_monitor/proposal_assist/turns/submit.py`:

```python
"""Atomically enqueue a user turn for an agent workspace.

This is the entry point B6's POST /workspaces/<id>/turns will call. It
writes one chat_turns row (role='user') and one turn_jobs row
(status='queued') in the caller's session/transaction. Caller commits.

Cap enforcement: pre-write, calls check_daily_token_cap. If exceeded,
raises TokenCapExceeded — caller (typically B6) catches this and
returns 429 to the client. No rows are written when the cap is hit.

B8 will layer the user-facing "you hit the cap" surfacing on top of
the TokenCapExceeded path.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from call_monitor.config import Settings
from call_monitor.db.models import ChatTurn, TurnJob
from call_monitor.proposal_assist.turns.caps import (
    TokenCapExceeded,
    check_daily_token_cap,
)


def submit_user_turn(
    *,
    session: Session,
    workspace_id: uuid.UUID,
    content: dict[str, Any] | list[Any] | str,
    settings: Settings,
) -> TurnJob:
    """Insert chat_turns(role='user') + turn_jobs(status='queued')
    atomically. Returns the TurnJob (not yet committed)."""
    cap = check_daily_token_cap(session, workspace_id, settings=settings)
    if not cap.allowed:
        raise TokenCapExceeded(
            workspace_id,
            used_in=cap.used_in,
            used_out=cap.used_out,
            cap_in=cap.cap_in,
            cap_out=cap.cap_out,
            reason=cap.reason or "cap_exceeded",
        )

    user_turn = ChatTurn(
        workspace_id=workspace_id,
        role="user",
        content=content,
    )
    session.add(user_turn)
    session.flush()  # so user_turn.id is populated for FK below

    job = TurnJob(
        workspace_id=workspace_id,
        user_turn_id=user_turn.id,
        status="queued",
        queued_at=datetime.now(UTC),
    )
    session.add(job)
    session.flush()
    return job
```

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_turns_submit.py -v
uv run ruff format src/call_monitor/proposal_assist/turns/submit.py tests/proposal_assist/test_turns_submit.py
git add src/call_monitor/proposal_assist/turns/submit.py tests/proposal_assist/test_turns_submit.py
git commit -m "feat(b4): submit_user_turn — atomic chat_turns + turn_jobs insert"
```

---

## Task 5: turn processor

The heart of B4. Given a `turn_jobs.id`, the processor:
1. Claims it (`UPDATE turn_jobs SET status='running', started_at=now() WHERE id=? AND status='queued'`).
2. Reads the linked user `chat_turns` content.
3. Calls `await controller.send_turn(workspace_id, user_text)`.
4. For each event yielded:
   - `AssistantTextEvent` → `chat_turns(role='assistant', content={"type":"text","text":...})`
   - `ToolUseEvent` → `chat_turns(role='assistant', content={"type":"tool_use", "id":..., "name":..., "input":...})`
   - `ToolResultEvent` → `chat_turns(role='tool', content={"type":"tool_result", "tool_use_id":..., "content":..., "is_error":...})`
   - `TurnDoneEvent` → stamp tokens on the LAST assistant `chat_turns` row written this turn (if any); update `workspaces.last_sdk_session_id = event.session_id` (if set); update `turn_jobs.status='completed'`, `completed_at=now()`; write `workspace_events(kind='cloud_turn_sent', payload={...})`.
5. Errors:
   - `SDKTurnError` → `turn_jobs.status='failed'`, `error=str(exc)`, `completed_at=now()`. No further chat_turns. Still write `workspace_events(kind='cloud_turn_sent', payload={"failed": true, "error": ...})`.
   - Any other exception → same as SDKTurnError but with `error='processor_failure: <type>: <msg>'`.

**Files:**
- Create: `src/call_monitor/proposal_assist/turns/processor.py`
- Test: `tests/proposal_assist/test_turns_processor.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_turns_processor.py`:

```python
import uuid

import pytest
from sqlalchemy import select

from call_monitor.db.models import ChatTurn, TurnJob, Workspace, WorkspaceEvent
from call_monitor.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    SDKTurnError,
    ToolResultEvent,
    ToolUseEvent,
    TurnDoneEvent,
)
from call_monitor.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_monitor.proposal_assist.drafting.registry import SubprocessRegistry
from call_monitor.proposal_assist.drafting.controller import WorkspaceController
from call_monitor.proposal_assist.turns.processor import process_turn_once
from call_monitor.proposal_assist.turns.submit import submit_user_turn
from tests.proposal_assist.conftest import make_workspace


def _build_controller_and_submit(pg_session_factory, _settings_env, events):
    from call_monitor.config import Settings
    settings = Settings()
    adapter = FakeAgentAdapter()
    adapter.script_next_turn(events)
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    with pg_session_factory() as s:
        job = submit_user_turn(
            session=s, workspace_id=wsid,
            content={"type": "text", "text": "Hi"}, settings=settings,
        )
        s.commit()
        job_id = job.id
    return wsid, job_id, controller, adapter


@pytest.mark.asyncio
async def test_process_turn_writes_assistant_text_event(
    pg_session_factory, _settings_env,
):
    wsid, job_id, controller, _ = _build_controller_and_submit(
        pg_session_factory, _settings_env,
        [
            AssistantTextEvent(text="Hello back."),
            TurnDoneEvent(tokens_in=10, tokens_out=20, model="fake", session_id="s1"),
        ],
    )

    await process_turn_once(
        session_factory=pg_session_factory, controller=controller, turn_job_id=job_id,
    )

    with pg_session_factory() as s:
        turns = list(s.scalars(
            select(ChatTurn).where(ChatTurn.workspace_id == wsid).order_by(ChatTurn.position)
        ))
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        ws = s.scalar(select(Workspace).where(Workspace.id == wsid))

    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[1].content == {"type": "text", "text": "Hello back."}
    assert turns[1].tokens_in == 10
    assert turns[1].tokens_out == 20
    assert turns[1].model == "fake"
    assert job.status == "completed"
    assert job.completed_at is not None
    assert ws.last_sdk_session_id == "s1"


@pytest.mark.asyncio
async def test_process_turn_writes_tool_events(
    pg_session_factory, _settings_env,
):
    wsid, job_id, controller, _ = _build_controller_and_submit(
        pg_session_factory, _settings_env,
        [
            AssistantTextEvent(text="Calling a tool."),
            ToolUseEvent(tool_name="X", tool_use_id="u1", input={"a": 1}),
            ToolResultEvent(tool_use_id="u1", content="result", is_error=False),
            AssistantTextEvent(text="Done."),
            TurnDoneEvent(tokens_in=5, tokens_out=15, model="fake", session_id="s2"),
        ],
    )

    await process_turn_once(
        session_factory=pg_session_factory, controller=controller, turn_job_id=job_id,
    )

    with pg_session_factory() as s:
        turns = list(s.scalars(
            select(ChatTurn).where(ChatTurn.workspace_id == wsid).order_by(ChatTurn.position)
        ))

    # user + assistant text + tool_use + tool_result + assistant text = 5 rows.
    assert [t.role for t in turns] == ["user", "assistant", "assistant", "tool", "assistant"]
    assert turns[2].content["type"] == "tool_use"
    assert turns[3].content["type"] == "tool_result"
    # Tokens land on the LAST assistant row.
    assert turns[-1].tokens_in == 5
    assert turns[-1].tokens_out == 15


@pytest.mark.asyncio
async def test_process_turn_handles_sdk_turn_error(
    pg_session_factory, _settings_env,
):
    """SDKTurnError mid-stream marks the turn_job failed and stops further writes."""
    adapter = FakeAgentAdapter()

    # Override send_turn to raise after one event.
    async def explode_after_one(handle, msg):
        yield AssistantTextEvent(text="started")
        raise SDKTurnError("rate limit", status="529")

    adapter.send_turn = explode_after_one  # type: ignore[method-assign]
    from call_monitor.proposal_assist.drafting.registry import SubprocessRegistry
    from call_monitor.proposal_assist.drafting.controller import WorkspaceController
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory,
    )

    from call_monitor.config import Settings
    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    with pg_session_factory() as s:
        job = submit_user_turn(
            session=s, workspace_id=wsid,
            content={"type": "text", "text": "hi"}, settings=settings,
        )
        s.commit()
        job_id = job.id

    await process_turn_once(
        session_factory=pg_session_factory, controller=controller, turn_job_id=job_id,
    )

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        events = list(s.scalars(
            select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == wsid)
        ))

    assert job.status == "failed"
    assert "529" in (job.error or "")
    cloud_events = [e for e in events if e.kind == "cloud_turn_sent"]
    assert len(cloud_events) == 1
    assert cloud_events[0].payload.get("failed") is True


@pytest.mark.asyncio
async def test_process_turn_writes_cloud_turn_sent_event(
    pg_session_factory, _settings_env,
):
    wsid, job_id, controller, _ = _build_controller_and_submit(
        pg_session_factory, _settings_env,
        [
            AssistantTextEvent(text="ok"),
            TurnDoneEvent(tokens_in=10, tokens_out=5, model="fake", session_id="s3"),
        ],
    )

    await process_turn_once(
        session_factory=pg_session_factory, controller=controller, turn_job_id=job_id,
    )

    with pg_session_factory() as s:
        events = list(s.scalars(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == wsid,
                WorkspaceEvent.kind == "cloud_turn_sent",
            )
        ))
    assert len(events) == 1
    payload = events[0].payload
    assert payload["tokens_in"] == 10
    assert payload["tokens_out"] == 5
    assert payload["turn_job_id"] == str(job_id)
    # Content NOT in payload — spec says content lives only in chat_turns.
    assert "content" not in payload


@pytest.mark.asyncio
async def test_process_turn_noop_if_already_running(
    pg_session_factory, _settings_env,
):
    """If the row was claimed by another worker (status='running' already),
    process_turn_once returns without raising or double-processing."""
    wsid, job_id, controller, _ = _build_controller_and_submit(
        pg_session_factory, _settings_env,
        [
            AssistantTextEvent(text="ok"),
            TurnDoneEvent(tokens_in=1, tokens_out=1, model="fake", session_id="s4"),
        ],
    )

    with pg_session_factory() as s:
        from datetime import UTC, datetime
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        job.status = "running"
        job.started_at = datetime.now(UTC)
        s.commit()

    await process_turn_once(
        session_factory=pg_session_factory, controller=controller, turn_job_id=job_id,
    )

    # Status unchanged; no extra chat_turns written.
    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        turns = list(s.scalars(
            select(ChatTurn).where(ChatTurn.workspace_id == wsid)
        ))
    assert job.status == "running"
    assert len(turns) == 1  # just the user turn from submit
```

- [ ] **Step 2: Implement**

`src/call_monitor/proposal_assist/turns/processor.py`:

```python
"""Process one queued turn end-to-end.

Called by the drain scheduler tick (one turn per workspace per tick).
Also invoked directly by the integration tests for deterministic
verification.

Responsibilities:
  - Atomically claim a queued turn (status='queued' → 'running').
  - Read the user message from the linked chat_turns row.
  - Dispatch via WorkspaceController.send_turn (async generator).
  - For each StreamEvent, write a chat_turns row.
  - On TurnDoneEvent: stamp tokens on the latest assistant row, persist
    session_id on the workspace, mark turn_job completed, write the
    audit event.
  - On SDKTurnError: mark turn_job failed, still write the audit event
    (the cloud call DID happen, just failed).
  - On any other exception: mark turn_job failed with processor_failure
    prefix, write the audit event.

Concurrency: B3 already enforces one running subprocess per workspace
via SubprocessRegistry. The atomic claim here prevents double-processing
of a single turn_jobs row if the drain tick is invoked twice (e.g.,
APScheduler max_instances slip). The DB-level unique-running-index on
turn_jobs (Slice A Part 2 migration 0026) is the belt-and-braces guard.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from call_monitor.db.models import ChatTurn, TurnJob, Workspace, WorkspaceEvent
from call_monitor.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    SDKTurnError,
    ToolResultEvent,
    ToolUseEvent,
    TurnDoneEvent,
)
from call_monitor.proposal_assist.drafting.controller import WorkspaceController

logger = logging.getLogger(__name__)


async def process_turn_once(
    *,
    session_factory: sessionmaker,
    controller: WorkspaceController,
    turn_job_id: uuid.UUID,
) -> None:
    """Process one queued turn. No-op if the row is not in 'queued' state."""
    # Atomic claim.
    with session_factory() as s:
        result = s.execute(
            update(TurnJob)
            .where(TurnJob.id == turn_job_id, TurnJob.status == "queued")
            .values(status="running", started_at=datetime.now(UTC))
            .returning(TurnJob.workspace_id, TurnJob.user_turn_id)
        ).first()
        s.commit()
        if result is None:
            logger.info("turn_job %s already claimed or completed; skipping", turn_job_id)
            return
        workspace_id, user_turn_id = result

    # Read user message.
    with session_factory() as s:
        user_turn = s.scalar(select(ChatTurn).where(ChatTurn.id == user_turn_id))
        if user_turn is None:
            logger.error("turn_job %s has no user_turn_id row", turn_job_id)
            _mark_failed(
                session_factory, turn_job_id, workspace_id,
                error="missing_user_turn_row",
            )
            return
        user_text = _content_to_text(user_turn.content)

    last_assistant_id: uuid.UUID | None = None
    tokens_in = 0
    tokens_out = 0
    model: str | None = None
    session_id: str | None = None
    sdk_error: SDKTurnError | None = None
    other_error: BaseException | None = None

    try:
        async for event in controller.send_turn(workspace_id, user_text):
            if isinstance(event, AssistantTextEvent):
                last_assistant_id = _write_chat_turn(
                    session_factory, workspace_id, role="assistant",
                    content={"type": "text", "text": event.text},
                )
            elif isinstance(event, ToolUseEvent):
                # Tool-use blocks live on assistant rows per SDK message shape.
                last_assistant_id = _write_chat_turn(
                    session_factory, workspace_id, role="assistant",
                    content={
                        "type": "tool_use",
                        "id": event.tool_use_id,
                        "name": event.tool_name,
                        "input": event.input,
                    },
                )
            elif isinstance(event, ToolResultEvent):
                _write_chat_turn(
                    session_factory, workspace_id, role="tool",
                    content={
                        "type": "tool_result",
                        "tool_use_id": event.tool_use_id,
                        "content": event.content,
                        "is_error": event.is_error,
                    },
                )
            elif isinstance(event, TurnDoneEvent):
                tokens_in = event.tokens_in or 0
                tokens_out = event.tokens_out or 0
                model = event.model
                session_id = event.session_id
    except SDKTurnError as e:
        sdk_error = e
    except BaseException as e:  # noqa: BLE001 - we re-raise BaseException after recording
        other_error = e

    # Finalize.
    if sdk_error is None and other_error is None:
        _finalize_success(
            session_factory,
            turn_job_id=turn_job_id,
            workspace_id=workspace_id,
            last_assistant_id=last_assistant_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            session_id=session_id,
        )
    elif sdk_error is not None:
        _finalize_failed(
            session_factory,
            turn_job_id=turn_job_id,
            workspace_id=workspace_id,
            error=f"sdk_turn_error: {sdk_error}",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
    else:
        assert other_error is not None
        _finalize_failed(
            session_factory,
            turn_job_id=turn_job_id,
            workspace_id=workspace_id,
            error=f"processor_failure: {type(other_error).__name__}: {other_error}",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        # Re-raise CancelledError so callers (drain tick) see it.
        if isinstance(other_error, BaseException) and not isinstance(other_error, Exception):
            raise other_error


def _content_to_text(content: Any) -> str:
    """Extract a string for the SDK. Handles plain string, single dict,
    or list-of-dicts (mirrors claude_adapter._flatten_content)."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", ""))
        return ""
    if isinstance(content, list):
        return " ".join(_content_to_text(b) for b in content if _content_to_text(b))
    return ""


def _write_chat_turn(
    session_factory: sessionmaker,
    workspace_id: uuid.UUID,
    *,
    role: str,
    content: dict[str, Any],
) -> uuid.UUID:
    with session_factory() as s:
        turn = ChatTurn(workspace_id=workspace_id, role=role, content=content)
        s.add(turn)
        s.commit()
        return turn.id


def _finalize_success(
    session_factory: sessionmaker,
    *,
    turn_job_id: uuid.UUID,
    workspace_id: uuid.UUID,
    last_assistant_id: uuid.UUID | None,
    tokens_in: int,
    tokens_out: int,
    model: str | None,
    session_id: str | None,
) -> None:
    with session_factory() as s:
        if last_assistant_id is not None:
            turn = s.scalar(select(ChatTurn).where(ChatTurn.id == last_assistant_id))
            if turn is not None:
                turn.tokens_in = tokens_in or None
                turn.tokens_out = tokens_out or None
                turn.model = model
        job = s.scalar(select(TurnJob).where(TurnJob.id == turn_job_id))
        if job is not None:
            job.status = "completed"
            job.completed_at = datetime.now(UTC)
        if session_id is not None:
            ws = s.scalar(select(Workspace).where(Workspace.id == workspace_id))
            if ws is not None:
                ws.last_sdk_session_id = session_id
        s.add(WorkspaceEvent(
            workspace_id=workspace_id,
            kind="cloud_turn_sent",
            payload={
                "turn_job_id": str(turn_job_id),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "model": model,
                "failed": False,
            },
        ))
        s.commit()


def _finalize_failed(
    session_factory: sessionmaker,
    *,
    turn_job_id: uuid.UUID,
    workspace_id: uuid.UUID,
    error: str,
    tokens_in: int,
    tokens_out: int,
) -> None:
    with session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == turn_job_id))
        if job is not None:
            job.status = "failed"
            job.error = error
            job.completed_at = datetime.now(UTC)
        s.add(WorkspaceEvent(
            workspace_id=workspace_id,
            kind="cloud_turn_sent",
            payload={
                "turn_job_id": str(turn_job_id),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "failed": True,
                "error": error,
            },
        ))
        s.commit()


def _mark_failed(
    session_factory: sessionmaker,
    turn_job_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    error: str,
) -> None:
    _finalize_failed(
        session_factory,
        turn_job_id=turn_job_id,
        workspace_id=workspace_id,
        error=error,
        tokens_in=0,
        tokens_out=0,
    )
```

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_turns_processor.py -v
# expect 5 passing
uv run ruff format src/call_monitor/proposal_assist/turns/processor.py tests/proposal_assist/test_turns_processor.py
git add src/call_monitor/proposal_assist/turns/processor.py tests/proposal_assist/test_turns_processor.py
git commit -m "feat(b4): turn processor — atomic claim, event-to-chat_turn translation, audit"
```

---

## Task 6: drain scheduler tick

Periodic tick that scans for workspaces with queued turn_jobs and dispatches one per workspace via `process_turn_once`. The B3 advisory lock + B4 atomic claim guarantee no double-processing.

**Files:**
- Create: `src/call_monitor/proposal_assist/turns/drain.py`
- Test: `tests/proposal_assist/test_turns_drain.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_turns_drain.py`:

```python
import uuid

import pytest
from sqlalchemy import select

from call_monitor.db.models import TurnJob
from call_monitor.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    TurnDoneEvent,
)
from call_monitor.proposal_assist.drafting.controller import WorkspaceController
from call_monitor.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_monitor.proposal_assist.drafting.registry import SubprocessRegistry
from call_monitor.proposal_assist.turns.drain import drain_turn_queue_tick
from call_monitor.proposal_assist.turns.submit import submit_user_turn
from tests.proposal_assist.conftest import make_workspace


@pytest.mark.asyncio
async def test_drain_tick_processes_one_queued_turn_per_workspace(
    pg_session_factory, _settings_env,
):
    """With two workspaces each having one queued turn, the drain tick
    dispatches both (one per workspace)."""
    from call_monitor.config import Settings
    settings = Settings()

    adapter = FakeAgentAdapter()
    # Two workspaces will each draw one turn; script two TurnDone sequences.
    adapter.script_next_turn([
        AssistantTextEvent(text="hello A"),
        TurnDoneEvent(tokens_in=1, tokens_out=1, model="fake", session_id="sA"),
    ])
    adapter.script_next_turn([
        AssistantTextEvent(text="hello B"),
        TurnDoneEvent(tokens_in=1, tokens_out=1, model="fake", session_id="sB"),
    ])
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory,
    )

    job_ids = []
    with pg_session_factory() as s:
        for _ in range(2):
            ws = make_workspace(s)
            s.commit()
            job = submit_user_turn(
                session=s, workspace_id=ws.id,
                content={"type": "text", "text": "hi"}, settings=settings,
            )
            s.commit()
            job_ids.append(job.id)

    await drain_turn_queue_tick(
        session_factory=pg_session_factory, controller=controller,
    )

    with pg_session_factory() as s:
        statuses = [
            s.scalar(select(TurnJob.status).where(TurnJob.id == j)) for j in job_ids
        ]
    assert statuses == ["completed", "completed"]


@pytest.mark.asyncio
async def test_drain_tick_no_op_when_no_queued_turns(
    pg_session_factory, _settings_env,
):
    """Empty queue is a noop — no errors, no work."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory,
    )
    await drain_turn_queue_tick(
        session_factory=pg_session_factory, controller=controller,
    )
    assert adapter.launches == []
```

- [ ] **Step 2: Implement**

`src/call_monitor/proposal_assist/turns/drain.py`:

```python
"""Drain scheduler tick: pick one queued turn per workspace and dispatch.

Production trigger: APScheduler interval job at TURN_DRAIN_TICK_SECONDS
cadence. The closure passes the workspace_controller (constructed by
main.py's lifespan) into this function via run_coroutine_threadsafe
onto the app loop, matching the H1 pattern from the idle-sweep tick.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import distinct, select
from sqlalchemy.orm import sessionmaker

from call_monitor.db.models import TurnJob
from call_monitor.proposal_assist.drafting.controller import WorkspaceController
from call_monitor.proposal_assist.turns.processor import process_turn_once

logger = logging.getLogger(__name__)


async def drain_turn_queue_tick(
    *,
    session_factory: sessionmaker,
    controller: WorkspaceController,
) -> int:
    """Find workspaces with queued turn_jobs; pick the oldest queued
    job per workspace; dispatch process_turn_once for each. Returns the
    count of dispatched turns."""
    # Step 1: find all (workspace_id, oldest_queued_id) pairs.
    with session_factory() as s:
        # One queued job per workspace — the oldest.
        # SELECT DISTINCT ON requires PG-specific ordering; use a window/groupby
        # approximation that works on standard SQLAlchemy.
        from sqlalchemy import func
        subq = (
            select(
                TurnJob.workspace_id,
                func.min(TurnJob.queued_at).label("oldest"),
            )
            .where(TurnJob.status == "queued")
            .group_by(TurnJob.workspace_id)
            .subquery()
        )
        rows = list(s.execute(
            select(TurnJob.id, TurnJob.workspace_id)
            .join(
                subq,
                (TurnJob.workspace_id == subq.c.workspace_id)
                & (TurnJob.queued_at == subq.c.oldest),
            )
            .where(TurnJob.status == "queued")
        ).all())

    if not rows:
        return 0

    # Step 2: process each. Sequentially within the tick — we don't want
    # to overwhelm the SDK's concurrency budget. B3's per-workspace
    # advisory lock + the registry's pending-launch coalescing handle
    # contention from any other path that might call launch concurrently.
    dispatched = 0
    for turn_id, workspace_id in rows:
        try:
            await process_turn_once(
                session_factory=session_factory,
                controller=controller,
                turn_job_id=turn_id,
            )
            dispatched += 1
        except Exception:
            logger.exception(
                "drain_turn_queue_tick: process_turn_once raised for turn_job %s "
                "(workspace %s) — continuing to next workspace",
                turn_id, workspace_id,
            )
    return dispatched
```

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_turns_drain.py -v
# expect 2 passing
uv run ruff format src/call_monitor/proposal_assist/turns/drain.py tests/proposal_assist/test_turns_drain.py
git add src/call_monitor/proposal_assist/turns/drain.py tests/proposal_assist/test_turns_drain.py
git commit -m "feat(b4): drain tick — one queued turn per workspace per tick"
```

---

## Task 7: compaction summarizer + tick

When a workspace's un-summarized chat_turns count >= `COMPACTION_TURN_THRESHOLD` OR un-summarized input tokens >= `COMPACTION_TOKEN_THRESHOLD`, a compaction job:

1. Loads the latest `chat_summaries` row (if any) to find the `summarized_through` boundary turn.
2. Loads all chat_turns with `position > boundary.position` (ordered by position).
3. Calls the summarizer model with a fixed prompt: "Summarize the following proposal-drafting conversation. Cover: user intent, agent's working approach, current state of the artifact, any pending decisions. Keep the summary under 2000 tokens."
4. Writes a new `chat_summaries` row: `summary=<text>`, `summarized_through=<id of last turn included>`, `tokens=<approx output size>`, `model=<summarizer model>`.

Uses the existing `AnthropicClient` (proposal_assist_llm_model from settings) for the summarization call.

**Files:**
- Create: `src/call_monitor/proposal_assist/turns/compaction.py`
- Test: `tests/proposal_assist/test_turns_compaction.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_turns_compaction.py`:

```python
import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from call_monitor.db.models import ChatSummary, ChatTurn
from call_monitor.proposal_assist.turns.compaction import (
    compact_workspace,
    needs_compaction,
    run_compaction_tick,
)
from tests.proposal_assist.conftest import make_chat_turn, make_workspace


def _stub_summarizer_client(text: str):
    """Build a stub AnthropicClient-like object whose chat() returns
    the given text in the standard envelope shape."""
    client = MagicMock()
    client.chat.return_value = {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "model": "claude-haiku-4-5",
    }
    client.model = "claude-haiku-4-5"
    return client


def test_needs_compaction_under_thresholds_returns_false(
    pg_session_factory, _settings_env,
):
    from call_monitor.config import Settings
    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        make_chat_turn(s, ws, role="user", content={"type": "text", "text": "x"})
        s.commit()
        assert needs_compaction(s, ws.id, settings=settings) is False


def test_needs_compaction_over_turn_threshold_returns_true(
    pg_session_factory, _settings_env, monkeypatch,
):
    monkeypatch.setenv("COMPACTION_TURN_THRESHOLD", "2")
    from call_monitor.config import Settings
    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        for i in range(3):
            make_chat_turn(s, ws, role="user", content={"type": "text", "text": f"t{i}"})
        s.commit()
        assert needs_compaction(s, ws.id, settings=settings) is True


def test_needs_compaction_over_token_threshold_returns_true(
    pg_session_factory, _settings_env, monkeypatch,
):
    monkeypatch.setenv("COMPACTION_TOKEN_THRESHOLD", "100")
    from call_monitor.config import Settings
    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        make_chat_turn(s, ws, role="assistant",
                       content={"type": "text", "text": "x"},
                       tokens_in=200)
        s.commit()
        assert needs_compaction(s, ws.id, settings=settings) is True


def test_compact_workspace_writes_summary_row(
    pg_session_factory, _settings_env,
):
    """Happy path: un-summarized turns are rolled into a chat_summaries row;
    summarized_through points at the last turn included."""
    from call_monitor.config import Settings
    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        t1 = make_chat_turn(s, ws, role="user", content={"type": "text", "text": "hi"})
        t2 = make_chat_turn(s, ws, role="assistant", content={"type": "text", "text": "hello"})
        s.commit()
        wsid = ws.id
        last_turn_id = t2.id

    client = _stub_summarizer_client("User said hi; assistant replied hello.")

    compact_workspace(
        session_factory=pg_session_factory,
        workspace_id=wsid,
        summarizer_client=client,
        settings=settings,
    )

    with pg_session_factory() as s:
        summaries = list(s.scalars(
            select(ChatSummary).where(ChatSummary.workspace_id == wsid)
        ))
    assert len(summaries) == 1
    assert "User said hi" in summaries[0].summary
    assert summaries[0].summarized_through == last_turn_id


def test_compact_workspace_only_summarizes_new_turns_after_prior_summary(
    pg_session_factory, _settings_env,
):
    """If a prior summary exists, only turns AFTER its summarized_through
    boundary are included in the next summary."""
    from call_monitor.config import Settings
    from datetime import UTC, datetime
    settings = Settings()
    client = _stub_summarizer_client("Second summary.")
    with pg_session_factory() as s:
        ws = make_workspace(s)
        old_turn = make_chat_turn(s, ws, role="user", content={"type": "text", "text": "old"})
        s.flush()
        # Write a prior summary that covers old_turn.
        prior = ChatSummary(
            workspace_id=ws.id,
            summary="Old summary.",
            summarized_through=old_turn.id,
            tokens=10,
            model="claude-haiku-4-5",
        )
        s.add(prior)
        new_turn = make_chat_turn(s, ws, role="user", content={"type": "text", "text": "new"})
        s.commit()
        wsid = ws.id
        new_turn_id = new_turn.id

    compact_workspace(
        session_factory=pg_session_factory,
        workspace_id=wsid,
        summarizer_client=client,
        settings=settings,
    )

    # The call to client.chat() should have ONLY seen the new turn — verify by
    # inspecting the messages= arg.
    sent_messages = client.chat.call_args.kwargs.get("messages") or client.chat.call_args.args[0]
    sent_payload = str(sent_messages)
    assert "old" not in sent_payload
    assert "new" in sent_payload

    with pg_session_factory() as s:
        summaries = list(s.scalars(
            select(ChatSummary).where(ChatSummary.workspace_id == wsid)
            .order_by(ChatSummary.created_at)
        ))
    assert len(summaries) == 2
    assert summaries[1].summarized_through == new_turn_id


def test_compact_workspace_noop_when_no_new_turns(
    pg_session_factory, _settings_env,
):
    """If summarized_through already points to the latest turn, no work."""
    from call_monitor.config import Settings
    settings = Settings()
    client = _stub_summarizer_client("(should not be called)")
    with pg_session_factory() as s:
        ws = make_workspace(s)
        t = make_chat_turn(s, ws, role="user", content={"type": "text", "text": "x"})
        s.flush()
        prior = ChatSummary(
            workspace_id=ws.id,
            summary="...",
            summarized_through=t.id,
            tokens=1,
            model="claude-haiku-4-5",
        )
        s.add(prior)
        s.commit()
        wsid = ws.id

    compact_workspace(
        session_factory=pg_session_factory,
        workspace_id=wsid,
        summarizer_client=client,
        settings=settings,
    )
    client.chat.assert_not_called()


def test_run_compaction_tick_processes_only_workspaces_over_threshold(
    pg_session_factory, _settings_env, monkeypatch,
):
    monkeypatch.setenv("COMPACTION_TURN_THRESHOLD", "2")
    from call_monitor.config import Settings
    settings = Settings()
    client = _stub_summarizer_client("Summary.")
    with pg_session_factory() as s:
        ws_over = make_workspace(s)
        for i in range(3):
            make_chat_turn(s, ws_over, role="user",
                           content={"type": "text", "text": f"x{i}"})
        ws_under = make_workspace(s)
        make_chat_turn(s, ws_under, role="user",
                       content={"type": "text", "text": "x"})
        s.commit()
        over_id = ws_over.id
        under_id = ws_under.id

    count = run_compaction_tick(
        session_factory=pg_session_factory,
        summarizer_client_factory=lambda: client,
        settings=settings,
    )
    assert count == 1

    with pg_session_factory() as s:
        over_summaries = list(s.scalars(
            select(ChatSummary).where(ChatSummary.workspace_id == over_id)
        ))
        under_summaries = list(s.scalars(
            select(ChatSummary).where(ChatSummary.workspace_id == under_id)
        ))
    assert len(over_summaries) == 1
    assert len(under_summaries) == 0
```

- [ ] **Step 2: Implement**

`src/call_monitor/proposal_assist/turns/compaction.py`:

```python
"""Bounded-replay compaction.

When a workspace accumulates many turns or many tokens, summarize the
un-summarized ones into a chat_summaries row. This caps the SDK
relaunch cost (once native session-id resume is wired) AND the
displayable history's size for B6.

Trigger: COMPACTION_TURN_THRESHOLD turns OR COMPACTION_TOKEN_THRESHOLD
input tokens since the latest chat_summaries.summarized_through
boundary. Whichever trips first.

Summarizer: the existing AnthropicClient (proposal_assist_llm_model
setting). Called with a fixed prompt; expects plain text back. A
fact-of-life: summarization itself uses tokens that count toward the
day's cap. The cap is checked at submit time, not here — compaction
runs on a separate budget.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from call_monitor.config import Settings
from call_monitor.db.models import ChatSummary, ChatTurn, Workspace

logger = logging.getLogger(__name__)

_COMPACTION_PROMPT = """You are summarizing a portion of a proposal-drafting conversation.

Summarize the conversation below into a compact briefing that captures:
- The user's overall intent and any specific asks they've made
- The agent's working approach (tool calls, files touched, decisions)
- The current state of the proposal artifact (sections completed, pending)
- Any unresolved questions or pending decisions

Keep the summary under 2000 tokens. Do not include direct quotes unless essential. Write as a coherent briefing, not bullet points.

Conversation:
"""


def _summarized_through_position(session: Session, workspace_id: uuid.UUID) -> int | None:
    """Return the `position` of the chat_turn the latest summary covers,
    or None if there is no summary yet."""
    latest = session.execute(
        select(ChatSummary.summarized_through)
        .where(ChatSummary.workspace_id == workspace_id)
        .order_by(ChatSummary.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is None:
        return None
    pos = session.scalar(
        select(ChatTurn.position).where(ChatTurn.id == latest)
    )
    return int(pos) if pos is not None else None


def _un_summarized_stats(
    session: Session, workspace_id: uuid.UUID,
) -> tuple[int, int]:
    """Return (count, sum_tokens_in) of chat_turns NOT yet summarized."""
    boundary = _summarized_through_position(session, workspace_id)
    base_q = select(
        func.count(ChatTurn.id),
        func.coalesce(func.sum(ChatTurn.tokens_in), 0),
    ).where(ChatTurn.workspace_id == workspace_id)
    if boundary is not None:
        base_q = base_q.where(ChatTurn.position > boundary)
    count, tokens = session.execute(base_q).one()
    return int(count), int(tokens)


def needs_compaction(
    session: Session, workspace_id: uuid.UUID, *, settings: Settings,
) -> bool:
    count, tokens = _un_summarized_stats(session, workspace_id)
    return (
        count >= settings.compaction_turn_threshold
        or tokens >= settings.compaction_token_threshold
    )


def compact_workspace(
    *,
    session_factory: sessionmaker,
    workspace_id: uuid.UUID,
    summarizer_client: Any,  # duck-typed: AnthropicClient or stub
    settings: Settings,
) -> ChatSummary | None:
    """Roll un-summarized turns for one workspace into a chat_summaries
    row. Returns the new summary row, or None if there were no turns
    to summarize."""
    with session_factory() as s:
        boundary_pos = _summarized_through_position(s, workspace_id)
        turns_q = select(ChatTurn).where(ChatTurn.workspace_id == workspace_id)
        if boundary_pos is not None:
            turns_q = turns_q.where(ChatTurn.position > boundary_pos)
        turns = list(s.scalars(turns_q.order_by(ChatTurn.position)))

    if not turns:
        return None

    # Build the input messages for the summarizer.
    formatted_conv = "\n\n".join(
        f"[{t.role}] {json.dumps(t.content, ensure_ascii=False)}"
        for t in turns
    )
    user_message = _COMPACTION_PROMPT + formatted_conv

    envelope = summarizer_client.chat(
        messages=[{"role": "user", "content": user_message}],
    )
    summary_text = envelope["choices"][0]["message"]["content"]
    used_model = envelope.get("model") or getattr(summarizer_client, "model", "unknown")

    with session_factory() as s:
        new_summary = ChatSummary(
            workspace_id=workspace_id,
            summary=summary_text,
            summarized_through=turns[-1].id,
            tokens=len(summary_text) // 4,  # rough token estimate
            model=used_model,
        )
        s.add(new_summary)
        s.commit()
        s.refresh(new_summary)
        return new_summary


def run_compaction_tick(
    *,
    session_factory: sessionmaker,
    summarizer_client_factory: Callable[[], Any],
    settings: Settings,
) -> int:
    """Scan all workspaces; for any over threshold, call compact_workspace.
    Returns the count of workspaces compacted.

    The factory pattern lets the scheduler construct the AnthropicClient
    lazily — same shape as the Phase 0 tick — so a missing API key
    surfaces only when work is actually needed."""
    with session_factory() as s:
        workspace_ids = list(s.scalars(
            select(Workspace.id).where(Workspace.soft_deleted_at.is_(None))
        ))

    compacted = 0
    client: Any | None = None
    for wsid in workspace_ids:
        with session_factory() as s:
            if not needs_compaction(s, wsid, settings=settings):
                continue
        if client is None:
            client = summarizer_client_factory()
        try:
            compact_workspace(
                session_factory=session_factory,
                workspace_id=wsid,
                summarizer_client=client,
                settings=settings,
            )
            compacted += 1
        except Exception:
            logger.exception(
                "compaction_tick: compact_workspace failed for %s — continuing",
                wsid,
            )
    return compacted
```

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_turns_compaction.py -v
# expect 7 passing
uv run ruff format src/call_monitor/proposal_assist/turns/compaction.py tests/proposal_assist/test_turns_compaction.py
git add src/call_monitor/proposal_assist/turns/compaction.py tests/proposal_assist/test_turns_compaction.py
git commit -m "feat(b4): compaction — needs_compaction + compact_workspace + run_compaction_tick"
```

---

## Task 8: WorkspaceController reads `last_sdk_session_id` on launch

The B3 handoff item. Inside the existing `work_session` block (where the orphan sweep runs), also fetch `workspace.last_sdk_session_id` and pass it on `LaunchContext`.

**Files:**
- Modify: `src/call_monitor/proposal_assist/drafting/controller.py`
- Test: `tests/proposal_assist/test_turns_session_id_persistence.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_turns_session_id_persistence.py`:

```python
import uuid

import pytest
from sqlalchemy import select

from call_monitor.db.models import Workspace
from call_monitor.proposal_assist.drafting.contract import LaunchContext
from call_monitor.proposal_assist.drafting.controller import WorkspaceController
from call_monitor.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_monitor.proposal_assist.drafting.registry import SubprocessRegistry
from tests.proposal_assist.conftest import make_workspace


@pytest.mark.asyncio
async def test_controller_launch_populates_resume_session_id_from_workspace(
    pg_session_factory,
):
    """When workspaces.last_sdk_session_id is set, controller.launch()
    passes it to the adapter via LaunchContext.resume_session_id —
    the B4 handoff for B3's native-resume scaffolding."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.last_sdk_session_id = "previous-session-uuid"
        s.commit()
        wsid = ws.id

    await controller.launch(wsid)

    # FakeAdapter records every LaunchContext it sees.
    assert len(adapter.launches) == 1
    assert adapter.launches[0].resume_session_id == "previous-session-uuid"


@pytest.mark.asyncio
async def test_controller_launch_resume_session_id_is_none_for_fresh_workspace(
    pg_session_factory,
):
    """A workspace with no prior session yields LaunchContext.resume_session_id=None."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    await controller.launch(wsid)
    assert adapter.launches[0].resume_session_id is None
```

- [ ] **Step 2: Modify `controller.py` `launch()`**

Inside the existing `work_session` block, after the orphan sweep, add a Workspace lookup:

```python
with self._session_factory() as work_session:
    sweep_orphaned_turn_jobs(work_session, workspace_id)
    ws_row = work_session.scalar(
        select(Workspace).where(Workspace.id == workspace_id)
    )
    resume_session_id = (
        ws_row.last_sdk_session_id if ws_row is not None else None
    )
    work_session.commit()
context = LaunchContext(
    workspace_id=workspace_id,
    resume_session_id=resume_session_id,
)
```

Add the imports:

```python
from sqlalchemy import select
from call_monitor.db.models import Workspace
```

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_turns_session_id_persistence.py tests/proposal_assist/test_drafting_controller.py -v
# expect all passing, new tests + existing ones unaffected
uv run ruff format src/call_monitor/proposal_assist/drafting/controller.py tests/proposal_assist/test_turns_session_id_persistence.py
git add src/call_monitor/proposal_assist/drafting/controller.py tests/proposal_assist/test_turns_session_id_persistence.py
git commit -m "feat(b4): controller reads workspace.last_sdk_session_id for native resume"
```

---

## Task 9: end-to-end integration test

Submit a user turn, drive the drain tick, verify the full pipeline:
- chat_turns rows written (user + assistant + tool ones).
- turn_jobs status='completed'.
- workspace.last_sdk_session_id populated.
- workspace_events('cloud_turn_sent') written.

Then submit a SECOND turn and verify the controller's launch passes resume_session_id correctly.

**Files:**
- Test only: `tests/proposal_assist/test_turns_end_to_end.py`

- [ ] **Step 1: Write the integration test**

```python
"""End-to-end: submit_user_turn → drain_turn_queue_tick → processor →
all rows materialized. Then second turn proves resume_session_id flows."""
import uuid

import pytest
from sqlalchemy import select

from call_monitor.db.models import ChatTurn, TurnJob, Workspace, WorkspaceEvent
from call_monitor.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnDoneEvent,
)
from call_monitor.proposal_assist.drafting.controller import WorkspaceController
from call_monitor.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_monitor.proposal_assist.drafting.registry import SubprocessRegistry
from call_monitor.proposal_assist.turns.drain import drain_turn_queue_tick
from call_monitor.proposal_assist.turns.submit import submit_user_turn
from tests.proposal_assist.conftest import make_workspace


@pytest.mark.asyncio
async def test_full_turn_pipeline(pg_session_factory, _settings_env):
    """Submit → drain → processor → expected state.

    Then submit a second turn and verify resume_session_id flows."""
    from call_monitor.config import Settings
    settings = Settings()
    adapter = FakeAgentAdapter()
    adapter.script_next_turn([
        AssistantTextEvent(text="Reading the spec..."),
        ToolUseEvent(tool_name="read_artifact", tool_use_id="u1", input={}),
        ToolResultEvent(tool_use_id="u1", content="<spec contents>", is_error=False),
        AssistantTextEvent(text="Done."),
        TurnDoneEvent(tokens_in=100, tokens_out=50, model="fake", session_id="sess-A"),
    ])
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory,
    )

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    with pg_session_factory() as s:
        job = submit_user_turn(
            session=s, workspace_id=wsid,
            content={"type": "text", "text": "Get the spec."},
            settings=settings,
        )
        s.commit()
        job_id = job.id

    await drain_turn_queue_tick(
        session_factory=pg_session_factory, controller=controller,
    )

    with pg_session_factory() as s:
        turns = list(s.scalars(
            select(ChatTurn).where(ChatTurn.workspace_id == wsid)
            .order_by(ChatTurn.position)
        ))
        job_row = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        ws_row = s.scalar(select(Workspace).where(Workspace.id == wsid))
        cloud_events = list(s.scalars(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == wsid,
                WorkspaceEvent.kind == "cloud_turn_sent",
            )
        ))

    assert [t.role for t in turns] == ["user", "assistant", "assistant", "tool", "assistant"]
    assert job_row.status == "completed"
    assert ws_row.last_sdk_session_id == "sess-A"
    assert len(cloud_events) == 1
    assert cloud_events[0].payload["tokens_in"] == 100

    # Now submit a SECOND turn. Terminate first to force a fresh launch
    # (otherwise the cached subprocess is reused and resume_session_id
    # isn't passed again).
    await controller.terminate(wsid)
    adapter.script_next_turn([
        AssistantTextEvent(text="hi again"),
        TurnDoneEvent(tokens_in=10, tokens_out=5, model="fake", session_id="sess-B"),
    ])

    with pg_session_factory() as s:
        submit_user_turn(
            session=s, workspace_id=wsid,
            content={"type": "text", "text": "Another turn."},
            settings=settings,
        )
        s.commit()

    await drain_turn_queue_tick(
        session_factory=pg_session_factory, controller=controller,
    )

    # The second launch's LaunchContext should have resume_session_id='sess-A'.
    assert adapter.launches[-1].resume_session_id == "sess-A"

    with pg_session_factory() as s:
        ws_row = s.scalar(select(Workspace).where(Workspace.id == wsid))
    assert ws_row.last_sdk_session_id == "sess-B"
```

- [ ] **Step 2: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_turns_end_to_end.py -v
# expect 1 passing
uv run ruff format tests/proposal_assist/test_turns_end_to_end.py
git add tests/proposal_assist/test_turns_end_to_end.py
git commit -m "test(b4): end-to-end turn pipeline + resume_session_id round trip"
```

---

## Task 10: scheduler ticks + wiring + milestone

Wire the drain + compaction ticks into `build_scheduler`, pass any new construction args from `main.py`, run the full suite + ruff, surface the B4 completion summary.

**Files:**
- Modify: `src/call_monitor/scheduler.py`
- Modify: `src/call_monitor/main.py` (verify — should already pass workspace_controller from B3)
- Test: `tests/proposal_assist/test_turns_scheduler_tick.py`

- [ ] **Step 1: Write failing test**

```python
"""The two new scheduler jobs: turn_drain + compaction_tick.

Verify both are registered with the right ids/intervals; verify
invoking each closure dispatches to the right code."""
import asyncio

import pytest


@pytest.mark.asyncio
async def test_build_scheduler_registers_turn_drain_and_compaction_ticks(
    monkeypatch, pg_url,
):
    from unittest.mock import MagicMock

    from call_monitor.config import Settings
    from call_monitor.db.session import make_engine, make_session_factory
    from call_monitor.proposal_assist.drafting.factory import (
        build_workspace_controller,
    )
    from call_monitor.scheduler import build_scheduler

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")
    settings = Settings()

    engine = make_engine(settings.database_url)
    try:
        session_factory = make_session_factory(engine)
        controller = build_workspace_controller(
            settings=settings, session_factory=session_factory,
        )

        class _Alerter:
            def record_failure(self, source_slug, *, error=None): pass
            def record_success(self, source_slug): pass

        embed_client = MagicMock(model="bge-m3")
        sched = build_scheduler(
            session_factory=session_factory,
            settings=settings,
            alerter=_Alerter(),
            ollama_url="http://localhost",
            gemma_model="gemma:test",
            ranking_tick_seconds=60,
            ranking_queue_batch=10,
            embedding_pregate_threshold=0.3,
            embed_client=embed_client,
            workspace_controller=controller,
            start=False,
        )
        try:
            drain_job = sched.get_job("turn_drain")
            assert drain_job is not None
            assert drain_job.trigger.interval.total_seconds() == settings.turn_drain_tick_seconds
            comp_job = sched.get_job("compaction_tick")
            assert comp_job is not None
            assert comp_job.trigger.interval.total_seconds() == settings.compaction_tick_seconds
        finally:
            sched.shutdown(wait=False)
    finally:
        engine.dispose()
```

- [ ] **Step 2: Register the ticks in `scheduler.py`**

Add after the existing `drafting_idle_sweep` job:

```python
    # Turn-drain tick. Uses the same app_loop dispatch pattern as
    # drafting_idle_sweep so the registry's loop-bound state isn't
    # corrupted by a worker-thread asyncio.run.
    def _turn_drain_tick() -> None:
        import asyncio

        from call_monitor.proposal_assist.turns.drain import drain_turn_queue_tick

        coro = drain_turn_queue_tick(
            session_factory=session_factory, controller=workspace_controller,
        )
        if app_loop is None:
            try:
                asyncio.run(coro)
            except Exception:
                _logger.exception("turn_drain tick failed")
            return
        fut = asyncio.run_coroutine_threadsafe(coro, app_loop)
        try:
            fut.result(timeout=300)
        except Exception:
            _logger.exception("turn_drain tick failed")

    sched.add_job(
        _turn_drain_tick,
        trigger=IntervalTrigger(seconds=settings.turn_drain_tick_seconds),
        id="turn_drain",
        max_instances=1,
        coalesce=True,
    )

    # Compaction tick. Synchronous (just DB + Anthropic HTTP call).
    def _compaction_tick() -> None:
        from call_monitor.proposal_assist.turns.compaction import run_compaction_tick

        def make_summarizer_client() -> AnthropicClient:
            return AnthropicClient(
                api_key=settings.anthropic_api_key,
                model=settings.proposal_assist_llm_model,
            )

        if not settings.anthropic_api_key:
            _logger.info("compaction tick skipped: ANTHROPIC_API_KEY not set")
            return

        try:
            run_compaction_tick(
                session_factory=session_factory,
                summarizer_client_factory=make_summarizer_client,
                settings=settings,
            )
        except Exception:
            _logger.exception("compaction tick failed")

    sched.add_job(
        _compaction_tick,
        trigger=IntervalTrigger(seconds=settings.compaction_tick_seconds),
        id="compaction_tick",
        max_instances=1,
        coalesce=True,
    )
```

`main.py` should already pass `workspace_controller=` and `app_loop=` from B3's Task 12. Verify; no changes needed if so.

- [ ] **Step 3: Run + lint + commit**

```bash
uv run pytest tests/proposal_assist/test_turns_scheduler_tick.py -v
uv run pytest -q
# expect everything green
uv run ruff format src/call_monitor/scheduler.py tests/proposal_assist/test_turns_scheduler_tick.py
uv run ruff check src tests
git add src/call_monitor/scheduler.py tests/proposal_assist/test_turns_scheduler_tick.py
git commit -m "feat(b4): scheduler ticks — turn_drain + compaction_tick"
```

- [ ] **Step 4: B4 milestone surface**

```bash
uv run pytest -q                       # expect ~730+ passing
uv run ruff check src tests
```

Surface to the user:

> B4 complete. The turn pipeline is end-to-end: submit_user_turn enqueues atomically, the drain tick picks one per workspace per `TURN_DRAIN_TICK_SECONDS`, the processor dispatches via WorkspaceController.send_turn, translates each StreamEvent into a chat_turns row, persists `TurnDoneEvent.session_id` on the workspace, writes the `cloud_turn_sent` audit event, and finalizes the turn_job. Compaction tick rolls un-summarized chat_turns into chat_summaries when over threshold. Daily token cap enforced at submit time.
>
> The B3 native-resume handoff is closed: controller.launch fetches workspaces.last_sdk_session_id and passes it via LaunchContext.resume_session_id.
>
> **Not yet wired (next slices):**
> - POST /workspaces/<id>/turns + SSE bridge → **B6**.
> - Tools (`edit_proposal`, etc.) inside the subprocess → **B5**.
> - Disclosure modal, per-turn banner, "you hit the cap" UX, admin override → **B6 + B8**.
> - Global kill switch (`DRAFTING_ENABLED`) → **B8**.

---

## Self-review notes (for the engineer running this plan)

1. **Tests vs production cadence.** The drain test invokes `drain_turn_queue_tick` directly. Production runs it via APScheduler. The test bypasses the scheduler — the scheduler tick test (Task 10) covers the registration + closure, not the inner work.

2. **One assistant chat_turn per StreamEvent.** This is a deliberate flattening — the SDK message shape (with multiple content blocks per message) is lost in chat_turns. Reasoning: we no longer need chat_turns to be SDK-replay-compatible (B3 native resume sidesteps this). One row per event is simpler for UI rendering (B6) and audit. If a future feature needs to reconstruct the SDK message boundaries, add an `sdk_message_id` column.

3. **`cloud_turn_sent` payload contains telemetry, not content.** Per spec line 343. Content lives in `chat_turns`.

4. **Compaction is best-effort.** A failed compaction logs and moves on; the chat_summaries row is just absent for one more tick. No retry queue. The next tick will try again. If compaction repeatedly fails for one workspace, admin will notice via log rate (no formal alerting yet — B8 territory).

5. **Token-cap fairness.** A workspace that goes over its cap mid-turn (e.g., the turn's output pushes it over) is NOT killed mid-flight — only PRE-enqueue is blocked. The currently-running turn finishes normally and is recorded. The user sees the cap rejection on their NEXT attempt. This matches the spec's "Exceeding returns a chat error" (the chat error is at submit time).

6. **`TurnJob.cancelled` status is unused in B4.** The spec defines `cancelled` for B6's "cancel before run" feature. B4 doesn't write `cancelled` anywhere — that's B6 (or admin tooling).

7. **Lock-stranding risk on drain failure.** If `process_turn_once` raises a non-Exception BaseException (e.g., CancelledError when the lifespan loop is shutting down), the turn_job stays in `status='running'` and the orphan sweep on next launch cleans it up. The processor's try/except catches BaseException so this case writes a "failed" status — but `_finalize_failed` itself could raise during shutdown. Worst case: orphan-sweep handles it on next launch. Acceptable.

8. **No SSE streaming yet.** B4 buffers each event as a row write. B6 will add SSE that bridges the `controller.send_turn` async generator to the browser AND triggers the row writes in parallel (or restructures so the processor itself becomes B6's SSE handler).

9. **Stockholm day boundary.** The cap window is "today in Europe/Stockholm" by spec; calendar-day boundary, not 24-hour rolling window. The check_daily_token_cap test exercises this with backdated rows.

10. **chat_summaries.tokens is approximate.** `len(text) // 4` is a rough token estimate. Good enough for "is the summary too big?" checks; B6 can refine if needed.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-20-base-spec-4-turn-queue-compaction.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, two-stage review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
