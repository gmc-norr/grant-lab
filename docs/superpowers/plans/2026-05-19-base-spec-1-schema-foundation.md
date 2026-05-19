# Base Spec B1 — Schema Foundation + Workspace Creation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land all 9 base-spec tables (`workspaces`, `workspace_viewers`, `workspace_events`, `chat_turns`, `turn_jobs`, `chat_summaries`, `artifact_versions`, `attachments`, `attachment_jobs`) with SQLAlchemy models, plus a `create_workspace()` service function that enqueues a Phase 0 setup job (Slice A's `workspace_setup_jobs`) and writes a `created` audit event. End state: Slice A Part 2's Phase 0 worker can drain real setup jobs created by this function.

**Architecture:** Pure data layer. Nine migrations (one per table, following Slice A's one-table-per-migration convention) + nine models in the existing single-file `src/call_monitor/db/models.py`. The service function lives in a new top-level module `src/call_monitor/workspaces.py` — it composes the new `Workspace` model with Slice A's `WorkspaceSetupJob` to provide a single "create workspace and queue Phase 0" entrypoint. No web endpoints, no chat machinery, no agent SDK — those are deferred to B3/B4/B5/B6.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x + Alembic, PostgreSQL 16 with pgvector, pytest + testcontainers (real Postgres in tests).

**Spec:** [docs/superpowers/specs/2026-04-18-call-manager-proposal-assist-design.md](../specs/2026-04-18-call-manager-proposal-assist-design.md) — Data model section (lines 86–203).

**Overview:** [docs/superpowers/plans/2026-05-19-base-spec-overview.md](2026-05-19-base-spec-overview.md)

---

## Project Conventions Reference

Read this once before executing any task. Every test snippet below assumes these patterns. (Inherited from Slice A's plan; covers the same testcontainers + pg_session_factory infrastructure.)

### Session / DB fixture

`tests/conftest.py` provides `pg_session_factory: () -> SessionContextManager`. Use it like:

```python
def test_something(pg_session_factory):
    with pg_session_factory() as s:
        s.add(some_row)
        s.commit()
        # ... assertions inside the same session
```

The factory's teardown TRUNCATEs every table (`Base.metadata.sorted_tables`, CASCADE, RESTART IDENTITY) and re-seeds the two `sources` rows (`eu_portal`, `vinnova`).

### Migration numbering

Slice A landed migrations `0012` and `0013`. B1 starts at `0014` and runs sequentially through `0022` (9 migrations).

### Test layout

Test files live under `tests/proposal_assist/` (created in Slice A). Helpers go in `tests/proposal_assist/conftest.py` (which already has `make_call` from Slice A).

### `_settings_env` fixture

Already defined in `tests/proposal_assist/conftest.py` (Slice A). Any test that constructs `Settings()` or `create_app()` declares `_settings_env` in its arg list.

### Model conventions

Single-file: all models live in `src/call_monitor/db/models.py`. Append new classes after the last existing class. Use SQLAlchemy 2.x `Mapped[...]` + `mapped_column(...)`. UUIDs use `from sqlalchemy.dialects.postgresql import UUID` with `UUID(as_uuid=True)` and `default=uuid.uuid4`. JSONB uses `from sqlalchemy.dialects.postgresql import JSONB`. CHECK constraints declared in `__table_args__` (see `User`, `CallSectionExtract` for examples).

### `IntegrityError` for constraint tests

When asserting a DB constraint, use `pytest.raises(IntegrityError)` (`from sqlalchemy.exc import IntegrityError`) — not bare `Exception`. Matches the project convention in `tests/test_models_calls.py`.

### Don't sweep up pre-existing format hunks

After each task, run `uv run ruff format --check` on the files you touched. If ruff wants to format lines this task didn't add, leave them — `git diff --cached --stat` after staging should show only the in-scope additions.

---

## File map

**Created:**
- `alembic/versions/0014_workspaces.py`
- `alembic/versions/0015_workspace_viewers.py`
- `alembic/versions/0016_workspace_events.py`
- `alembic/versions/0017_chat_turns.py`
- `alembic/versions/0018_turn_jobs.py`
- `alembic/versions/0019_chat_summaries.py`
- `alembic/versions/0020_artifact_versions.py`
- `alembic/versions/0021_attachments.py`
- `alembic/versions/0022_attachment_jobs.py`
- `src/call_monitor/workspaces.py` — `create_workspace()` service function
- `tests/proposal_assist/test_models_workspace.py` — workspaces + workspace_viewers tests
- `tests/proposal_assist/test_models_workspace_events.py`
- `tests/proposal_assist/test_models_chat_turns.py`
- `tests/proposal_assist/test_models_turn_jobs.py`
- `tests/proposal_assist/test_models_chat_summaries.py`
- `tests/proposal_assist/test_models_artifact_versions.py`
- `tests/proposal_assist/test_models_attachments.py`
- `tests/proposal_assist/test_workspaces.py` — service-function tests

**Modified:**
- `src/call_monitor/db/models.py` — append 9 new model classes
- `tests/proposal_assist/conftest.py` — extend with `make_user()` helper used by multiple test files

---

## Task 1: `workspaces` table + `Workspace` model

**Files:**
- Create: `alembic/versions/0014_workspaces.py`
- Modify: `src/call_monitor/db/models.py`
- Modify: `tests/proposal_assist/conftest.py` (add `make_user` helper)
- Test: `tests/proposal_assist/test_models_workspace.py`

- [ ] **Step 1: Add `make_user` helper to conftest**

Append to `tests/proposal_assist/conftest.py`:

```python
def make_user(session, *, email: str = "owner@ki.se") -> "User":
    """Build a User row. Each call uses a unique email by default so multiple
    callers in the same test don't UNIQUE-collide. Callers that care about a
    specific email pass it explicitly."""
    from call_monitor.db.models import User
    u = User(email=email)
    session.add(u)
    session.flush()
    return u
```

- [ ] **Step 2: Write failing test**

`tests/proposal_assist/test_models_workspace.py`:

```python
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_monitor.db.models import Workspace
from tests.proposal_assist.conftest import make_call, make_user


def test_workspace_insert_minimum_fields(pg_session_factory):
    """Owner is the only required FK; call_id and profile_id are nullable."""
    with pg_session_factory() as s:
        owner = make_user(s)
        s.add(Workspace(title="My proposal", owner_id=owner.id))
        s.commit()
        ws = s.scalar(select(Workspace).where(Workspace.title == "My proposal"))
        assert ws is not None
        assert isinstance(ws.id, uuid.UUID)
        assert ws.owner_id == owner.id
        assert ws.call_id is None
        assert ws.profile_id is None
        assert ws.status == "drafting"
        assert ws.deadline is None
        assert ws.soft_deleted_at is None
        assert isinstance(ws.created_at, datetime)
        assert isinstance(ws.updated_at, datetime)


def test_workspace_optional_call_and_profile(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        call = make_call(s)
        s.commit()
        s.add(Workspace(
            title="Linked draft",
            owner_id=owner.id,
            call_id=call.id,
            deadline=datetime(2026, 12, 1, tzinfo=UTC),
        ))
        s.commit()
        ws = s.scalar(select(Workspace).where(Workspace.title == "Linked draft"))
        assert ws.call_id == call.id
        assert ws.deadline == datetime(2026, 12, 1, tzinfo=UTC)


def test_workspace_status_check_constraint(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        s.add(Workspace(title="bad", owner_id=owner.id, status="not_a_status"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_workspace_owner_fk_required(pg_session_factory):
    with pg_session_factory() as s:
        s.add(Workspace(title="no owner"))
        with pytest.raises(IntegrityError):
            s.commit()
```

- [ ] **Step 3: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_models_workspace.py -v
```

Expected: `ImportError: cannot import name 'Workspace' from 'call_monitor.db.models'`.

- [ ] **Step 4: Write migration**

`alembic/versions/0014_workspaces.py`:

```python
"""workspaces

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("call_id", sa.BigInteger(), nullable=True),
        sa.Column("profile_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default="drafting",
        ),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("soft_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["call_id"], ["calls.id"], ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["profiles.id"], ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('drafting','submitted','awarded','rejected','abandoned')",
            name="ck_workspaces_status",
        ),
    )
    op.create_index(
        "ix_workspaces_owner_status",
        "workspaces",
        ["owner_id", "status"],
        postgresql_where=sa.text("soft_deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_workspaces_owner_status", table_name="workspaces")
    op.drop_table("workspaces")
```

- [ ] **Step 5: Add `Workspace` model to `models.py`**

In `src/call_monitor/db/models.py`, append after the last existing class:

```python
import uuid
from sqlalchemy.dialects.postgresql import UUID


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint(
            "status IN ('drafting','submitted','awarded','rejected','abandoned')",
            name="ck_workspaces_status",
        ),
        Index(
            "ix_workspaces_owner_status",
            "owner_id", "status",
            postgresql_where=text("soft_deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False,
    )
    call_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("calls.id", ondelete="SET NULL"), nullable=True,
    )
    profile_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True,
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="drafting",
    )
    deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    soft_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
```

If `uuid` (the stdlib module) or `UUID` (from `sqlalchemy.dialects.postgresql`) aren't already imported at the top of `models.py`, add them. Slice A may have already imported them via `CallSectionExtract` or another model — check before adding.

- [ ] **Step 6: Run tests**

```bash
uv run pytest tests/proposal_assist/test_models_workspace.py -v
```

Expected: all 4 tests pass.

- [ ] **Step 7: Verify ruff**

```bash
uv run ruff format --check alembic/versions/0014_workspaces.py src/call_monitor/db/models.py tests/proposal_assist/conftest.py tests/proposal_assist/test_models_workspace.py
uv run ruff check alembic/versions/0014_workspaces.py src/call_monitor/db/models.py tests/proposal_assist/conftest.py tests/proposal_assist/test_models_workspace.py
```

If format wants pre-existing changes to `models.py`, leave them. Commit only your new lines.

- [ ] **Step 8: Commit**

```bash
git add alembic/versions/0014_workspaces.py src/call_monitor/db/models.py tests/proposal_assist/conftest.py tests/proposal_assist/test_models_workspace.py
git diff --cached --stat   # confirm only these 4 files
git commit -m "feat(base-spec): add workspaces table + Workspace model"
```

---

## Task 2: `workspace_viewers` table + `WorkspaceViewer` model

**Files:**
- Create: `alembic/versions/0015_workspace_viewers.py`
- Modify: `src/call_monitor/db/models.py` (append `WorkspaceViewer`)
- Modify: `tests/proposal_assist/test_models_workspace.py` (append viewer tests)

- [ ] **Step 1: Add `make_workspace` helper to conftest**

Append to `tests/proposal_assist/conftest.py`:

```python
def make_workspace(
    session,
    *,
    owner=None,
    title: str = "Test workspace",
    call_id: int | None = None,
    profile_id: int | None = None,
) -> "Workspace":
    """Build a Workspace row. If `owner` not provided, creates a fresh User."""
    from call_monitor.db.models import Workspace
    if owner is None:
        owner = make_user(session)
    ws = Workspace(
        title=title,
        owner_id=owner.id,
        call_id=call_id,
        profile_id=profile_id,
    )
    session.add(ws)
    session.flush()
    return ws
```

- [ ] **Step 2: Append failing tests**

Append to `tests/proposal_assist/test_models_workspace.py`:

```python
from call_monitor.db.models import WorkspaceViewer
from tests.proposal_assist.conftest import make_workspace


def test_workspace_viewer_composite_pk(pg_session_factory):
    """The (workspace_id, user_id) composite PK is enforced — inserting the
    same pair twice raises IntegrityError."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        viewer = make_user(s, email="viewer@ki.se")
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=viewer.id))
        s.commit()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=viewer.id))
        with pytest.raises(IntegrityError):
            s.commit()


