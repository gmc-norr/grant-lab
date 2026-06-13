# Proposal-Assist Turn-Outcome Recording & Error Hygiene (B1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every terminal turn failure/cancel a persistent, categorized, viewer-safe `system` chat row plus a consistent audit event (from all five writers), and make `post_turn` errors render inline as JSON without eating the typed message — with no connector/generator changes.

**Architecture:** A single new module `turns/outcomes.py` holds `record_terminal_outcome` (a workspace-scoped conditional `UPDATE … RETURNING` that flips status, clears the user-turn token pre-count, writes a viewer-safe `system` chat row when `emit_chat=True`, and writes the audit event) plus the pure `outcome_message` categorizer. The five terminal-failure/cancel writers (processor `_finalize_failed`, reaper, orphan sweep, cancel route, soft-delete) all route through it. A migration adds `'system'` to the `chat_turns.role` check. Two correctness fixes ride along: `submit.py` rechecks `soft_deleted_at` under its existing `FOR UPDATE` lock (closes the enqueue-after-delete race), and the `main.py` exception handler returns JSON for htmx partials regardless of `Accept` (so the converted `post_turn` `HTTPException`s reach app.js).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (`update().returning()`), PostgreSQL 16, Alembic, Jinja2, pytest + testcontainers Postgres (`pg_session_factory`).

**Spec:** [docs/superpowers/specs/2026-06-12-proposal-assist-turn-outcomes-design.md](../specs/2026-06-12-proposal-assist-turn-outcomes-design.md)

**Key codebase facts (verified):**
- `TurnJob` has **no** `tokens_in`/`tokens_out` columns — those go only into the `WorkspaceEvent.payload`. The "clear the pre-count" step clears `ChatTurn.tokens_in` on the linked **user-turn** row.
- `ChatTurn.content` is `JSONB`; `position` is sequence-backed (`nextval('chat_turns_position_seq')`) — never set it manually.
- `WorkspaceEvent` has no `kind` check constraint (free `Text`).
- The atomic claim in `processor.py` already uses `update(...).returning(...)`, so the `RETURNING` idiom is established.
- Tests run against an Alembic-migrated Postgres (`migrated_engine` → head `0046`); `pg_session_factory` truncates+reseeds between tests. Helpers: `make_workspace(s, owner=…, current_phase="spec")`, `make_chat_turn(...)`, `make_user(s)` in `tests/proposal_assist/conftest.py`.

---

### Task 1: Migration + model constraint — add `'system'` to `chat_turns.role`

**Files:**
- Create: `alembic/versions/0047_chat_turns_system_role.py`
- Modify: `src/call_manager/db/models.py:777-780` (the `ChatTurn` `ck_chat_turns_role` CheckConstraint)
- Test: `tests/proposal_assist/test_turn_outcomes.py`

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_turn_outcomes.py`:

```python
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import ChatTurn
from tests.proposal_assist.conftest import make_workspace