def test_workspace_viewer_cascade_on_workspace_delete(pg_session_factory):
    """Deleting a workspace cascades to its viewers."""
    from sqlalchemy import delete
    with pg_session_factory() as s:
        ws = make_workspace(s)
        viewer = make_user(s, email="viewer2@ki.se")
        ws_id = ws.id
        viewer_id = viewer.id
        s.add(WorkspaceViewer(workspace_id=ws_id, user_id=viewer_id))
        s.commit()
        s.execute(delete(Workspace).where(Workspace.id == ws_id))
        s.commit()
        remaining = s.scalar(
            select(WorkspaceViewer).where(
                WorkspaceViewer.workspace_id == ws_id,
                WorkspaceViewer.user_id == viewer_id,
            )
        )
        assert remaining is None
```

- [ ] **Step 3: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_models_workspace.py::test_workspace_viewer_composite_pk -v
```

Expected: ImportError.

- [ ] **Step 4: Write migration**

`alembic/versions/0015_workspace_viewers.py`:

```python
"""workspace_viewers

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_viewers",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "added_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "user_id"),
    )


def downgrade() -> None:
    op.drop_table("workspace_viewers")
```

- [ ] **Step 5: Add model**

In `models.py`, append after `Workspace`:

```python
class WorkspaceViewer(Base):
    __tablename__ = "workspace_viewers"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
```

- [ ] **Step 6: Run + lint + commit**

```bash
uv run pytest tests/proposal_assist/test_models_workspace.py -v
uv run ruff format --check alembic/versions/0015_workspace_viewers.py src/call_monitor/db/models.py tests/proposal_assist/conftest.py tests/proposal_assist/test_models_workspace.py
git add alembic/versions/0015_workspace_viewers.py src/call_monitor/db/models.py tests/proposal_assist/conftest.py tests/proposal_assist/test_models_workspace.py
git commit -m "feat(base-spec): add workspace_viewers table + model"
```

---

## Task 3: `workspace_events` table + `WorkspaceEvent` model

**Files:**
- Create: `alembic/versions/0016_workspace_events.py`
- Modify: `src/call_monitor/db/models.py`
- Test: `tests/proposal_assist/test_models_workspace_events.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_models_workspace_events.py`:

```python
import uuid
from datetime import datetime

from sqlalchemy import select

from call_monitor.db.models import WorkspaceEvent
from tests.proposal_assist.conftest import make_user, make_workspace


def test_workspace_event_insert_with_payload(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        actor = make_user(s, email="actor@ki.se")
        s.add(WorkspaceEvent(
            workspace_id=ws.id,
            kind="created",
            payload={"title": ws.title, "owner_email": "actor@ki.se"},
            actor_id=actor.id,
        ))
        s.commit()
        ev = s.scalar(
            select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == ws.id)
        )
        assert ev is not None
        assert isinstance(ev.id, uuid.UUID)
        assert ev.kind == "created"
        assert ev.payload["title"] == ws.title
        assert ev.actor_id == actor.id
        assert isinstance(ev.created_at, datetime)


def test_workspace_event_actor_id_is_nullable(pg_session_factory):
    """System events (e.g., scheduler-driven) have no actor."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(WorkspaceEvent(
            workspace_id=ws.id,
            kind="status_changed",
            payload={"from": "drafting", "to": "submitted"},
            actor_id=None,
        ))
        s.commit()
        ev = s.scalar(
            select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == ws.id)
        )
        assert ev.actor_id is None
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_models_workspace_events.py -v
```

- [ ] **Step 3: Migration**

`alembic/versions/0016_workspace_events.py`:

```python
"""workspace_events

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workspace_events_ws_created",
        "workspace_events",
        ["workspace_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_events_ws_created", table_name="workspace_events",
    )
    op.drop_table("workspace_events")
```

- [ ] **Step 4: Model**

In `models.py`, after `WorkspaceViewer`:

```python
class WorkspaceEvent(Base):
    __tablename__ = "workspace_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"),
    )
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
```

- [ ] **Step 5: Run + lint + commit**

```bash
uv run pytest tests/proposal_assist/test_models_workspace_events.py -v
uv run ruff format --check alembic/versions/0016_workspace_events.py src/call_monitor/db/models.py tests/proposal_assist/test_models_workspace_events.py
git add alembic/versions/0016_workspace_events.py src/call_monitor/db/models.py tests/proposal_assist/test_models_workspace_events.py
git commit -m "feat(base-spec): add workspace_events audit table + model"
```

---

## Task 4: `chat_turns` table + `ChatTurn` model

**Files:**
- Create: `alembic/versions/0017_chat_turns.py`
- Modify: `src/call_monitor/db/models.py`
- Test: `tests/proposal_assist/test_models_chat_turns.py`

- [ ] **Step 1: Write failing tests**

`tests/proposal_assist/test_models_chat_turns.py`:

```python
import uuid
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_monitor.db.models import ChatTurn
from tests.proposal_assist.conftest import make_workspace


def test_chat_turn_insert_user_role(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(ChatTurn(
            workspace_id=ws.id,
            role="user",
            content={"type": "text", "text": "Help me draft this."},
        ))
        s.commit()
        turn = s.scalar(
            select(ChatTurn).where(ChatTurn.workspace_id == ws.id)
        )
        assert isinstance(turn.id, uuid.UUID)
        assert turn.role == "user"
        assert turn.content["text"] == "Help me draft this."
        assert turn.tokens_in is None
        assert turn.tokens_out is None
        assert turn.model is None
        assert isinstance(turn.created_at, datetime)


def test_chat_turn_insert_assistant_with_tokens(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(ChatTurn(
            workspace_id=ws.id,
            role="assistant",
            content={"type": "text", "text": "Here's a draft."},
            tokens_in=1234,
            tokens_out=567,
            model="claude-opus-4-7",
        ))
        s.commit()
        turn = s.scalar(
            select(ChatTurn).where(ChatTurn.workspace_id == ws.id)
        )
        assert turn.role == "assistant"
        assert turn.tokens_in == 1234
        assert turn.tokens_out == 567
        assert turn.model == "claude-opus-4-7"


def test_chat_turn_role_check_constraint(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(ChatTurn(
            workspace_id=ws.id, role="not_a_role", content={},
        ))
        with pytest.raises(IntegrityError):
            s.commit()
```

- [ ] **Step 2: Run to confirm failure** — ImportError.

- [ ] **Step 3: Migration**

`alembic/versions/0017_chat_turns.py`:

```python
"""chat_turns

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "content",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "role IN ('user','assistant','tool')",
            name="ck_chat_turns_role",
        ),
    )
    op.create_index(
        "ix_chat_turns_ws_created",
        "chat_turns",
        ["workspace_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_turns_ws_created", table_name="chat_turns")
    op.drop_table("chat_turns")
```

- [ ] **Step 4: Model**

In `models.py`:

```python
class ChatTurn(Base):
    __tablename__ = "chat_turns"
    __table_args__ = (
        CheckConstraint(
            "role IN ('user','assistant','tool')",
            name="ck_chat_turns_role",
        ),
        Index("ix_chat_turns_ws_created", "workspace_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
```

- [ ] **Step 5: Run + lint + commit**

```bash
uv run pytest tests/proposal_assist/test_models_chat_turns.py -v
uv run ruff format --check alembic/versions/0017_chat_turns.py src/call_monitor/db/models.py tests/proposal_assist/test_models_chat_turns.py
git add alembic/versions/0017_chat_turns.py src/call_monitor/db/models.py tests/proposal_assist/test_models_chat_turns.py
git commit -m "feat(base-spec): add chat_turns table + model"
```

---

## Task 5: `turn_jobs` table + `TurnJob` model + single-running-per-workspace lock

**Files:**
- Create: `alembic/versions/0018_turn_jobs.py`
- Modify: `src/call_monitor/db/models.py`
- Test: `tests/proposal_assist/test_models_turn_jobs.py`

- [ ] **Step 1: Write failing tests**

`tests/proposal_assist/test_models_turn_jobs.py`:

```python
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_monitor.db.models import ChatTurn, TurnJob
from tests.proposal_assist.conftest import make_workspace


def test_turn_job_insert_queued(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        user_turn = ChatTurn(
            workspace_id=ws.id, role="user", content={"text": "Help."},
        )
        s.add(user_turn)
        s.flush()
        s.add(TurnJob(
            workspace_id=ws.id,
            user_turn_id=user_turn.id,
            status="queued",
        ))
        s.commit()
        job = s.scalar(
            select(TurnJob).where(TurnJob.workspace_id == ws.id)
        )
        assert isinstance(job.id, uuid.UUID)
        assert job.status == "queued"
        assert job.error is None
        assert job.user_turn_id == user_turn.id


def test_turn_job_status_check(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(TurnJob(workspace_id=ws.id, status="not_a_status"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_turn_job_unique_running_per_workspace(pg_session_factory):
    """The partial UNIQUE INDEX on (workspace_id) WHERE status='running'
    enforces at-most-one-running turn per workspace."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(TurnJob(workspace_id=ws.id, status="running"))
        s.commit()
        s.add(TurnJob(workspace_id=ws.id, status="running"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_turn_job_multiple_queued_allowed(pg_session_factory):
    """The unique constraint is partial — it only applies to status='running'.
    Multiple queued jobs per workspace are fine (the FIFO drain pattern)."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(TurnJob(workspace_id=ws.id, status="queued"))
        s.add(TurnJob(workspace_id=ws.id, status="queued"))
        s.commit()  # should not raise


def test_turn_job_completed_does_not_conflict_with_running(pg_session_factory):
    """A completed job + a new running job on the same workspace is fine."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(TurnJob(workspace_id=ws.id, status="completed"))
        s.add(TurnJob(workspace_id=ws.id, status="running"))
        s.commit()  # should not raise
```

- [ ] **Step 2: Run to confirm failure** — ImportError.

- [ ] **Step 3: Migration**

`alembic/versions/0018_turn_jobs.py`:

```python
"""turn_jobs

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "turn_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_turn_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "queued_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_turn_id"], ["chat_turns.id"], ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','failed','cancelled')",
            name="ck_turn_jobs_status",
        ),
    )
    op.create_index(
        "ix_turn_jobs_ws_queued",
        "turn_jobs",
        ["workspace_id", "queued_at"],
    )
    # Single-running-turn-per-workspace enforcement.
    op.create_index(
        "uq_turn_jobs_running",
        "turn_jobs",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("uq_turn_jobs_running", table_name="turn_jobs")
    op.drop_index("ix_turn_jobs_ws_queued", table_name="turn_jobs")
    op.drop_table("turn_jobs")
```

- [ ] **Step 4: Model**

In `models.py`:

```python
class TurnJob(Base):
    __tablename__ = "turn_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','completed','failed','cancelled')",
            name="ck_turn_jobs_status",
        ),
        Index("ix_turn_jobs_ws_queued", "workspace_id", "queued_at"),
        Index(
            "uq_turn_jobs_running",
            "workspace_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_turns.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
```