def test_system_role_chat_turn_inserts(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(
            ChatTurn(
                workspace_id=ws.id,
                role="system",
                content={"type": "turn_outcome", "outcome": "failed", "text": "x"},
            )
        )
        s.commit()
        row = s.scalar(select(ChatTurn).where(ChatTurn.workspace_id == ws.id))
        assert row.role == "system"


def test_unknown_role_still_rejected(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(ChatTurn(workspace_id=ws.id, role="bogus", content={"type": "text", "text": "x"}))
        with pytest.raises(IntegrityError):
            s.commit()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/proposal_assist/test_turn_outcomes.py::test_system_role_chat_turn_inserts -v`
Expected: FAIL — `IntegrityError` on commit (the DB still has the 3-value `ck_chat_turns_role`).

- [ ] **Step 3: Write the migration**

Create `alembic/versions/0047_chat_turns_system_role.py`:

```python
"""Add 'system' to chat_turns.role check constraint (turn-outcome rows).

Revision ID: 0047
Revises: 0046
Create Date: 2026-06-13
"""

from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_chat_turns_role", "chat_turns", type_="check")
    op.create_check_constraint(
        "ck_chat_turns_role",
        "chat_turns",
        "role IN ('user','assistant','tool','system')",
    )


def downgrade() -> None:
    # Forward-only in spirit: a downgrade fails if any 'system' rows exist.
    op.execute("ALTER TABLE chat_turns DROP CONSTRAINT IF EXISTS ck_chat_turns_role")
    op.create_check_constraint(
        "ck_chat_turns_role",
        "chat_turns",
        "role IN ('user','assistant','tool')",
    )
```

- [ ] **Step 4: Update the model constraint**

In `src/call_manager/db/models.py`, the `ChatTurn.__table_args__` CheckConstraint:

```python
        CheckConstraint(
            "role IN ('user','assistant','tool','system')",
            name="ck_chat_turns_role",
        ),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/proposal_assist/test_turn_outcomes.py -v`
Expected: PASS (both). The `migrated_engine` session fixture runs `alembic upgrade head`, which now includes `0047`.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/0047_chat_turns_system_role.py src/call_manager/db/models.py tests/proposal_assist/test_turn_outcomes.py
git commit -m "feat(proposal-assist): add 'system' chat_turns role for turn-outcome rows"
```

---

### Task 2: `outcome_message` — pure viewer-safe categorizer

**Files:**
- Create: `src/call_manager/proposal_assist/turns/outcomes.py`
- Test: `tests/proposal_assist/test_turn_outcomes.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_turn_outcomes.py`:

```python
from call_manager.proposal_assist.turns.outcomes import outcome_message


@pytest.mark.parametrize(
    "outcome, raw_error, expected",
    [
        ("cancelled", None, "This turn was cancelled before it started."),
        ("cancelled", "workspace soft-deleted", "This turn was cancelled before it started."),
        ("failed", "reaped: stuck in running > 600s (workspace wedged)",
         "This turn was stopped after running too long."),
        ("failed", "orphaned", "This turn was interrupted. Please try again."),
        ("failed", "sdk_turn_error: boom", "The drafting assistant couldn't finish this turn. Please try again."),
        ("failed", "processor_failure: ValueError: secret/path/leak", "The drafting assistant couldn't finish this turn. Please try again."),
    ],
)
def test_outcome_message_maps_safely(outcome, raw_error, expected):
    msg = outcome_message(outcome, raw_error)
    assert msg == expected
    if raw_error:
        # raw diagnostics must never leak into the viewer-visible string
        assert raw_error not in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/proposal_assist/test_turn_outcomes.py::test_outcome_message_maps_safely -v`
Expected: FAIL — `ModuleNotFoundError: call_manager.proposal_assist.turns.outcomes`.

- [ ] **Step 3: Create the module with `outcome_message`**

Create `src/call_manager/proposal_assist/turns/outcomes.py`:

```python
"""Centralized terminal-outcome recording for proposal-assist turns.

Every terminal failed/cancelled transition routes through
``record_terminal_outcome`` so the status flip, the viewer-safe ``system``
chat row, and the audit event stay consistent across all five writers
(processor finalize, reaper, orphan sweep, cancel route, soft-delete).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from call_manager.db.models import ChatTurn, TurnJob, WorkspaceEvent


def outcome_message(outcome: str, raw_error: str | None) -> str:
    """Map an outcome + raw error to a short, viewer-safe sentence.

    NEVER echoes ``raw_error`` (it can contain stack/path diagnostics).
    Phrased neutrally: the same row is shown to viewers who cannot act,
    and 'orphaned' can follow either a crash or a redeploy.
    """
    if outcome == "cancelled":
        return "This turn was cancelled before it started."
    if raw_error is not None and raw_error.startswith("reaped:"):
        return "This turn was stopped after running too long."
    if raw_error == "orphaned":
        return "This turn was interrupted. Please try again."
    return "The drafting assistant couldn't finish this turn. Please try again."
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/proposal_assist/test_turn_outcomes.py::test_outcome_message_maps_safely -v`
Expected: PASS (all 6 cases).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/turns/outcomes.py tests/proposal_assist/test_turn_outcomes.py
git commit -m "feat(proposal-assist): outcome_message viewer-safe categorizer"
```

---

### Task 3: `record_terminal_outcome` — the centralized writer

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/outcomes.py`
- Test: `tests/proposal_assist/test_turn_outcomes.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/test_turn_outcomes.py`:

```python
from datetime import UTC, datetime

from call_manager.db.models import TurnJob, WorkspaceEvent
from call_manager.proposal_assist.turns.outcomes import record_terminal_outcome
from tests.proposal_assist.conftest import make_chat_turn, make_user


def _running_job(s, ws, *, submitter, tokens_in=42):
    user_turn = make_chat_turn(s, ws, role="user", content={"type": "text", "text": "hi"}, tokens_in=tokens_in)
    job = TurnJob(
        workspace_id=ws.id,
        user_turn_id=user_turn.id,
        submitted_by_user_id=submitter.id,
        status="running",
        started_at=datetime.now(UTC),
    )
    s.add(job)
    s.flush()
    return job, user_turn


def test_records_failed_outcome_row_event_and_status(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        job, user_turn = _running_job(s, ws, submitter=owner)
        s.commit()
        ok = record_terminal_outcome(
            s,
            turn_job_id=job.id,
            workspace_id=ws.id,
            outcome="failed",
            expected_status="running",
            raw_error="sdk_turn_error: boom",
            tokens_in=10,
            tokens_out=5,
        )
        s.commit()
        assert ok is True
        j = s.get(TurnJob, job.id)
        assert j.status == "failed"
        assert j.completed_at is not None
        row = s.scalar(select(ChatTurn).where(ChatTurn.role == "system", ChatTurn.workspace_id == ws.id))
        assert row.content["type"] == "turn_outcome"
        assert row.content["outcome"] == "failed"
        assert row.content["text"] == "The drafting assistant couldn't finish this turn. Please try again."
        assert row.content["turn_job_id"] == str(job.id)
        assert row.content["user_turn_id"] == str(user_turn.id)
        assert "boom" not in row.content["text"]
        ev = s.scalar(select(WorkspaceEvent).where(WorkspaceEvent.kind == "cloud_turn_failed"))
        assert ev.payload["error"] == "sdk_turn_error: boom"
        assert ev.actor_id == owner.id
        assert s.get(ChatTurn, user_turn.id).tokens_in is None  # pre-count cleared


def test_expected_status_mismatch_returns_false_and_writes_nothing(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        job, _ = _running_job(s, ws, submitter=owner)
        job.status = "completed"  # already terminal
        s.commit()
        ok = record_terminal_outcome(
            s, turn_job_id=job.id, workspace_id=ws.id, outcome="failed",
            expected_status="running", raw_error="late",
        )
        s.commit()
        assert ok is False
        assert s.get(TurnJob, job.id).status == "completed"
        assert s.scalar(select(ChatTurn).where(ChatTurn.role == "system")) is None
        assert s.scalar(select(WorkspaceEvent).where(WorkspaceEvent.kind == "cloud_turn_failed")) is None


def test_wrong_workspace_returns_false(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        other = make_workspace(s, owner=owner)
        job, _ = _running_job(s, ws, submitter=owner)
        s.commit()
        ok = record_terminal_outcome(
            s, turn_job_id=job.id, workspace_id=other.id, outcome="failed",
            expected_status="running", raw_error="x",
        )
        s.commit()
        assert ok is False
        assert s.get(TurnJob, job.id).status == "running"


def test_emit_chat_false_writes_event_but_no_row(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        user_turn = make_chat_turn(s, ws, role="user", content={"type": "text", "text": "hi"})
        job = TurnJob(workspace_id=ws.id, user_turn_id=user_turn.id, submitted_by_user_id=owner.id, status="queued")
        s.add(job)
        s.commit()
        ok = record_terminal_outcome(
            s, turn_job_id=job.id, workspace_id=ws.id, outcome="cancelled",
            expected_status="queued", raw_error="workspace soft-deleted", emit_chat=False,
        )
        s.commit()
        assert ok is True
        assert s.get(TurnJob, job.id).status == "cancelled"
        assert s.scalar(select(ChatTurn).where(ChatTurn.role == "system")) is None
        assert s.scalar(select(WorkspaceEvent).where(WorkspaceEvent.kind == "cloud_turn_cancelled")) is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/proposal_assist/test_turn_outcomes.py -k record_ or emit_chat or expected_status or wrong_workspace -v`
Expected: FAIL — `ImportError: cannot import name 'record_terminal_outcome'`.

- [ ] **Step 3: Implement `record_terminal_outcome`**

Append to `src/call_manager/proposal_assist/turns/outcomes.py`:

```python
def record_terminal_outcome(
    session: Session,
    *,
    turn_job_id: uuid.UUID,
    workspace_id: uuid.UUID,
    outcome: str,            # 'failed' | 'cancelled'
    expected_status: str,    # 'running' | 'queued' — the conditional guard
    raw_error: str | None,   # stored in the audit event, NEVER in the chat row
    emit_chat: bool = True,  # False → audit event only (soft-delete: no surface)
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> bool:
    """Record a terminal failed/cancelled outcome in the caller's transaction.

    Returns True if this call performed the transition, False if the job was
    already terminal / in the wrong workspace (no double-finalize). The caller
    commits.
    """
    terminal = "failed" if outcome == "failed" else "cancelled"
    # Workspace-scoped conditional flip + atomic read-back of the authoritative
    # ids. Test the RETURNING result, NOT rowcount (psycopg3 reports rowcount
    # unreliably for UPDATE ... RETURNING).
    row = session.execute(
        update(TurnJob)
        .where(
            TurnJob.id == turn_job_id,
            TurnJob.status == expected_status,
            TurnJob.workspace_id == workspace_id,
        )
        .values(status=terminal, error=raw_error, completed_at=datetime.now(UTC))
        .returning(TurnJob.user_turn_id, TurnJob.submitted_by_user_id)
    ).first()
    if row is None:
        return False
    user_turn_id, submitted_by_user_id = row
    # Clear the submit-time tokens_in pre-count on the user-turn row so a
    # terminal turn doesn't keep burning daily-cap budget for input it never
    # actually consumed (mirrors the finalize/reaper behaviour).
    if user_turn_id is not None:
        user_turn = session.scalar(select(ChatTurn).where(ChatTurn.id == user_turn_id))
        if user_turn is not None:
            user_turn.tokens_in = None
    if emit_chat:
        session.add(
            ChatTurn(
                workspace_id=workspace_id,
                role="system",
                content={
                    "type": "turn_outcome",
                    "outcome": outcome,
                    "text": outcome_message(outcome, raw_error),
                    "turn_job_id": str(turn_job_id),
                    "user_turn_id": str(user_turn_id) if user_turn_id is not None else None,
                },
            )
        )
    kind = "cloud_turn_failed" if outcome == "failed" else "cloud_turn_cancelled"
    session.add(
        WorkspaceEvent(
            workspace_id=workspace_id,
            kind=kind,
            actor_id=submitted_by_user_id,
            payload={
                "turn_job_id": str(turn_job_id),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "error": raw_error,
            },
        )
    )
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/proposal_assist/test_turn_outcomes.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/turns/outcomes.py tests/proposal_assist/test_turn_outcomes.py
git commit -m "feat(proposal-assist): record_terminal_outcome centralized writer"
```

---

### Task 4: Render the outcome row — `chat_turn.html` + `.turn-outcome` CSS

**Files:**
- Modify: `src/call_manager/web/templates/proposal_assist/partials/chat_turn.html:33-44` (add a mapping branch)
- Modify: `src/call_manager/web/static/app.css` (add `.turn-outcome` near the `.callout` rules)
- Test: `tests/proposal_assist/test_turn_outcomes.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_turn_outcomes.py`:

```python
from call_manager.web.templates_env import get_templates  # see Step 3 note if path differs


def test_chat_turn_renders_turn_outcome_notice():
    # render the partial directly with a turn_outcome system row
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader("src/call_manager/web/templates"),
        autoescape=select_autoescape(["html"]),
    )
    tmpl = env.get_template("proposal_assist/partials/chat_turn.html")

    class _Turn:
        role = "system"
        workspace_id = "00000000-0000-0000-0000-000000000000"
        content = {
            "type": "turn_outcome",
            "outcome": "failed",
            "text": "The drafting assistant couldn't finish this turn. Please try again.",
        }

    html = tmpl.render(turn=_Turn(), chat_reference_map={})
    assert 'class="turn-outcome"' in html
    assert "couldn't finish this turn" in html
    assert "<pre>" not in html  # not the fallback branch
```

(If a shared Jinja env helper exists, the implementer may use it; the inline `Environment` above is self-contained and matches the autoescape-on default of the app.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/proposal_assist/test_turn_outcomes.py::test_chat_turn_renders_turn_outcome_notice -v`
Expected: FAIL — `'class="turn-outcome"'` not present (the `turn_outcome` mapping falls through to the `<pre>` fallback).

- [ ] **Step 3: Add the dispatch branch**

In `src/call_manager/web/templates/proposal_assist/partials/chat_turn.html`, after the `tool_result` mapping branch and **before** the `{%- elif c is sequence -%}` branch, insert:

```jinja
  {%- elif c is mapping and c.get("type") == "turn_outcome" -%}
    <p class="turn-outcome">{{ c.get("text", "") }}</p>
```

(So the full mapping dispatch reads: `text` → `tool_use` → `tool_result` → `turn_outcome` → then `sequence`. Autoescape stays on — no `| safe`.)

- [ ] **Step 4: Add the CSS rule**

In `src/call_manager/web/static/app.css`, near the `.callout` block, add:

```css
.turn-outcome {
  color: var(--cm-muted);
  font-style: italic;
  background: var(--cm-neutral-bg);
  border: 1px solid var(--cm-neutral-border);
  border-radius: 0.5rem;
  padding: 0.5rem 0.75rem;
  margin: 0.4rem 0;
  font-size: 0.9rem;
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/proposal_assist/test_turn_outcomes.py::test_chat_turn_renders_turn_outcome_notice -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/templates/proposal_assist/partials/chat_turn.html src/call_manager/web/static/app.css tests/proposal_assist/test_turn_outcomes.py
git commit -m "feat(proposal-assist): render turn_outcome system rows as a muted notice"
```

---

### Task 5: Processor `_finalize_failed` → thin wrapper over the helper

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/processor.py:429-483` (rewrite `_finalize_failed` body; add import)
- Test: `tests/proposal_assist/test_turn_outcomes.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_turn_outcomes.py`:

```python
from call_manager.proposal_assist.turns.processor import _finalize_failed


def test_finalize_failed_wrapper_writes_row_and_event(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        job, user_turn = _running_job(s, ws, submitter=owner)
        s.commit()
        wsid, jid, utid = ws.id, job.id, user_turn.id

    _finalize_failed(
        pg_session_factory,
        turn_job_id=jid,
        workspace_id=wsid,
        submitted_by_user_id=owner.id,
        error="sdk_turn_error: kaboom",
        tokens_in=3,
        tokens_out=1,
        user_turn_id=utid,
    )

    with pg_session_factory() as s:
        assert s.get(TurnJob, jid).status == "failed"
        row = s.scalar(select(ChatTurn).where(ChatTurn.role == "system", ChatTurn.workspace_id == wsid))
        assert row.content["outcome"] == "failed"
        assert s.scalar(select(WorkspaceEvent).where(WorkspaceEvent.kind == "cloud_turn_failed")) is not None
        assert s.get(ChatTurn, utid).tokens_in is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/proposal_assist/test_turn_outcomes.py::test_finalize_failed_wrapper_writes_row_and_event -v`
Expected: FAIL — no `system` `turn_outcome` row exists (the current `_finalize_failed` writes only the event).

- [ ] **Step 3: Add the import**

In `src/call_manager/proposal_assist/turns/processor.py`, with the other `call_manager.proposal_assist.turns.*` imports near the top, add:

```python
from call_manager.proposal_assist.turns.outcomes import record_terminal_outcome
```

- [ ] **Step 4: Rewrite `_finalize_failed` body (keep the signature)**

Replace the body of `_finalize_failed` (lines ~429-483) with:

```python
def _finalize_failed(
    session_factory: sessionmaker,
    *,
    turn_job_id: uuid.UUID,
    workspace_id: uuid.UUID,
    submitted_by_user_id: int | None,
    error: str,
    tokens_in: int,
    tokens_out: int,
    user_turn_id: uuid.UUID | None = None,
) -> None:
    # Thin wrapper: the helper performs the conditional running→failed flip,
    # clears the user-turn token pre-count, writes the viewer-safe system row,
    # and writes the cloud_turn_failed event — all in one transaction. It reads
    # user_turn_id / submitted_by_user_id from the row via RETURNING, so the
    # passed-in copies are no longer threaded (kept in the signature so callers
    # are unchanged). A no-op (already terminal/reaped) returns False and writes
    # nothing — the prior writer already cleared the pre-count and emitted its
    # own event.
    with session_factory() as s:
        record_terminal_outcome(
            s,
            turn_job_id=turn_job_id,
            workspace_id=workspace_id,
            outcome="failed",
            expected_status="running",
            raw_error=error,
            emit_chat=True,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        s.commit()
```

(Leave `_finalize_success` and the four call sites at lines 170/193/288/300 unchanged — they still pass `error=`, `user_turn_id=`, etc.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/proposal_assist/test_turn_outcomes.py -v && pytest tests/proposal_assist/test_turn_processor*.py -v`
Expected: PASS. If an existing processor test asserts an **exact** `chat_turns` row count for a failed turn, update it to expect the additional `system` row (the `cloud_turn_failed` event assertions are unchanged).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/turns/processor.py tests/proposal_assist/test_turn_outcomes.py
git commit -m "refactor(proposal-assist): _finalize_failed routes through record_terminal_outcome"
```

---

### Task 6: Reaper → per-job helper loop

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/reaper.py:42-99` (`reap_stuck_turns` body; add import)
- Test: `tests/proposal_assist/test_turn_reaper.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_turn_reaper.py` (imports `select`, `TurnJob`, `make_workspace`, `make_chat_turn`, `make_user` as needed):

```python
def test_reaper_writes_outcome_row_and_event(pg_session_factory):
    from sqlalchemy import select

    from call_manager.db.models import ChatTurn, TurnJob, WorkspaceEvent
    from tests.proposal_assist.conftest import make_chat_turn, make_user, make_workspace

    old = datetime.now(UTC) - timedelta(seconds=DEFAULT_REAP_TTL_SECONDS + 60)
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        ut = make_chat_turn(s, ws, role="user", content={"type": "text", "text": "hi"}, tokens_in=99)
        s.add(TurnJob(workspace_id=ws.id, user_turn_id=ut.id, submitted_by_user_id=owner.id,
                      status="running", started_at=old))
        s.commit()
        wsid, utid = ws.id, ut.id

    assert reap_stuck_turns(session_factory=pg_session_factory) == 1

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
        assert job.status == "failed"
        row = s.scalar(select(ChatTurn).where(ChatTurn.role == "system", ChatTurn.workspace_id == wsid))
        assert row.content["text"] == "This turn was stopped after running too long."
        assert s.scalar(select(WorkspaceEvent).where(WorkspaceEvent.kind == "cloud_turn_failed")) is not None
        assert s.get(ChatTurn, utid).tokens_in is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/proposal_assist/test_turn_reaper.py::test_reaper_writes_outcome_row_and_event -v`
Expected: FAIL — no `system` row / no `cloud_turn_failed` event (current reaper does a bulk UPDATE only).

- [ ] **Step 3: Rewrite `reap_stuck_turns` to loop through the helper**

In `src/call_manager/proposal_assist/turns/reaper.py`, add the import near the top:

```python
from call_manager.proposal_assist.turns.outcomes import record_terminal_outcome
```

Replace the `with session_factory() as s:` block body (the select + bulk update + token clear + commit) with:

```python
    with session_factory() as s:
        # Per-job so each reaped turn gets a viewer-safe system row + audit
        # event (the helper also clears the user-turn token pre-count). Stuck
        # turns are normally zero, so per-job cost is irrelevant.
        stuck = s.execute(
            select(TurnJob.id, TurnJob.workspace_id).where(
                TurnJob.status == "running",
                TurnJob.started_at.is_not(None),
                TurnJob.started_at < cutoff,
            )
        ).all()
        reaped = 0
        for row in stuck:
            if record_terminal_outcome(
                s,
                turn_job_id=row.id,
                workspace_id=row.workspace_id,
                outcome="failed",
                expected_status="running",
                raw_error=f"reaped: stuck in running > {ttl_seconds}s (workspace wedged)",
                emit_chat=True,
            ):
                reaped += 1
        s.commit()
```

(The trailing `if reaped: logger.warning(...)` and `return reaped` stay. The reaper now returns the count of **successful** transitions. Drop the now-unused separate `ChatTurn` token-clear UPDATE — the helper does it per row.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/proposal_assist/test_turn_reaper.py -v`
Expected: PASS (the new test and the existing `test_reaper_marks_old_running_turn_as_failed`).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/turns/reaper.py tests/proposal_assist/test_turn_reaper.py
git commit -m "refactor(proposal-assist): reaper routes through record_terminal_outcome (+audit event)"
```

---

### Task 7: Orphan sweep → per-job helper loop

**Files:**
- Modify: `src/call_manager/proposal_assist/drafting/orphan_sweep.py:19-37` (add import; loop)
- Test: `tests/proposal_assist/test_drafting_orphan_sweep.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_drafting_orphan_sweep.py`:

```python
def test_orphan_sweep_writes_outcome_row_and_event(pg_session_factory):
    from sqlalchemy import select

    from call_manager.db.models import ChatTurn, TurnJob, WorkspaceEvent
    from tests.proposal_assist.conftest import make_chat_turn, make_user, make_workspace

    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        ut = make_chat_turn(s, ws, role="user", content={"type": "text", "text": "hi"})
        s.add(TurnJob(workspace_id=ws.id, user_turn_id=ut.id, submitted_by_user_id=owner.id,
                      status="running", started_at=datetime.now(UTC)))
        s.commit()
        wsid = ws.id

        swept = sweep_orphaned_turn_jobs(s, wsid)
        s.commit()

        assert swept == 1
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
        assert job.status == "failed"
        row = s.scalar(select(ChatTurn).where(ChatTurn.role == "system", ChatTurn.workspace_id == wsid))
        assert row.content["text"] == "This turn was interrupted. Please try again."
        assert s.scalar(select(WorkspaceEvent).where(WorkspaceEvent.kind == "cloud_turn_failed")) is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/proposal_assist/test_drafting_orphan_sweep.py::test_orphan_sweep_writes_outcome_row_and_event -v`
Expected: FAIL — no system row / no event.

- [ ] **Step 3: Rewrite `sweep_orphaned_turn_jobs`**

Replace `src/call_manager/proposal_assist/drafting/orphan_sweep.py`'s function with (keep the signature; the caller still commits):

```python
import uuid
from datetime import UTC, datetime  # noqa: F401  (UTC/datetime may already be imported)

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import TurnJob
from call_manager.proposal_assist.turns.outcomes import record_terminal_outcome


def sweep_orphaned_turn_jobs(session: Session, workspace_id: uuid.UUID) -> int:
    """Mark any 'running' turn_jobs for this workspace 'failed' with
    error='orphaned', writing a viewer-safe system row + audit event per job.
    Returns the count of rows actually transitioned. Caller commits.
    Workspace-scoped — does not touch other workspaces.
    """
    rows = session.execute(
        select(TurnJob.id).where(
            TurnJob.workspace_id == workspace_id,
            TurnJob.status == "running",
        )
    ).all()
    swept = 0
    for row in rows:
        if record_terminal_outcome(
            session,
            turn_job_id=row.id,
            workspace_id=workspace_id,
            outcome="failed",
            expected_status="running",
            raw_error="orphaned",
            emit_chat=True,
        ):
            swept += 1
    return swept
```

(Reconcile imports with whatever is already at the top of the file — do not duplicate.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/proposal_assist/test_drafting_orphan_sweep.py -v`
Expected: PASS (new + existing `test_sweep_marks_running_turn_jobs_as_failed`).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/drafting/orphan_sweep.py tests/proposal_assist/test_drafting_orphan_sweep.py
git commit -m "refactor(proposal-assist): orphan sweep routes through record_terminal_outcome (+audit event)"
```

---

### Task 8: Cancel route → helper

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py:1376-1427` (`cancel_turn`; add import)
- Test: `tests/proposal_assist/test_web_workspaces_cancel.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_web_workspaces_cancel.py`:

```python
def test_cancel_writes_system_outcome_row(client_authed_owner, pg_session_factory, current_user):
    from datetime import UTC, datetime

    from sqlalchemy import select

    from call_manager.db.models import ChatTurn, TurnJob, WorkspaceEvent
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(ut)
        s.flush()
        job = TurnJob(workspace_id=ws.id, user_turn_id=ut.id, submitted_by_user_id=current_user.id,
                      status="queued", queued_at=datetime.now(UTC))
        s.add(job)
        s.commit()
        wsid, jid = ws.id, job.id

    resp = client_authed_owner.post(f"/workspaces/{wsid}/turns/{jid}/cancel")
    assert resp.status_code == 200

    with pg_session_factory() as s:
        assert s.get(TurnJob, jid).status == "cancelled"
        row = s.scalar(select(ChatTurn).where(ChatTurn.role == "system", ChatTurn.workspace_id == wsid))
        assert row.content["outcome"] == "cancelled"
        assert row.content["text"] == "This turn was cancelled before it started."
        assert s.scalar(select(WorkspaceEvent).where(WorkspaceEvent.kind == "cloud_turn_cancelled")) is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/proposal_assist/test_web_workspaces_cancel.py::test_cancel_writes_system_outcome_row -v`
Expected: FAIL — no `system` row (current cancel writes only the event).

- [ ] **Step 3: Route `cancel_turn` through the helper**

In `src/call_manager/proposal_assist/web/workspaces.py`, add the import near the other `turns.*` imports:

```python
from call_manager.proposal_assist.turns.outcomes import record_terminal_outcome
```

Replace the body of `cancel_turn` after `_require_owner(...)` (the `result = session.execute(update(TurnJob)...)` block through the two `HTMLResponse` returns) with:

```python
    did = record_terminal_outcome(
        session,
        turn_job_id=turn_job_id,
        workspace_id=ws.id,
        outcome="cancelled",
        expected_status="queued",
        raw_error=None,
        emit_chat=True,
    )
    session.commit()
    if did:
        return HTMLResponse("<p>cancelled</p>")
    return HTMLResponse("<p>too late — already started or finished</p>")
```

(The route's response contract is unchanged for B1 — the small status-fragment + Cancel button is B2. Note: the audit event's `actor_id` is now the turn's `submitted_by_user_id` (read via RETURNING) rather than `user.id`; for an owner-submitted turn these are the same user, and only the owner can cancel.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/proposal_assist/test_web_workspaces_cancel.py -v`
Expected: PASS (new + existing `test_cancel_queued_turn_marks_it_cancelled`).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/web/workspaces.py tests/proposal_assist/test_web_workspaces_cancel.py
git commit -m "refactor(proposal-assist): cancel route routes through record_terminal_outcome (system row)"
```

---

### Task 9: Soft-delete → helper loop (1C: cancelled + audit-only)

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py:1298-1310` (the queued-turn bulk update inside `soft_delete`)
- Test: `tests/proposal_assist/test_web_workspaces_delete.py` (append; create file if absent)

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_web_workspaces_delete.py` (create the file with the standard imports if it doesn't exist):

```python
def test_soft_delete_cancels_queued_turn_audit_only(client_authed_owner, pg_session_factory, current_user):
    from datetime import UTC, datetime

    from sqlalchemy import select

    from call_manager.db.models import ChatTurn, TurnJob, Workspace, WorkspaceEvent
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(ut)
        s.flush()
        job = TurnJob(workspace_id=ws.id, user_turn_id=ut.id, submitted_by_user_id=current_user.id,
                      status="queued", queued_at=datetime.now(UTC))
        s.add(job)
        s.commit()
        wsid, jid = ws.id, job.id

    resp = client_authed_owner.post(f"/workspaces/{wsid}/delete")
    assert resp.status_code in (200, 303)

    with pg_session_factory() as s:
        assert s.get(Workspace, wsid).soft_deleted_at is not None
        assert s.get(TurnJob, jid).status == "cancelled"  # not 'failed'
        # audit event closes the cloud_turn_sent chain, but NO viewer row
        assert s.scalar(select(WorkspaceEvent).where(WorkspaceEvent.kind == "cloud_turn_cancelled")) is not None
        assert s.scalar(select(ChatTurn).where(ChatTurn.role == "system")) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/proposal_assist/test_web_workspaces_delete.py::test_soft_delete_cancels_queued_turn_audit_only -v`
Expected: FAIL — current soft-delete sets status `'failed'` (not `'cancelled'`) and writes no `cloud_turn_cancelled` event.

- [ ] **Step 3: Route queued turns through the helper (emit_chat=False)**

In `soft_delete`, replace the `session.execute(update(TurnJob).where(...status=='queued').values(status='failed', ...))` block with:

```python
    # Cancel any QUEUED turns so a deletion-race turn can't dispatch. Route
    # through the helper for audit parity (closes each cloud_turn_sent chain),
    # but emit_chat=False — the workspace is 404 the instant it's soft-deleted,
    # so a viewer-safe row would be dead-on-arrival. 'cancelled' (owner-
    # initiated, never started), not 'failed'.
    queued = session.execute(
        select(TurnJob.id).where(
            TurnJob.workspace_id == workspace_id,
            TurnJob.status == "queued",
        )
    ).all()
    for row in queued:
        record_terminal_outcome(
            session,
            turn_job_id=row.id,
            workspace_id=workspace_id,
            outcome="cancelled",
            expected_status="queued",
            raw_error="workspace soft-deleted",
            emit_chat=False,
        )
    session.commit()
```

(`ws.soft_deleted_at = datetime.now(UTC)` stays above this; `session.commit()` then persists both the soft-delete and the cancellations in one transaction. The `record_terminal_outcome` import was added in Task 8.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/proposal_assist/test_web_workspaces_delete.py -v`
Expected: PASS. If an existing soft-delete test asserts the queued turn becomes `'failed'`, update it to `'cancelled'`.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/web/workspaces.py tests/proposal_assist/test_web_workspaces_delete.py
git commit -m "feat(proposal-assist): soft-delete cancels queued turns via helper (audit-only, cancelled)"
```

---

### Task 10: Close the enqueue-after-delete race — `submit.py`

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/submit.py:243-247` (the `FOR UPDATE` lock query)
- Test: `tests/proposal_assist/test_turn_submit.py` (append; create file if absent)

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_turn_submit.py`:

```python
def test_submit_rejected_on_soft_deleted_workspace(pg_session_factory):
    """A submit that races a soft_delete (workspace already soft-deleted by the
    time submit acquires the FOR UPDATE lock) must be rejected — no TurnJob,
    no cloud_turn_sent — so the drain can never orphan a queued turn."""
    from datetime import UTC, datetime

    import pytest
    from sqlalchemy import select

    from call_manager.config import Settings
    from call_manager.db.models import TurnJob, WorkspaceEvent
    from call_manager.proposal_assist.turns.submit import submit_user_turn
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        ws.soft_deleted_at = datetime.now(UTC)  # already soft-deleted
        s.commit()
        wsid = ws.id

    with pg_session_factory() as s:
        with pytest.raises(ValueError):
            submit_user_turn(
                session=s,
                workspace_id=wsid,
                content_text="hi",
                submitted_by_user_id=owner.id,
                settings=Settings(),
            )
        s.rollback()

    with pg_session_factory() as s:
        assert s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid)) is None
        assert s.scalar(select(WorkspaceEvent).where(WorkspaceEvent.kind == "cloud_turn_sent")) is None
```

(If `Settings()` needs required env in this repo's test setup, mirror however the existing `test_turn_submit.py` / cap tests construct settings — e.g. a `settings` fixture or `monkeypatch.setenv` block.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/proposal_assist/test_turn_submit.py::test_submit_rejected_on_soft_deleted_workspace -v`
Expected: FAIL — `submit_user_turn` enqueues a turn (the lock query ignores `soft_deleted_at`), so no `ValueError` is raised.

- [ ] **Step 3: Add the `soft_deleted_at` predicate under the lock**

In `src/call_manager/proposal_assist/turns/submit.py`, change the lock query:

```python
    # Lock the workspace row for the duration of this transaction AND re-check
    # soft_deleted_at under the lock. submit and soft_delete serialize on this
    # row (submit's SELECT FOR UPDATE conflicts with soft_delete's UPDATE), so
    # re-reading here catches the post-deletion ordering: a None result means
    # the workspace is missing OR soft-deleted → reject, so no turn can be
    # enqueued (and orphan its cloud_turn_sent) after deletion.
    locked = session.execute(
        select(Workspace.id)
        .where(Workspace.id == workspace_id, Workspace.soft_deleted_at.is_(None))
        .with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        raise ValueError(f"workspace {workspace_id} does not exist or was deleted")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/proposal_assist/test_turn_submit.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/turns/submit.py tests/proposal_assist/test_turn_submit.py
git commit -m "fix(proposal-assist): reject turn enqueue on soft-deleted workspace (close race)"
```

---

### Task 11: Exception handler — htmx partials get JSON regardless of `Accept`

**Files:**
- Modify: `src/call_manager/main.py:294-330` (the `_redirect_html_401` HTTPException handler)
- Test: `tests/proposal_assist/test_error_handler_htmx.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_error_handler_htmx.py`:

```python
def test_htmx_partial_gets_json_detail_even_with_accept_html(monkeypatch, pg_url, pg_session_factory):
    """A 409 raised by post_turn during Phase-0 setup must reach app.js as JSON
    {"detail": ...} for an htmx partial request, even when the browser also
    sends Accept: text/html (so the global htmx:responseError handler renders
    the detail inline rather than swapping an HTML error page)."""
    from fastapi.testclient import TestClient

    from call_manager.auth.session_cookie import SessionSigner
    from call_manager.db.models import User, Workspace
    from call_manager.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()

    with pg_session_factory() as s:
        owner = User(email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T", current_phase="setup")  # Phase-0 → 409 gate
        s.add(ws)
        s.commit()
        uid, wsid = owner.id, ws.id

    client = TestClient(app)
    client.__enter__()
    try:
        client.cookies.set("session", app.state.session_signer.make(user_id=uid))
        headers = {"HX-Request": "true", "Accept": "text/html"}
        resp = client.post(f"/workspaces/{wsid}/turns", data={"content": "hi"}, headers=headers)
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 409
    assert resp.headers["content-type"].startswith("application/json")
    assert isinstance(resp.json()["detail"], str)


def test_plain_browser_navigation_still_gets_html_error_page(monkeypatch, pg_url, pg_session_factory):
    from fastapi.testclient import TestClient

    from call_manager.db.models import User, Workspace
    from call_manager.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()

    with pg_session_factory() as s:
        owner = User(email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T", current_phase="setup")
        s.add(ws)
        s.commit()
        uid, wsid = owner.id, ws.id

    client = TestClient(app)
    client.__enter__()
    try:
        client.cookies.set("session", app.state.session_signer.make(user_id=uid))
        resp = client.post(f"/workspaces/{wsid}/turns", data={"content": "hi"},
                           headers={"Accept": "text/html"})  # no HX-Request
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 409
    assert resp.headers["content-type"].startswith("text/html")
```

- [ ] **Step 2: Run tests to verify the first fails**

Run: `pytest tests/proposal_assist/test_error_handler_htmx.py -v`
Expected: `test_htmx_partial_gets_json_detail_even_with_accept_html` FAILS (handler returns the HTML `error.html` page because `Accept: text/html` and 409 ∈ `_HTML_ERROR_STATUSES`); the second test PASSES already.

- [ ] **Step 3: Gate the HTML-error branch on "not an htmx partial"**

In `src/call_manager/main.py`, inside `_redirect_html_401`, replace the `if wants_html and exc.status_code in _HTML_ERROR_STATUSES:` block's condition so htmx partials are excluded:

```python
        wants_html = "text/html" in request.headers.get("accept", "")
        # An htmx PARTIAL request (HX-Request, not a full-page hx-boost
        # navigation) must always get JSON {"detail": ...} so the global
        # htmx:responseError handler can render it inline — never a styled
        # HTML page swapped into a fragment target. Full-page navigations
        # (no HX-Request, or boosted) keep the styled error page.
        hx_partial = (
            request.headers.get("hx-request") == "true"
            and request.headers.get("hx-boosted") != "true"
        )
        if exc.status_code == 401 and wants_html:
            # ... (unchanged 401 redirect block) ...
            ...
        if wants_html and not hx_partial and exc.status_code in _HTML_ERROR_STATUSES:
            return app.state.templates.TemplateResponse(
                request=request,
                name="error.html",
                context={"status_code": exc.status_code, "message": exc.detail},
                status_code=exc.status_code,
                headers=getattr(exc, "headers", None) or None,
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None) or {},
        )
```

(Leave the 401 redirect branch exactly as-is — auth redirect behaviour is out of scope. Only the `_HTML_ERROR_STATUSES` branch gains `and not hx_partial`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/proposal_assist/test_error_handler_htmx.py -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/main.py tests/proposal_assist/test_error_handler_htmx.py
git commit -m "fix(web): htmx partial requests get JSON error detail regardless of Accept"
```

---

### Task 12: `post_turn` error branches → `HTTPException` + composer reset gate

**Files:**
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (the `post_turn` error branches at ~760, ~780, ~835/844/848/854/859/883/915, ~942/953/963)
- Modify: `src/call_manager/web/templates/proposal_assist/partials/workspace_main.html` (composer `hx-on::after-request`)
- Test: `tests/proposal_assist/test_web_workspaces_turn_errors.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_web_workspaces_turn_errors.py`:

```python
def _setup_ready_ws(s, current_user):
    from tests.proposal_assist.conftest import make_workspace
    return make_workspace(s, owner=current_user, current_phase="spec", ack_disclosure=True)


def test_disclosure_gate_returns_json_403(client_authed_owner, pg_session_factory, current_user):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, current_phase="spec")  # NO ack
        s.commit()
        wsid = ws.id
    resp = client_authed_owner.post(f"/workspaces/{wsid}/turns", data={"content": "hi"},
                                    headers={"HX-Request": "true"})
    assert resp.status_code == 403
    assert "disclosure" in resp.json()["detail"].lower()


def test_too_many_inclusions_returns_json_400(client_authed_owner, pg_session_factory, current_user):
    with pg_session_factory() as s:
        ws = _setup_ready_ws(s, current_user)
        s.commit()
        wsid = ws.id
    data = [("content", "hi")] + [("reference_ids", str(__import__("uuid").uuid4())) for _ in range(6)]
    resp = client_authed_owner.post(f"/workspaces/{wsid}/turns", data=data,
                                    headers={"HX-Request": "true"})
    assert resp.status_code == 400
    assert "too many inclusions" in resp.json()["detail"].lower()
```

(Use whatever `client_authed_owner` / `current_user` fixtures the existing `test_web_workspaces_*` tests use. The disclosure test relies on `post_turn`'s 403 firing before the kill-switch/cap branches.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/proposal_assist/test_web_workspaces_turn_errors.py -v`
Expected: FAIL — current branches return `HTMLResponse`, so `resp.json()` raises / `content-type` is `text/html`.

- [ ] **Step 3: Convert each error branch to `raise HTTPException` (cleanup before raise)**

In `post_turn`, convert every user-facing error branch. Keep each branch's existing `session.rollback()` + `_rollback_created_uploads()` **before** the `raise`. Drop the `_html.escape(...)` wrappers (JSON `detail` is rendered with `textContent`, so no HTML-escaping needed). Specifically:

Kill-switch 503:
```python
    if not settings.drafting_enabled:
        raise HTTPException(
            status_code=503,
            detail="Drafting is currently disabled by an administrator. Please try again later.",
        )
```

Disclosure 403:
```python
    if prior_ack is None:
        raise HTTPException(
            status_code=403,
            detail="Please acknowledge the data-disclosure notice before sending a message to the drafting assistant.",
        )
```

Each upload-validation branch (unsupported MIME, `_read_capped`, empty upload, `_probe_image_format`, content-type mismatch, storage write, inclusions cap) — example for unsupported MIME:
```python
        if content_type not in _ALLOWED_MIMES:
            session.rollback()
            _rollback_created_uploads()
            raise HTTPException(status_code=400, detail=f"Unsupported content_type: {content_type!r}")
```
`_read_capped` / `_probe_image_format` HTTPException re-raises:
```python
        except HTTPException as e:
            session.rollback()
            _rollback_created_uploads()
            raise HTTPException(status_code=e.status_code, detail=str(e.detail)) from e
```
Empty upload: `raise HTTPException(status_code=400, detail="empty upload")`.
Content-type mismatch: `raise HTTPException(status_code=400, detail=f"content_type {content_type!r} does not match the actual image format {detected_mime!r}")`.
Storage write: `raise HTTPException(status_code=500, detail="storage write failed")`.
Inclusions cap: `raise HTTPException(status_code=400, detail=f"Too many inclusions: {all_inclusions} > {MAX_TURN_INCLUSIONS} max per turn.")`.

The `submit_user_turn` exception handlers (`CommitMutexTimeout` already raises 503 — leave it):
```python
    except GlobalTokenCapExceeded as e:
        session.rollback()
        _rollback_created_uploads()
        raise HTTPException(
            status_code=429,
            detail=(
                f"Global org-wide daily token cap exceeded: {e.reason}. "
                f"Used {e.used_in} input / {e.used_out} output today; cap is {e.cap_in} / {e.cap_out}. "
                "Try again after midnight Europe/Stockholm or ask an admin to raise the cap."
            ),
            headers={"X-Token-Cap": "global"},
        ) from e
    except TokenCapExceeded as e:
        session.rollback()
        _rollback_created_uploads()
        raise HTTPException(
            status_code=429,
            detail=(
                f"Workspace daily token cap exceeded: {e.reason}. "
                f"Used {e.used_in} input / {e.used_out} output today; cap is {e.cap_in} / {e.cap_out}. "
                "Try again after midnight Europe/Stockholm or ask an admin to raise the cap."
            ),
        ) from e
    except ValueError as e:
        session.rollback()
        _rollback_created_uploads()
        raise HTTPException(status_code=400, detail=str(e)) from e
```

(`HTTPException` is already imported in `workspaces.py`. The global-cap `headers={"X-Token-Cap": "global"}` is preserved and the detail still contains "Global" — `test_global_cap.py` stays green because the handler's JSON branch forwards `exc.headers` and `"global" in resp.text.lower()` still holds.)

- [ ] **Step 4: Gate the composer reset on success**

In `src/call_manager/web/templates/proposal_assist/partials/workspace_main.html`, find the composer form attribute `hx-on::after-request="this.reset()"` and change it to:

```html
hx-on::after-request="if (event.detail.successful) this.reset()"
```

- [ ] **Step 5: Run the new tests + the cap test**

Run: `pytest tests/proposal_assist/test_web_workspaces_turn_errors.py tests/proposal_assist/test_global_cap.py -v`
Expected: PASS.

- [ ] **Step 6: Update any existing tests that asserted old HTML error bodies**

Run: `git grep -nE 'status_code == (400|403|409|413|429|500|503)' tests/proposal_assist | git grep -n 'content' || true`
Then run the turn-related suite and fix assertions: any test asserting `b"..." in resp.content` for a `post_turn` error body should assert `resp.json()["detail"]` instead.

Run: `pytest tests/proposal_assist/test_web_workspaces_turns.py tests/proposal_assist/test_web_workspaces_post_turn*.py -v`
Expected: PASS after updates. (Convert `<p>...</p>` body assertions to `resp.json()["detail"]`.)

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/proposal_assist/web/workspaces.py src/call_manager/web/templates/proposal_assist/partials/workspace_main.html tests/proposal_assist/
git commit -m "fix(proposal-assist): post_turn errors raise HTTPException (JSON detail); composer reset gated on success"
```

---

### Task 13: Full-suite verification + lint

**Files:** none (verification only)

- [ ] **Step 1: Lint**

Run: `uv run ruff check src/call_manager/proposal_assist/turns/outcomes.py src/call_manager/proposal_assist/turns/reaper.py src/call_manager/proposal_assist/drafting/orphan_sweep.py src/call_manager/proposal_assist/turns/submit.py src/call_manager/proposal_assist/web/workspaces.py src/call_manager/proposal_assist/turns/processor.py src/call_manager/main.py`
Expected: no errors. Fix any (notably unused imports left behind in `reaper.py` / `orphan_sweep.py` after dropping the bulk-update code).

- [ ] **Step 2: Run the proposal-assist suite (capture the real summary)**

Run: `uv run pytest tests/proposal_assist/ -q`
Expected: all pass. (Do **not** pipe through `tail` — that reports tail's exit code. If it fails, read the summary directly.) Known non-regression flakiness: in-process APScheduler threads racing testcontainer teardown can emit "Event loop is closed" noise and occasionally flake `test_editing_e2e`; re-run the specific test to confirm it's environmental, not a regression.

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass. Fix any fallout from the `HTTPException`/`'system'`-role/`cancelled`-status changes by updating the affected assertions (not the production behaviour).

- [ ] **Step 4: Final commit (if Step 1-3 required fixes)**

```bash
git add -A
git commit -m "test(proposal-assist): align existing tests with B1 outcome/error changes"
```

---

## Self-Review

**Spec coverage:**
- Component A (`record_terminal_outcome`, workspace-scoped `RETURNING`, `emit_chat`) → Task 3. ✅
- Component B (`outcome_message`, neutral copy) → Task 2. ✅
- Component C (migration + model constraint) → Task 1. ✅
- Component D (`chat_turn.html` `turn_outcome` branch + `.turn-outcome` CSS) → Task 4. ✅
- Component E (five writers): processor → Task 5; reaper → Task 6; orphan → Task 7; cancel → Task 8; soft-delete (1C) → Task 9. ✅
- Component J (`post_turn` → `HTTPException`, cleanup-before-raise, global-cap header, composer reset gate) → Task 12. ✅
- Component K (submit `soft_deleted_at` recheck) → Task 10. ✅
- Component L (exception handler HX-Request precedence) → Task 11. ✅
- Stable attribution (`turn_job_id`/`user_turn_id` in content) → Task 3. ✅
- Reaper/orphan return successful-transition counts → Tasks 6/7. ✅

**Type/name consistency:** `record_terminal_outcome(session, *, turn_job_id, workspace_id, outcome, expected_status, raw_error, emit_chat=True, tokens_in=0, tokens_out=0) -> bool` is used identically in Tasks 3/5/6/7/8/9. `outcome_message(outcome, raw_error)` matches Tasks 2/3. Content shape `{"type":"turn_outcome","outcome","text","turn_job_id","user_turn_id"}` matches Tasks 3/4/8.

**Placeholder scan:** none — every code step shows the actual code; the only "find/replace" steps (composer `this.reset()`, existing-test fixups) name the exact string/transform.

**Note for the implementer:** `TurnJob` has no `tokens_in`/`tokens_out` columns — `tokens_in`/`tokens_out` flow only into `WorkspaceEvent.payload`; the "clear pre-count" always targets the linked **user-turn** `ChatTurn`. Never set `ChatTurn.position` manually (sequence-backed).