- [ ] **Step 5: Run + lint + commit**

```bash
uv run pytest tests/proposal_assist/test_models_turn_jobs.py -v
uv run ruff format --check alembic/versions/0018_turn_jobs.py src/call_monitor/db/models.py tests/proposal_assist/test_models_turn_jobs.py
git add alembic/versions/0018_turn_jobs.py src/call_monitor/db/models.py tests/proposal_assist/test_models_turn_jobs.py
git commit -m "feat(base-spec): add turn_jobs table + single-running-per-workspace lock"
```

---

## Task 6: `chat_summaries` table + `ChatSummary` model

**Files:**
- Create: `alembic/versions/0019_chat_summaries.py`
- Modify: `src/call_monitor/db/models.py`
- Test: `tests/proposal_assist/test_models_chat_summaries.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_models_chat_summaries.py`:

```python
import uuid

from sqlalchemy import select

from call_monitor.db.models import ChatSummary, ChatTurn
from tests.proposal_assist.conftest import make_workspace


def test_chat_summary_insert(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        last_turn = ChatTurn(
            workspace_id=ws.id, role="assistant",
            content={"text": "summary boundary turn"},
        )
        s.add(last_turn)
        s.flush()
        s.add(ChatSummary(
            workspace_id=ws.id,
            summary="100 turns of brainstorming about cancer biomarkers.",
            summarized_through=last_turn.id,
            tokens=4321,
            model="claude-haiku-4-5",
        ))
        s.commit()
        cs = s.scalar(
            select(ChatSummary).where(ChatSummary.workspace_id == ws.id)
        )
        assert isinstance(cs.id, uuid.UUID)
        assert cs.summary.startswith("100 turns")
        assert cs.summarized_through == last_turn.id
        assert cs.tokens == 4321
        assert cs.model == "claude-haiku-4-5"
```

- [ ] **Step 2: Run to confirm failure** — ImportError.

- [ ] **Step 3: Migration**

`alembic/versions/0019_chat_summaries.py`:

```python
"""chat_summaries

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_summaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "summarized_through", postgresql.UUID(as_uuid=True), nullable=False,
        ),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["summarized_through"], ["chat_turns.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_chat_summaries_ws_created",
        "chat_summaries",
        ["workspace_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_chat_summaries_ws_created", table_name="chat_summaries",
    )
    op.drop_table("chat_summaries")
```

- [ ] **Step 4: Model**

```python
class ChatSummary(Base):
    __tablename__ = "chat_summaries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    summarized_through: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_turns.id", ondelete="CASCADE"),
        nullable=False,
    )
    tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
```

- [ ] **Step 5: Run + lint + commit**

```bash
uv run pytest tests/proposal_assist/test_models_chat_summaries.py -v
uv run ruff format --check alembic/versions/0019_chat_summaries.py src/call_monitor/db/models.py tests/proposal_assist/test_models_chat_summaries.py
git add alembic/versions/0019_chat_summaries.py src/call_monitor/db/models.py tests/proposal_assist/test_models_chat_summaries.py
git commit -m "feat(base-spec): add chat_summaries table + model"
```

---

## Task 7: `artifact_versions` table + `ArtifactVersion` model

**Files:**
- Create: `alembic/versions/0020_artifact_versions.py`
- Modify: `src/call_monitor/db/models.py`
- Test: `tests/proposal_assist/test_models_artifact_versions.py`

- [ ] **Step 1: Write failing tests**

`tests/proposal_assist/test_models_artifact_versions.py`:

```python
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_monitor.db.models import ArtifactVersion, ChatTurn
from tests.proposal_assist.conftest import make_workspace


def test_artifact_version_insert_agent_commit(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        triggering = ChatTurn(
            workspace_id=ws.id, role="assistant", content={"text": "edit"},
        )
        s.add(triggering)
        s.flush()
        s.add(ArtifactVersion(
            workspace_id=ws.id,
            commit_sha="a" * 40,
            triggering_turn_id=triggering.id,
            created_by="agent",
            message="agent: edit proposal section 2",
        ))
        s.commit()
        v = s.scalar(
            select(ArtifactVersion).where(ArtifactVersion.workspace_id == ws.id)
        )
        assert isinstance(v.id, uuid.UUID)
        assert v.commit_sha == "a" * 40
        assert v.created_by == "agent"
        assert v.triggering_turn_id == triggering.id


def test_artifact_version_triggering_turn_id_nullable(pg_session_factory):
    """System-driven commits (e.g., from a recovery script) have no turn."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(ArtifactVersion(
            workspace_id=ws.id,
            commit_sha="b" * 40,
            triggering_turn_id=None,
            created_by="system",
            message="system: initial commit",
        ))
        s.commit()
        v = s.scalar(
            select(ArtifactVersion).where(ArtifactVersion.workspace_id == ws.id)
        )
        assert v.triggering_turn_id is None


def test_artifact_version_created_by_check(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(ArtifactVersion(
            workspace_id=ws.id, commit_sha="c" * 40,
            created_by="not_a_role", message="x",
        ))
        with pytest.raises(IntegrityError):
            s.commit()
```

- [ ] **Step 2: Run to confirm failure** — ImportError.

- [ ] **Step 3: Migration**

`alembic/versions/0020_artifact_versions.py`:

```python
"""artifact_versions

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifact_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("commit_sha", sa.Text(), nullable=False),
        sa.Column(
            "triggering_turn_id", postgresql.UUID(as_uuid=True), nullable=True,
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["triggering_turn_id"], ["chat_turns.id"], ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "created_by IN ('agent','user','system','reconciled','unrecoverable')",
            name="ck_artifact_versions_created_by",
        ),
    )
    op.create_index(
        "ix_artifact_versions_ws_created",
        "artifact_versions",
        ["workspace_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_artifact_versions_ws_created", table_name="artifact_versions",
    )
    op.drop_table("artifact_versions")
```

- [ ] **Step 4: Model**

```python
class ArtifactVersion(Base):
    __tablename__ = "artifact_versions"
    __table_args__ = (
        CheckConstraint(
            "created_by IN ('agent','user','system','reconciled','unrecoverable')",
            name="ck_artifact_versions_created_by",
        ),
        Index(
            "ix_artifact_versions_ws_created",
            "workspace_id",
            text("created_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    triggering_turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_turns.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
```

- [ ] **Step 5: Run + lint + commit**

```bash
uv run pytest tests/proposal_assist/test_models_artifact_versions.py -v
uv run ruff format --check alembic/versions/0020_artifact_versions.py src/call_monitor/db/models.py tests/proposal_assist/test_models_artifact_versions.py
git add alembic/versions/0020_artifact_versions.py src/call_monitor/db/models.py tests/proposal_assist/test_models_artifact_versions.py
git commit -m "feat(base-spec): add artifact_versions table + model"
```

---

## Task 8: `attachments` table + `Attachment` model

**Files:**
- Create: `alembic/versions/0021_attachments.py`
- Modify: `src/call_monitor/db/models.py`
- Test: `tests/proposal_assist/test_models_attachments.py`

- [ ] **Step 1: Write failing tests**

`tests/proposal_assist/test_models_attachments.py`:

```python
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_monitor.db.models import Attachment
from tests.proposal_assist.conftest import make_user, make_workspace


def test_attachment_insert_figure(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        uploader = make_user(s, email="uploader@ki.se")
        s.add(Attachment(
            workspace_id=ws.id,
            kind="figure",
            filename="fig1.png",
            storage_path="figs/fig1.png",
            mime_type="image/png",
            uploaded_by=uploader.id,
        ))
        s.commit()
        a = s.scalar(
            select(Attachment).where(Attachment.workspace_id == ws.id)
        )
        assert isinstance(a.id, uuid.UUID)
        assert a.kind == "figure"
        assert a.filename == "fig1.png"
        assert a.description is None  # populated later by describe_figure job


def test_attachment_kind_check(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        uploader = make_user(s, email="x@ki.se")
        s.add(Attachment(
            workspace_id=ws.id, kind="not_a_kind",
            filename="x", storage_path="x", mime_type="x/x",
            uploaded_by=uploader.id,
        ))
        with pytest.raises(IntegrityError):
            s.commit()
```

- [ ] **Step 2: Run to confirm failure** — ImportError.

- [ ] **Step 3: Migration**

`alembic/versions/0021_attachments.py`:

```python
"""attachments

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("uploaded_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "uploaded_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "kind IN ('figure','preliminary_data','past_grant','other')",
            name="ck_attachments_kind",
        ),
    )
    op.create_index(
        "ix_attachments_ws",
        "attachments",
        ["workspace_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_attachments_ws", table_name="attachments")
    op.drop_table("attachments")
```

- [ ] **Step 4: Model**

```python
class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('figure','preliminary_data','past_grant','other')",
            name="ck_attachments_kind",
        ),
        Index("ix_attachments_ws", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False,
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
```

- [ ] **Step 5: Run + lint + commit**

```bash
uv run pytest tests/proposal_assist/test_models_attachments.py -v
uv run ruff format --check alembic/versions/0021_attachments.py src/call_monitor/db/models.py tests/proposal_assist/test_models_attachments.py
git add alembic/versions/0021_attachments.py src/call_monitor/db/models.py tests/proposal_assist/test_models_attachments.py
git commit -m "feat(base-spec): add attachments table + model"
```

---

## Task 9: `attachment_jobs` table + `AttachmentJob` model

**Files:**
- Create: `alembic/versions/0022_attachment_jobs.py`
- Modify: `src/call_monitor/db/models.py`
- Test: extend `tests/proposal_assist/test_models_attachments.py`

- [ ] **Step 1: Append failing tests**

```python
from call_monitor.db.models import AttachmentJob


def _add_attachment(s, ws_id, user_id, *, filename="fig.png"):
    a = Attachment(
        workspace_id=ws_id, kind="figure",
        filename=filename, storage_path=f"figs/{filename}",
        mime_type="image/png", uploaded_by=user_id,
    )
    s.add(a)
    s.flush()
    return a


def test_attachment_job_insert_queued(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        uploader = make_user(s, email="u@ki.se")
        attachment = _add_attachment(s, ws.id, uploader.id)
        s.add(AttachmentJob(
            workspace_id=ws.id,
            attachment_id=attachment.id,
            kind="describe_figure",
            status="queued",
        ))
        s.commit()
        j = s.scalar(
            select(AttachmentJob).where(
                AttachmentJob.attachment_id == attachment.id
            )
        )
        assert j is not None
        assert j.status == "queued"
        assert j.attempts == 0


def test_attachment_job_unique_active_per_attachment_kind(pg_session_factory):
    """Partial UNIQUE on (attachment_id, kind) WHERE status IN
    ('queued','running') prevents double-enqueueing the same describe job."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        uploader = make_user(s, email="u2@ki.se")
        attachment = _add_attachment(s, ws.id, uploader.id)
        s.add(AttachmentJob(
            workspace_id=ws.id, attachment_id=attachment.id,
            kind="describe_figure", status="queued",
        ))
        s.commit()
        s.add(AttachmentJob(
            workspace_id=ws.id, attachment_id=attachment.id,
            kind="describe_figure", status="running",
        ))
        with pytest.raises(IntegrityError):
            s.commit()


def test_attachment_job_completed_allows_re_enqueue(pg_session_factory):
    """A previously-completed job doesn't block a new queued job for the
    same (attachment_id, kind) — the partial index is bounded by status."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        uploader = make_user(s, email="u3@ki.se")
        attachment = _add_attachment(s, ws.id, uploader.id)
        s.add(AttachmentJob(
            workspace_id=ws.id, attachment_id=attachment.id,
            kind="describe_figure", status="completed",
        ))
        s.commit()
        s.add(AttachmentJob(
            workspace_id=ws.id, attachment_id=attachment.id,
            kind="describe_figure", status="queued",
        ))
        s.commit()  # should not raise
```

- [ ] **Step 2: Run to confirm failure** — ImportError.

- [ ] **Step 3: Migration**

`alembic/versions/0022_attachment_jobs.py`:

```python
"""attachment_jobs

Revision ID: 0022
Revises: 0021
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attachment_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attachment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "attempts", sa.Integer(),
            nullable=False, server_default=sa.text("0"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "queued_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["attachment_id"], ["attachments.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','failed')",
            name="ck_attachment_jobs_status",
        ),
        sa.CheckConstraint(
            "kind IN ('describe_figure')",
            name="ck_attachment_jobs_kind",
        ),
    )
    op.create_index(
        "ix_attachment_jobs_status_updated",
        "attachment_jobs",
        ["status", "updated_at"],
    )
    op.create_index(
        "uq_attachment_jobs_active",
        "attachment_jobs",
        ["attachment_id", "kind"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_attachment_jobs_active", table_name="attachment_jobs",
    )
    op.drop_index(
        "ix_attachment_jobs_status_updated", table_name="attachment_jobs",
    )
    op.drop_table("attachment_jobs")
```

- [ ] **Step 4: Model**

```python
class AttachmentJob(Base):
    __tablename__ = "attachment_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','completed','failed')",
            name="ck_attachment_jobs_status",
        ),
        CheckConstraint(
            "kind IN ('describe_figure')",
            name="ck_attachment_jobs_kind",
        ),
        Index(
            "ix_attachment_jobs_status_updated", "status", "updated_at",
        ),
        Index(
            "uq_attachment_jobs_active",
            "attachment_id", "kind",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    attachment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("attachments.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"),
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
```

- [ ] **Step 5: Run + lint + commit**

```bash
uv run pytest tests/proposal_assist/test_models_attachments.py -v
uv run ruff format --check alembic/versions/0022_attachment_jobs.py src/call_monitor/db/models.py tests/proposal_assist/test_models_attachments.py
git add alembic/versions/0022_attachment_jobs.py src/call_monitor/db/models.py tests/proposal_assist/test_models_attachments.py
git commit -m "feat(base-spec): add attachment_jobs table + unique-active-job index"
```

---

## Task 10: `create_workspace()` service function

The function that ties Slice A back into the rest of the system: given an owner + optional call + optional profile, insert a Workspace row, enqueue a `workspace_setup_jobs` row (Slice A's table), and write a `workspace_events(kind='created')` audit row — all in one transaction.

**Files:**
- Create: `src/call_monitor/workspaces.py`
- Test: `tests/proposal_assist/test_workspaces.py`

- [ ] **Step 1: Write failing tests**

`tests/proposal_assist/test_workspaces.py`:

```python
import pytest
from sqlalchemy import select

from call_monitor.db.models import (
    Workspace,
    WorkspaceEvent,
    WorkspaceSetupJob,
)
from call_monitor.workspaces import create_workspace
from tests.proposal_assist.conftest import make_call, make_user


def test_create_workspace_inserts_workspace_row(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        s.commit()
        ws = create_workspace(
            session=s,
            owner_id=owner.id,
            title="Cancer-biomarker proposal",
        )
        s.commit()

        fetched = s.get(Workspace, ws.id)
        assert fetched is not None
        assert fetched.title == "Cancer-biomarker proposal"
        assert fetched.owner_id == owner.id
        assert fetched.call_id is None
        assert fetched.profile_id is None
        assert fetched.status == "drafting"


def test_create_workspace_enqueues_phase0_setup_job(pg_session_factory):
    """Phase 0 (Slice A) drains workspace_setup_jobs. create_workspace
    must enqueue a row so the next tick picks it up."""
    with pg_session_factory() as s:
        owner = make_user(s)
        s.commit()
        ws = create_workspace(session=s, owner_id=owner.id, title="x")
        s.commit()

        job = s.scalar(
            select(WorkspaceSetupJob).where(
                WorkspaceSetupJob.workspace_id == ws.id
            )
        )
        assert job is not None
        assert job.status == "queued"
        assert job.attempts == 0


def test_create_workspace_writes_created_audit_event(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        s.commit()
        ws = create_workspace(
            session=s, owner_id=owner.id, title="audit-test",
        )
        s.commit()

        events = s.scalars(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws.id,
                WorkspaceEvent.kind == "created",
            )
        ).all()
        assert len(events) == 1
        assert events[0].actor_id == owner.id
        assert events[0].payload["title"] == "audit-test"


def test_create_workspace_with_call_and_profile(pg_session_factory):
    """Optional call_id + profile_id propagate to the row. The Phase 0
    worker uses these to know which call to snapshot."""
    with pg_session_factory() as s:
        owner = make_user(s)
        call = make_call(s)
        s.commit()
        ws = create_workspace(
            session=s, owner_id=owner.id, title="linked",
            call_id=call.id, profile_id=None,
        )
        s.commit()

        fetched = s.get(Workspace, ws.id)
        assert fetched.call_id == call.id


def test_create_workspace_raises_for_unknown_owner(pg_session_factory):
    """If owner_id doesn't exist, the workspace insert fails (FK).
    No partial state is left behind."""
    from sqlalchemy.exc import IntegrityError

    with pg_session_factory() as s:
        with pytest.raises(IntegrityError):
            create_workspace(
                session=s, owner_id=99999, title="bad",
            )
            s.commit()
        s.rollback()
        # No setup job, no event, no workspace.
        assert s.scalar(select(WorkspaceSetupJob)) is None
        assert s.scalar(select(WorkspaceEvent)) is None
        assert s.scalar(select(Workspace)) is None
```

- [ ] **Step 2: Run to confirm failure** — `ModuleNotFoundError: No module named 'call_monitor.workspaces'`.

- [ ] **Step 3: Implement**

Create `src/call_monitor/workspaces.py`:

```python
"""Workspace lifecycle service functions.

Single entrypoint today: `create_workspace`. It does three things in one
unit of work:

  1. Insert a Workspace row.
  2. Enqueue a `workspace_setup_jobs` row (Slice A's Phase 0 worker drains
     this on its scheduler tick — see
     src/call_monitor/proposal_assist/phases/setup.py).
  3. Write a workspace_events(kind='created') audit row.

The caller commits. If any of the three fails, the caller rolls back and
no partial state lands.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from call_monitor.db.models import (
    Workspace,
    WorkspaceEvent,
    WorkspaceSetupJob,
)


def create_workspace(
    *,
    session: Session,
    owner_id: int,
    title: str,
    call_id: int | None = None,
    profile_id: int | None = None,
) -> Workspace:
    """Create a new workspace and enqueue Phase 0 setup. Returns the
    Workspace (with `.id` populated via session.flush())."""
    ws = Workspace(
        owner_id=owner_id,
        title=title,
        call_id=call_id,
        profile_id=profile_id,
    )
    session.add(ws)
    session.flush()  # populates ws.id from the DB default

    session.add(WorkspaceSetupJob(
        workspace_id=ws.id,
        status="queued",
    ))
    session.add(WorkspaceEvent(
        workspace_id=ws.id,
        kind="created",
        payload={
            "title": title,
            "call_id": call_id,
            "profile_id": profile_id,
        },
        actor_id=owner_id,
    ))
    return ws
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/proposal_assist/test_workspaces.py -v
```

Expected: 5 tests pass.

- [ ] **Step 5: Ruff**

```bash
uv run ruff format --check src/call_monitor/workspaces.py tests/proposal_assist/test_workspaces.py
uv run ruff check src/call_monitor/workspaces.py tests/proposal_assist/test_workspaces.py
```

- [ ] **Step 6: Commit**

```bash
git add src/call_monitor/workspaces.py tests/proposal_assist/test_workspaces.py
git diff --cached --stat   # 2 files
git commit -m "feat(base-spec): create_workspace() service — inserts row, enqueues Phase 0 job, writes audit event"
```

---

## Task 11: B1 milestone

- [ ] **Step 1: Full suite**

```bash
uv run pytest -q
```

Expected: 572 (post-Slice-A) + ~30 new = ~600 tests pass.

- [ ] **Step 2: Migration round-trip**

```bash
uv run alembic downgrade 0013
uv run alembic upgrade head
uv run pytest tests/proposal_assist/ -v
```

Confirms all 9 new migrations roll back and re-apply cleanly. Note: requires `DATABASE_URL` env to be set if running outside the test fixtures; pytest's `migrated_engine` fixture handles it during the test run.

- [ ] **Step 3: Lint**

```bash
uv run ruff check src/call_monitor tests/proposal_assist
```

(Run without `format --check` here — the project has pre-existing format violations in `models.py` from before Slice A; we don't own those.)

- [ ] **Step 4: Surface to user**

B1 delivers:
- 9 new tables (`workspaces`, `workspace_viewers`, `workspace_events`, `chat_turns`, `turn_jobs`, `chat_summaries`, `artifact_versions`, `attachments`, `attachment_jobs`) — migrations `0014`–`0022`
- 9 new SQLAlchemy models in `src/call_monitor/db/models.py`
- `create_workspace()` service function at `src/call_monitor/workspaces.py` that wires workspace creation into Slice A's Phase 0 queue

**Slice A Part 2 is now unblocked.** The Phase 0 worker can drain real setup jobs created via `create_workspace()`.

**What's NOT in B1:**
- Web endpoints (POST /workspaces, etc.) — B6
- Agent SDK adapter + drafting subprocess — B3
- Turn queue logic, chat machinery, compaction — B4
- Tool surface (edit_proposal, etc.) — B5
- Figures, attachments upload, vision — B7
- Disclosure, kill switch, spend guardrails — B8
- Rename `call-monitor` → `call-manager` — B2

Surface to the user; ask whether to continue with **Slice A Part 2** (now that it's unblocked) or move to **B2/B3/...** next.

---

## Self-review notes (for the engineer running this plan)

1. **Migration revision numbering (0014–0022)** — assumes no concurrent branch has added migrations after 0013. If a parallel branch has bumped past 0013, renumber: rename files + update `revision = "..."` + `down_revision = "..."` chains.

2. **`__table_args__` parity with migrations** — every CHECK / partial UNIQUE in a migration must be mirrored in the model's `__table_args__` so `alembic check --autogenerate` sees no drift. This bit Slice A Task 3 (had to fix in a follow-up commit) — get it right the first time here.

3. **Pre-existing format violations in `models.py`** — running `uv run ruff format` on `models.py` will sweep up violations from before Slice A. Don't include those in your B1 commits. After each `git add models.py`, run `git diff --cached src/call_monitor/db/models.py` and confirm only your new lines appear.

4. **The `import uuid` and `from sqlalchemy.dialects.postgresql import UUID, JSONB` lines** — add to the top of `models.py` once (Task 1), then subsequent tasks reuse them. If Slice A already added them, you'll see them present; if not, add them.

5. **Test layout** — putting these under `tests/proposal_assist/` matches the precedent. If a future plan needs to share the schema-foundation tests with a non-proposal-assist module, we can reorganize then.
