# Collaborative Editing — Slice 1: Yjs Sync Server (transport + persistence + auth) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up an authenticated, permission-gated, Postgres-persisted Yjs WebSocket sync server — one room per `(workspace, artifact)` — that two `pycrdt` clients converge through and that survives a server restart, with **no frontend and no git coupling yet**.

**Architecture:** A **hand-rolled** room on a FastAPI `WebSocket` route using `pycrdt.Doc` + the y-sync protocol codec already shipped in `pycrdt` (`pycrdt._sync`). We do NOT use `pycrdt-websocket`/`pycrdt-store`: their high-level `YRoom.on_message` gives only raw bytes (no sender identity) and `BaseYStore.write` gives only bytes (no actor) — both disqualifying for our **per-update write-permission gate** and **per-update actor tagging**. We own the per-connection loop, the in-process room registry, and a Postgres update-log table. The backend is sync-SQLAlchemy, so every DB call inside the async WS handler is offloaded with `starlette.concurrency.run_in_threadpool`.

**Tech Stack:** Python 3.12, FastAPI/Starlette WebSockets, `pycrdt` 0.14.0 (already a dependency — confirmed exporting `Doc`, `YMessageType`, `YSyncMessageType`, `create_sync_message`, `handle_sync_message`, `create_update_message`, `read_message`, `write_var_uint` from `pycrdt._sync`), SQLAlchemy 2.x + Alembic + PostgreSQL, pytest + testcontainers PG.

## Global Constraints

- **No new dependencies.** `pycrdt` 0.14.0 (added in the Slice-0 spike) already provides every codec symbol; verified `pycrdt.websocket`/`pycrdt.store` are absent and unneeded. Do not add `pycrdt-websocket`, `pycrdt-store`, or `sqlite-anyio`.
- **Pure transport.** Rooms start **empty**. NO git hydration and NO checkpoint-to-git in this slice (both are Slice 2). Existing draft markdown in git is irrelevant to Slice 1 (treated as demo content; back-compat is explicitly out of scope per product decision).
- **Auth model = connect-time + cached per-update gate.** Authenticate the connection from the `session` cookie and resolve `can_write` ONCE at connect; gate every inbound document-changing message against that cached flag. **No mid-session downgrade / re-query / notify** — that is Slice 2 (where spec-lock becomes room-aware).
- **Single-instance backend.** All clients of a room reach the same process; an in-process room registry is correct (consistent with the existing single-instance scheduler constraint). No horizontal scaling.
- **Sync DB in async handler.** Reuse the existing **sync** `Session`/`session_factory`; never call it directly on the event loop — wrap each DB op in `run_in_threadpool`.
- **Per-update actor tagging.** Every persisted Yjs update row records the authenticated `user_id` that sent it (for authorship in later slices).
- **Security & failure-path invariants (from the Codex pre-implementation review).** (a) WebSocket **Origin check** (CSWSH defense — the HTTP CSRF middleware does NOT cover WS handshakes) before auth/accept, reusing the canonical `csrf._hostport`. (b) **Durable-log-first integrity:** apply → persist → broadcast; a persist failure leaves memory ahead of the log, so the room is **poisoned and evicted** and rebuilt from the log (the CRDT client re-offers its update on reconnect). (c) **Broadcast error-isolation:** a broken client is dropped without starving others. (d) **Inbound frame-size bound** (no HTTP body-size middleware on WS frames). Malformed updates are dropped (never persisted/broadcast).
- **Test location.** All tests go under `tests/proposal_assist/collab/` (cockpit `/api`/proposal-assist tests live under `tests/proposal_assist/`). Whole-suite runs may flake on testcontainers PG under load — verify via grouped dir runs (`pytest tests/proposal_assist/collab/`).
- **Lint/format gate:** `ruff check .` must pass (CI runs `ruff check .`, not format).
- **Frontend untouched** in this slice (Tiptap is Slice 3).

---

## File Structure

- Create `src/call_manager/proposal_assist/collab/protocol.py` — thin, tested helpers over `pycrdt._sync`: message classification (`is_sync_step1`, `is_sync_write`, `is_awareness`) + `extract_update`, re-exporting the codec functions the room uses.
- Create `src/call_manager/proposal_assist/collab/store.py` — sync persistence functions `append_update` / `load_updates` over the new table.
- Create `src/call_manager/proposal_assist/collab/room.py` — socket-agnostic `Room` (holds `Doc`, clients, lock; connect/handle/disconnect) + `RoomManager` registry (get-or-create with recovery replay; evict-on-empty).
- Create `src/call_manager/proposal_assist/collab/ws_auth.py` — `user_from_cookie` (pure, reuses `SessionSigner.verify` + DB lookup/status/epoch), `resolve_ws_user(websocket)` adapter, and `resolve_access(session, ...)` permission resolver.
- Create `src/call_manager/proposal_assist/web/collab_ws.py` — the FastAPI `WebSocket` route wiring auth → permission → room → per-connection loop, with `run_in_threadpool` DB offload.
- Modify `src/call_manager/db/models.py` — add the `YjsUpdate` model.
- Create `alembic/versions/0059_yjs_update_log.py` — the migration.
- Modify `src/call_manager/main.py` — `app.include_router(collab_ws.router)` **before** `mount_spa(...)`.
- Tests: `tests/proposal_assist/collab/test_collab_protocol.py`, `test_collab_store.py`, `test_collab_room.py`, `test_collab_ws_auth.py`, `test_collab_ws_route.py`, `test_collab_recovery.py`.
- Create `docs/superpowers/notes/2026-06-30-collab-slice1-sync-server-notes.md` — what shipped + what's deferred.

**Interface summary (locked across tasks — keep signatures byte-identical):**

```
# protocol.py
def is_sync_step1(message: bytes) -> bool
def is_sync_write(message: bytes) -> bool       # SYNC_STEP2 or SYNC_UPDATE (carries doc changes)
def is_awareness(message: bytes) -> bool
def extract_update(message: bytes) -> bytes     # raw Y update payload from a SYNC write frame
# re-exported: create_sync_message, handle_sync_message, create_update_message

# store.py  (sync SQLAlchemy)
def append_update(session, *, workspace_id: uuid.UUID, artifact: str, actor_id: int | None, update: bytes) -> None
def load_updates(session, *, workspace_id: uuid.UUID, artifact: str) -> list[bytes]

# room.py
class PersistError(Exception): ...                  # durable write failed -> room poisoned -> evict
class Room:
    key: tuple[uuid.UUID, str]
    doc: pycrdt.Doc
    clients: set
    poisoned: bool
    def initial_message(self) -> bytes              # server SYNC_STEP1
    async def handle(self, *, sender, user_id: int, can_write: bool, message: bytes,
                     persist: Callable[[int, bytes], Awaitable[None]]) -> None   # raises PersistError
class RoomManager:
    async def acquire(self, key, *, load: Callable[[], Awaitable[list[bytes]]], client) -> Room
    async def release(self, key, *, client) -> None
    async def evict(self, key) -> None              # force-remove (poison recovery)

# ws_auth.py
def user_from_cookie(cookie: str | None, *, signer, session_factory) -> User   # raises WSAuthError
async def resolve_ws_user(websocket) -> User
def check_origin(websocket) -> bool                 # CSWSH defense (reuses csrf._hostport)
def resolve_access(session, *, user: User, workspace_id: uuid.UUID, artifact: str) -> tuple[bool, bool]  # (is_member, can_write)
class WSAuthError(Exception): ...
```

---

### Task 1: Y-sync protocol helpers (`protocol.py`)

**Files:**
- Create: `src/call_manager/proposal_assist/collab/protocol.py`
- Test: `tests/proposal_assist/collab/test_collab_protocol.py`

**Interfaces:**
- Consumes: `pycrdt` (`Doc`, `YMessageType`, `YSyncMessageType`, `create_sync_message`, `handle_sync_message`, `create_update_message`, `read_message`).
- Produces: `is_sync_step1`, `is_sync_write`, `is_awareness`, `extract_update`, and re-exports `create_sync_message`, `handle_sync_message`, `create_update_message`.

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/collab/test_collab_protocol.py
from pycrdt import Doc, Text

from call_manager.proposal_assist.collab import protocol as P


def _make_update_message(text: str) -> bytes:
    """A real SYNC_UPDATE wire frame produced by editing a Doc."""
    src = Doc()
    src["body"] = Text()
    sub = []
    src.observe(lambda e: sub.append(e.update))
    src["body"] += text
    # create_update_message wraps the raw update in a [SYNC][UPDATE][len] frame
    return P.create_update_message(sub[-1])


def test_classifies_step1_message():
    msg = P.create_sync_message(Doc())  # SYNC_STEP1
    assert P.is_sync_step1(msg) is True
    assert P.is_sync_write(msg) is False
    assert P.is_awareness(msg) is False


def test_classifies_update_message_as_write():
    msg = _make_update_message("hello")
    assert P.is_sync_write(msg) is True
    assert P.is_sync_step1(msg) is False


def test_extract_update_round_trips_into_a_fresh_doc():
    msg = _make_update_message("hello world")
    update = P.extract_update(msg)
    dst = Doc()
    dst["body"] = Text()
    dst.apply_update(update)
    assert str(dst["body"]) == "hello world"


def test_step1_drives_convergence_via_handle_sync_message():
    # A populated server doc replies to a fresh client's STEP1 with a STEP2 the
    # client applies to converge. Proves we understand the codec end to end.
    server = Doc()
    server["body"] = Text()
    server["body"] += "server content"
    client = Doc()
    client["body"] = Text()
    step1_from_client = P.create_sync_message(client)
    reply = P.handle_sync_message(step1_from_client[1:], server)  # -> SYNC_STEP2
    assert reply is not None
    client.apply_update(P.extract_update(reply))
    assert str(client["body"]) == "server content"


def test_empty_message_is_not_a_write():
    assert P.is_sync_write(b"") is False
    assert P.is_sync_step1(b"") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/proposal_assist/collab/test_collab_protocol.py -q`
Expected: FAIL with `ModuleNotFoundError: ...collab.protocol`.

- [ ] **Step 3: Write the implementation**

```python
# src/call_manager/proposal_assist/collab/protocol.py
"""Thin, tested helpers over the y-sync protocol codec shipped in pycrdt.

We hand-roll the sync room rather than use pycrdt.websocket, so these helpers
classify inbound wire frames (to gate writes) and pull the raw Y update out of
a SYNC write frame (to persist it with an actor tag and re-broadcast it).

Wire format (pycrdt._sync):
  SYNC frame:      [YMessageType.SYNC (0)] [YSyncMessageType] [varuint len] [payload]
  AWARENESS frame: [YMessageType.AWARENESS (1)] [varuint len] [payload]
"""

from __future__ import annotations

from pycrdt import (
    YMessageType,
    YSyncMessageType,
    create_sync_message,
    create_update_message,
    handle_sync_message,
    read_message,
)

__all__ = [
    "create_sync_message",
    "handle_sync_message",
    "create_update_message",
    "is_sync_step1",
    "is_sync_write",
    "is_awareness",
    "extract_update",
]


def is_awareness(message: bytes) -> bool:
    return len(message) >= 1 and message[0] == YMessageType.AWARENESS


def is_sync_step1(message: bytes) -> bool:
    return (
        len(message) >= 2
        and message[0] == YMessageType.SYNC
        and message[1] == YSyncMessageType.SYNC_STEP1
    )


def is_sync_write(message: bytes) -> bool:
    """True for SYNC frames that carry document changes (STEP2 or UPDATE).

    These are the frames a non-writer connection must not be allowed to apply,
    persist, or broadcast.
    """
    return (
        len(message) >= 2
        and message[0] == YMessageType.SYNC
        and message[1] in (YSyncMessageType.SYNC_STEP2, YSyncMessageType.SYNC_UPDATE)
    )


def extract_update(message: bytes) -> bytes:
    """Return the raw Y update payload from a SYNC write frame.

    Strips the 2-byte [SYNC][subtype] header and reads the varuint-prefixed
    payload. Caller is responsible for checking is_sync_write() first.
    """
    return read_message(message[2:])
```

- [ ] **Step 4: Create the package marker**

The `collab` package already exists (`src/call_manager/proposal_assist/collab/__init__.py` from Slice 0). No new `__init__` needed.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/proposal_assist/collab/test_collab_protocol.py -q`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/collab/protocol.py tests/proposal_assist/collab/test_collab_protocol.py
git commit -m "feat(collab): y-sync protocol helpers over pycrdt codec"
```

---

### Task 2: `YjsUpdate` model + migration 0059

**Files:**
- Modify: `src/call_manager/db/models.py` (add `YjsUpdate`)
- Create: `alembic/versions/0059_yjs_update_log.py`
- Test: `tests/proposal_assist/collab/test_collab_store.py` (model-insert portion; store fns added in Task 3)

**Interfaces:**
- Produces: ORM `YjsUpdate` (table `yjs_updates`), used by `store.py`.

Append-only update log. Uses a `BigInteger` autoincrement PK for natural insertion ordering (intentional deviation from the repo's UUID-PK convention — an ordered log wants a monotonic key, not a random UUID). Composite index on `(workspace_id, artifact, id)` for per-room ordered reads.

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/collab/test_collab_store.py
import uuid

from call_manager.db.models import YjsUpdate
from tests.proposal_assist.conftest import make_user, make_workspace


def test_yjs_update_row_roundtrips_and_orders_by_id(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s, email="yjs-owner@ki.se")
        ws = make_workspace(s, owner=owner)
        s.add(YjsUpdate(workspace_id=ws.id, artifact="draft", actor_id=owner.id, update_bytes=b"\x01")) 
        s.add(YjsUpdate(workspace_id=ws.id, artifact="draft", actor_id=owner.id, update_bytes=b"\x02"))
        s.commit()
        wid = ws.id
    with pg_session_factory() as s:
        from sqlalchemy import select

        rows = (
            s.execute(
                select(YjsUpdate)
                .where(YjsUpdate.workspace_id == wid, YjsUpdate.artifact == "draft")
                .order_by(YjsUpdate.id)
            )
            .scalars()
            .all()
        )
        assert [r.update_bytes for r in rows] == [b"\x01", b"\x02"]
        assert rows[0].id < rows[1].id
        assert rows[0].actor_id is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/proposal_assist/collab/test_collab_store.py -q`
Expected: FAIL with `ImportError: cannot import name 'YjsUpdate'`.

- [ ] **Step 3: Add the model**

Add to `src/call_manager/db/models.py` (near the other proposal-assist models; follow the file's existing `Mapped`/`mapped_column` style — match whichever declarative form the surrounding models use):

```python
class YjsUpdate(Base):
    """Append-only Yjs update log for the collaborative editor.

    One row per persisted CRDT update, keyed by (workspace_id, artifact) =
    the room. `actor_id` is the authenticated user who sent the update (for
    authorship in later slices). Ordering is by the autoincrement `id`.
    """

    __tablename__ = "yjs_updates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    update_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("artifact IN ('draft','spec')", name="ck_yjs_updates_artifact"),
        Index("ix_yjs_updates_room", "workspace_id", "artifact", "id"),
    )
```

Ensure the imports used here (`BigInteger`, `Text`, `LargeBinary`, `DateTime`, `ForeignKey`, `CheckConstraint`, `Index`, `func`, `UUID`, `datetime`, `uuid`) are already imported at the top of `models.py`; add any missing ones to the existing import block (most are already present — `LargeBinary` likely is not).

- [ ] **Step 4: Write the migration**

```python
# alembic/versions/0059_yjs_update_log.py
"""yjs update log

Revision ID: 0059
Revises: 0058

Append-only Yjs CRDT update log for the collaborative editor (Slice 1).
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "yjs_updates",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("artifact", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("update_bytes", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("artifact IN ('draft','spec')", name="ck_yjs_updates_artifact"),
    )
    op.create_index("ix_yjs_updates_room", "yjs_updates", ["workspace_id", "artifact", "id"])


def downgrade() -> None:
    op.drop_index("ix_yjs_updates_room", table_name="yjs_updates")
    op.drop_table("yjs_updates")
```

- [ ] **Step 5: Run the test (migration applied by the testcontainer fixture)**

Run: `uv run pytest tests/proposal_assist/collab/test_collab_store.py -q`
Expected: PASS (the `pg_session_factory`/`migrated_engine` fixture runs Alembic to head, creating `yjs_updates`).

- [ ] **Step 6: Verify migration round-trips (defense for the downgrade)**

Run: `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: no errors. (If no local DB is configured for the CLI, skip — the testcontainer fixture already proved `upgrade`; note the skip.)

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/db/models.py alembic/versions/0059_yjs_update_log.py tests/proposal_assist/collab/test_collab_store.py
git commit -m "feat(collab): yjs_updates append-only log (model + migration 0059)"
```

---

### Task 3: Persistence functions (`store.py`)

**Files:**
- Create: `src/call_manager/proposal_assist/collab/store.py`
- Test: extend `tests/proposal_assist/collab/test_collab_store.py`

**Interfaces:**
- Consumes: `YjsUpdate` (Task 2).
- Produces: `append_update(session, *, workspace_id, artifact, actor_id, update)`, `load_updates(session, *, workspace_id, artifact) -> list[bytes]`.

- [ ] **Step 1: Write the failing test (append below the Task-2 test)**

```python
# tests/proposal_assist/collab/test_collab_store.py  (add)
from call_manager.proposal_assist.collab import store


def test_append_and_load_updates_are_room_scoped_and_ordered(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s, email="store-owner@ki.se")
        ws = make_workspace(s, owner=owner)
        wid = ws.id
        uid = owner.id
        s.commit()

    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=uid, update=b"a")
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=uid, update=b"b")
        store.append_update(s, workspace_id=wid, artifact="spec", actor_id=uid, update=b"z")
        s.commit()

    with pg_session_factory() as s:
        assert store.load_updates(s, workspace_id=wid, artifact="draft") == [b"a", b"b"]
        assert store.load_updates(s, workspace_id=wid, artifact="spec") == [b"z"]


def test_load_updates_empty_room_returns_empty_list(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s, email="empty-room@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        assert store.load_updates(s, workspace_id=ws.id, artifact="draft") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/proposal_assist/collab/test_collab_store.py -q`
Expected: FAIL with `ModuleNotFoundError: ...collab.store`.

- [ ] **Step 3: Write the implementation**

```python
# src/call_manager/proposal_assist/collab/store.py
"""Sync persistence for the Yjs update log (the room's durable truth).

These run on a worker thread (the WS handler offloads them via
run_in_threadpool), so they take an ordinary sync Session.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import YjsUpdate


def append_update(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    artifact: str,
    actor_id: int | None,
    update: bytes,
) -> None:
    """Append one Yjs update for a room, tagged with the sending user."""
    session.add(
        YjsUpdate(
            workspace_id=workspace_id,
            artifact=artifact,
            actor_id=actor_id,
            update_bytes=update,
        )
    )
    session.commit()


def load_updates(session: Session, *, workspace_id: uuid.UUID, artifact: str) -> list[bytes]:
    """All persisted updates for a room, in insertion order (for replay)."""
    rows = (
        session.execute(
            select(YjsUpdate.update_bytes)
            .where(YjsUpdate.workspace_id == workspace_id, YjsUpdate.artifact == artifact)
            .order_by(YjsUpdate.id)
        )
        .scalars()
        .all()
    )
    return list(rows)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/proposal_assist/collab/test_collab_store.py -q`
Expected: PASS (4 tests in the file now).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/collab/store.py tests/proposal_assist/collab/test_collab_store.py
git commit -m "feat(collab): yjs update-log persistence (append/load)"
```

---

### Task 4: Room + RoomManager (`room.py`) — convergence, write-gate, recovery

**Files:**
- Create: `src/call_manager/proposal_assist/collab/room.py`
- Test: `tests/proposal_assist/collab/test_collab_room.py`

**Interfaces:**
- Consumes: `protocol.py` (Task 1), `pycrdt.Doc`.
- Produces: `Room` (socket-agnostic) + `RoomManager`. The room talks to clients via a duck-typed object exposing `async send_bytes(data: bytes)`; persistence and recovery-load are **injected callables** (so this layer is DB-free and unit-testable).

This is the GO-critical task: it proves convergence + the per-update write gate + recovery, with **no sockets and no DB** (fakes for both). The real socket/DB wiring is Task 6.

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/collab/test_collab_room.py
import uuid

import pytest
from pycrdt import Doc, Text

from call_manager.proposal_assist.collab import protocol as P
from call_manager.proposal_assist.collab.room import Room, RoomManager


class FakeClient:
    """Stand-in for a connected WebSocket; records what the room sends it."""

    def __init__(self):
        self.sent: list[bytes] = []

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)


def _update_frame(text: str) -> bytes:
    """Return a SYNC_UPDATE wire frame for inserting `text` into a fresh doc."""
    d = Doc()
    d["body"] = Text()
    captured: list[bytes] = []
    sub = d.observe(lambda e: captured.append(e.update))
    d["body"] += text
    d.unobserve(sub)
    return P.create_update_message(captured[-1])


def _persist_sink():
    log: list[tuple[int, bytes]] = []

    async def persist(user_id: int, update: bytes) -> None:
        log.append((user_id, update))

    return log, persist


@pytest.mark.anyio
async def test_writer_update_is_applied_persisted_and_broadcast_to_others():
    key = (uuid.uuid4(), "draft")
    room = Room(key)
    writer, reader = FakeClient(), FakeClient()
    room.clients.update({writer, reader})
    log, persist = _persist_sink()

    frame = _update_frame("hello")

    await room.handle(sender=writer, user_id=7, can_write=True, message=frame, persist=persist)

    # applied to the room doc
    assert "hello" in str(room.doc.get("body", type=Text))
    # persisted with the writer's user_id
    assert len(log) == 1 and log[0][0] == 7
    # broadcast to the OTHER client only (not echoed to sender)
    assert reader.sent and not writer.sent


@pytest.mark.anyio
async def test_non_writer_update_is_dropped_not_persisted_not_broadcast():
    key = (uuid.uuid4(), "draft")
    room = Room(key)
    bad, other = FakeClient(), FakeClient()
    room.clients.update({bad, other})
    log, persist = _persist_sink()

    frame = _update_frame("nope")

    await room.handle(sender=bad, user_id=9, can_write=False, message=frame, persist=persist)

    assert log == []                      # not persisted
    assert other.sent == []               # not broadcast
    assert "nope" not in str(room.doc.get("body", type=Text))  # not applied


@pytest.mark.anyio
async def test_step1_is_answered_for_any_client_even_read_only():
    key = (uuid.uuid4(), "draft")
    room = Room(key)
    # seed the room with content
    room.doc["body"] = Text()
    room.doc["body"] += "seeded"
    reader = FakeClient()
    room.clients.add(reader)
    log, persist = _persist_sink()

    client_doc = Doc()
    client_doc["body"] = Text()
    step1 = P.create_sync_message(client_doc)

    await room.handle(sender=reader, user_id=3, can_write=False, message=step1, persist=persist)

    # the read path is always allowed: reader received a STEP2 it can apply
    assert reader.sent
    client_doc.apply_update(P.extract_update(reader.sent[-1]))
    assert "seeded" in str(client_doc["body"])
    assert log == []  # answering STEP1 is not a write


@pytest.mark.anyio
async def test_two_clients_converge_through_the_room():
    key = (uuid.uuid4(), "draft")
    room = Room(key)
    a, b = FakeClient(), FakeClient()
    room.clients.update({a, b})
    _, persist = _persist_sink()

    db = Doc()
    db["body"] = Text()
    frame = _update_frame("from A")

    await room.handle(sender=a, user_id=1, can_write=True, message=frame, persist=persist)
    # b receives the broadcast and applies it
    db.apply_update(P.extract_update(b.sent[-1]))
    assert str(db["body"]) == "from A"


@pytest.mark.anyio
async def test_manager_replays_persisted_updates_on_cold_acquire():
    # build a populated doc, capture its updates as the "persisted" log
    seed = Doc()
    seed["body"] = Text()
    captured: list[bytes] = []
    sub = seed.observe(lambda e: captured.append(e.update))
    seed["body"] += "recovered state"
    seed.unobserve(sub)

    async def load():
        return list(captured)

    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    client = FakeClient()
    room = await mgr.acquire(key, load=load, client=client)
    assert "recovered state" in str(room.doc.get("body", type=Text))
    assert client in room.clients


@pytest.mark.anyio
async def test_manager_evicts_room_when_last_client_leaves():
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    c1, c2 = FakeClient(), FakeClient()

    async def load():
        return []

    await mgr.acquire(key, load=load, client=c1)
    await mgr.acquire(key, load=load, client=c2)  # same room, second client
    await mgr.release(key, client=c1)
    assert mgr.has_room(key)            # still one client
    await mgr.release(key, client=c2)
    assert not mgr.has_room(key)        # evicted


@pytest.mark.anyio
async def test_persist_failure_poisons_room_and_does_not_broadcast():
    key = (uuid.uuid4(), "draft")
    room = Room(key)
    writer, other = FakeClient(), FakeClient()
    room.clients.update({writer, other})

    async def persist_boom(user_id, update):
        raise RuntimeError("db down")

    from call_manager.proposal_assist.collab.room import PersistError

    frame = _update_frame("doomed")
    with pytest.raises(PersistError):
        await room.handle(
            sender=writer, user_id=1, can_write=True, message=frame, persist=persist_boom
        )
    assert room.poisoned is True
    assert other.sent == []   # persist failed before broadcast -> not published


@pytest.mark.anyio
async def test_malformed_update_is_dropped_without_persist_or_broadcast():
    # A SYNC_UPDATE frame whose payload is not a valid Y update: apply raises,
    # the room drops it (no persist, no broadcast, no exception out of handle).
    key = (uuid.uuid4(), "draft")
    room = Room(key)
    writer, other = FakeClient(), FakeClient()
    room.clients.update({writer, other})
    log, persist = _persist_sink()

    garbage = P.create_update_message(b"\xde\xad\xbe\xef\xde\xad")  # not a real update
    await room.handle(
        sender=writer, user_id=1, can_write=True, message=garbage, persist=persist
    )
    assert log == []
    assert other.sent == []


@pytest.mark.anyio
async def test_broadcast_isolates_a_failing_client():
    class BoomClient(FakeClient):
        async def send_bytes(self, data):
            raise RuntimeError("socket closed")

    key = (uuid.uuid4(), "draft")
    room = Room(key)
    writer, good, boom = FakeClient(), FakeClient(), BoomClient()
    room.clients.update({writer, good, boom})
    _, persist = _persist_sink()

    await room.handle(
        sender=writer, user_id=1, can_write=True, message=_update_frame("x"), persist=persist
    )
    assert good.sent                  # healthy client still received the update
    assert boom not in room.clients   # broken client dropped from the room
```

Note: the test module needs `anyio` mode. If the repo's `tests/conftest.py` does not already set `anyio_backend`, add a local fixture at the top of this test file:

```python
@pytest.fixture
def anyio_backend():
    return "asyncio"
```

(Check `tests/conftest.py` first; only add if absent, to avoid a duplicate-fixture shadow.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/proposal_assist/collab/test_collab_room.py -q`
Expected: FAIL with `ModuleNotFoundError: ...collab.room`.

- [ ] **Step 3: Write the implementation**

```python
# src/call_manager/proposal_assist/collab/room.py
"""In-process Yjs rooms (socket-agnostic).

A Room owns one pycrdt.Doc, the set of connected clients, and an asyncio lock
serialising mutations. It is deliberately free of FastAPI, sockets, and the DB:
clients are any object with `async send_bytes(bytes)`, and persistence/recovery
are injected callables. The WS route (Task 6) supplies real sockets and a
Postgres-backed persist/load.

Single-instance constraint: the module-global RoomManager IS the room registry;
all clients of a room reach the same process, so in-memory state is canonical
for the live doc and Postgres is the durable replay log.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable

from pycrdt import Doc

from . import protocol as P

RoomKey = tuple[uuid.UUID, str]
PersistFn = Callable[[int, bytes], Awaitable[None]]
LoadFn = Callable[[], Awaitable[list[bytes]]]

# An empty Yjs update is two zero bytes; nothing to apply/persist/broadcast.
_EMPTY_UPDATE = b"\x00\x00"


logger = logging.getLogger(__name__)


class PersistError(Exception):
    """Durable persistence of an applied update failed.

    The in-memory doc is now AHEAD of the durable log, so the room is poisoned
    and must be evicted and rebuilt from the log (the route does this). The
    sender's CRDT client re-offers its update on reconnect, so no data is lost.
    """


class Room:
    def __init__(self, key: RoomKey) -> None:
        self.key = key
        self.doc = Doc()
        self.clients: set = set()
        self.poisoned = False
        self._lock = asyncio.Lock()

    def initial_message(self) -> bytes:
        """The SYNC_STEP1 the server sends a client on connect."""
        return P.create_sync_message(self.doc)

    async def handle(
        self,
        *,
        sender,
        user_id: int,
        can_write: bool,
        message: bytes,
        persist: PersistFn,
    ) -> None:
        if P.is_sync_step1(message):
            # Read path: always answer with our state (a SYNC_STEP2 reply).
            reply = P.handle_sync_message(message[1:], self.doc)
            if reply is not None:
                await sender.send_bytes(reply)
            return

        if P.is_sync_write(message):
            if not can_write:
                return  # silently drop — non-writer cannot mutate the room
            update = P.extract_update(message)
            if not update or update == _EMPTY_UPDATE:
                return
            async with self._lock:
                # Apply FIRST so we never persist an update that won't replay
                # (keeps the durable log clean by construction). apply_update is
                # atomic: a malformed update raises without mutating the doc.
                try:
                    self.doc.apply_update(update)
                except Exception:
                    logger.warning(
                        "collab.malformed_update",
                        extra={"event": "collab_malformed_update", "room": str(self.key)},
                    )
                    return  # drop malformed: not persisted, not broadcast
                # Then persist DURABLY. If this fails the doc is ahead of the
                # log: poison the room so it is evicted and rebuilt from the log.
                try:
                    await persist(user_id, update)
                except Exception as e:
                    self.poisoned = True
                    raise PersistError(str(self.key)) from e
            # Broadcast outside the lock (CRDT tolerates reordering; a slow
            # client must not block other writers).
            await self._broadcast(P.create_update_message(update), exclude=sender)
            return

        # AWARENESS and anything else: ignored in Slice 1 (presence is Slice 3).
        return

    async def _broadcast(self, frame: bytes, *, exclude) -> None:
        """Send to every client except `exclude`, isolating per-client failures:
        a broken/closed socket is dropped and the rest still receive the frame."""
        dead = []
        for client in list(self.clients):
            if client is exclude:
                continue
            try:
                await client.send_bytes(frame)
            except Exception:
                dead.append(client)
        for client in dead:
            self.clients.discard(client)


class RoomManager:
    def __init__(self) -> None:
        self._rooms: dict[RoomKey, Room] = {}
        self._lock = asyncio.Lock()

    def has_room(self, key: RoomKey) -> bool:
        return key in self._rooms

    async def acquire(self, key: RoomKey, *, load: LoadFn, client) -> Room:
        """Get-or-create the room, replaying persisted updates on cold start,
        and register `client`."""
        async with self._lock:
            room = self._rooms.get(key)
            if room is None:
                room = Room(key)
                for update in await load():
                    room.doc.apply_update(update)
                self._rooms[key] = room
            room.clients.add(client)
            return room

    async def release(self, key: RoomKey, *, client) -> None:
        """Deregister a client; evict the room when the last one leaves."""
        async with self._lock:
            room = self._rooms.get(key)
            if room is None:
                return
            room.clients.discard(client)
            if not room.clients:
                del self._rooms[key]

    async def evict(self, key: RoomKey) -> None:
        """Force-remove a room regardless of connected clients (poison recovery).
        The next acquire rebuilds it from the durable log."""
        async with self._lock:
            self._rooms.pop(key, None)


# Module-global registry (single-instance backend).
MANAGER = RoomManager()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/proposal_assist/collab/test_collab_room.py -q`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/collab/room.py tests/proposal_assist/collab/test_collab_room.py
git commit -m "feat(collab): in-process Yjs room + manager (gate, broadcast, recovery)"
```

---

### Task 5: WebSocket auth + permission resolver (`ws_auth.py`)

**Files:**
- Create: `src/call_manager/proposal_assist/collab/ws_auth.py`
- Test: `tests/proposal_assist/collab/test_collab_ws_auth.py`

**Interfaces:**
- Consumes: `SessionSigner`/`InvalidSession` (`auth/session_cookie.py`), `User`/`Workspace`/`WorkspaceViewer` models, `_perms` patterns.
- Produces: `user_from_cookie(cookie, *, signer, session_factory) -> User` (raises `WSAuthError`), `resolve_ws_user(websocket) -> User`, `resolve_access(session, *, user, workspace_id, artifact) -> (is_member, can_write)`, `WSAuthError`.

`user_from_cookie` mirrors `get_current_user`'s verification exactly (signer.verify → DB get → `status == "active"` → `session_epoch` match) but is pure (no `Request`), so it's unit-testable and reusable from a WS context. Permission rules (connect-time):
- not owner and not viewer ⇒ no access (`is_member=False`); the route rejects the connection.
- `current_phase == "setup"` ⇒ `can_write = is_owner` (setup is owner-only, both artifacts).
- `artifact == "spec"` and `spec_locked_at is not None` ⇒ `can_write = is_owner`.
- otherwise (active phase; draft, or unlocked spec) ⇒ `can_write = is_member`.

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/collab/test_collab_ws_auth.py
import uuid

import pytest

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import User, WorkspaceViewer
from call_manager.proposal_assist.collab.ws_auth import (
    WSAuthError,
    resolve_access,
    user_from_cookie,
)
from tests.proposal_assist.conftest import make_user, make_workspace


def _signer() -> SessionSigner:
    return SessionSigner(secret="test-secret-at-least-16-chars")


def test_user_from_cookie_valid(pg_session_factory):
    with pg_session_factory() as s:
        u = make_user(s, email="wsauth-ok@ki.se")
        s.commit()
        uid = u.id
    signer = _signer()
    cookie = signer.make(user_id=uid)
    user = user_from_cookie(cookie, signer=signer, session_factory=pg_session_factory)
    assert user.id == uid


def test_user_from_cookie_missing_or_bad_raises(pg_session_factory):
    signer = _signer()
    with pytest.raises(WSAuthError):
        user_from_cookie(None, signer=signer, session_factory=pg_session_factory)
    with pytest.raises(WSAuthError):
        user_from_cookie("garbage", signer=signer, session_factory=pg_session_factory)


def test_user_from_cookie_rejects_inactive_and_stale_epoch(pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="disabled", email="wsauth-disabled@ki.se")
        s.add(u)
        s.commit()
        uid = u.id
    signer = _signer()
    with pytest.raises(WSAuthError):
        user_from_cookie(signer.make(user_id=uid), signer=signer, session_factory=pg_session_factory)

    # active but epoch bumped after the cookie was minted
    with pg_session_factory() as s:
        u = make_user(s, email="wsauth-epoch@ki.se")
        s.commit()
        uid = u.id
    cookie = signer.make(user_id=uid, session_epoch=0)
    with pg_session_factory() as s:
        s.get(User, uid).session_epoch = 5
        s.commit()
    with pytest.raises(WSAuthError):
        user_from_cookie(cookie, signer=signer, session_factory=pg_session_factory)


def _add_viewer(session, ws, user):
    session.add(WorkspaceViewer(workspace_id=ws.id, user_id=user.id))
    session.commit()


def test_access_matrix(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s, email="acc-owner@ki.se")
        member = make_user(s, email="acc-member@ki.se")
        stranger = make_user(s, email="acc-stranger@ki.se")
        ws_active = make_workspace(s, owner=owner, current_phase="active")
        _add_viewer(s, ws_active, member)
        s.commit()
        owner_id, member_id, stranger_id, wid = owner.id, member.id, stranger.id, ws_active.id

    with pg_session_factory() as s:
        owner = s.get(User, owner_id)
        member = s.get(User, member_id)
        stranger = s.get(User, stranger_id)

        # active draft: owner & member write, stranger has no access
        assert resolve_access(s, user=owner, workspace_id=wid, artifact="draft") == (True, True)
        assert resolve_access(s, user=member, workspace_id=wid, artifact="draft") == (True, True)
        assert resolve_access(s, user=stranger, workspace_id=wid, artifact="draft") == (False, False)


def test_access_setup_phase_is_owner_only(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s, email="setup-owner@ki.se")
        member = make_user(s, email="setup-member@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="setup")
        _add_viewer(s, ws, member)
        s.commit()
        oid, mid, wid = owner.id, member.id, ws.id
    with pg_session_factory() as s:
        owner, member = s.get(User, oid), s.get(User, mid)
        assert resolve_access(s, user=owner, workspace_id=wid, artifact="draft") == (True, True)
        assert resolve_access(s, user=member, workspace_id=wid, artifact="draft") == (True, False)


def test_access_locked_spec_is_owner_only(pg_session_factory):
    from datetime import UTC, datetime

    with pg_session_factory() as s:
        owner = make_user(s, email="lock-owner@ki.se")
        member = make_user(s, email="lock-member@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        _add_viewer(s, ws, member)
        ws.spec_locked_at = datetime.now(UTC)
        s.commit()
        oid, mid, wid = owner.id, member.id, ws.id
    with pg_session_factory() as s:
        owner, member = s.get(User, oid), s.get(User, mid)
        # locked spec: member read-only, owner still writes
        assert resolve_access(s, user=member, workspace_id=wid, artifact="spec") == (True, False)
        assert resolve_access(s, user=owner, workspace_id=wid, artifact="spec") == (True, True)
        # draft unaffected by spec lock
        assert resolve_access(s, user=member, workspace_id=wid, artifact="draft") == (True, True)


class _FakeWS:
    def __init__(self, origin, base_url="http://testserver"):
        self.headers = {} if origin is None else {"origin": origin}

        class _State:
            pass

        class _Settings:
            app_base_url = base_url

        self.app = type("A", (), {})()
        self.app.state = _State()
        self.app.state.settings = _Settings()


def test_check_origin_matrix():
    from call_manager.proposal_assist.collab.ws_auth import check_origin

    assert check_origin(_FakeWS("http://testserver")) is True      # same origin
    assert check_origin(_FakeWS(None)) is True                     # non-browser (no Origin)
    assert check_origin(_FakeWS("http://evil.example")) is False   # cross-site
    assert check_origin(_FakeWS("null")) is False                  # sandboxed/opaque
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/proposal_assist/collab/test_collab_ws_auth.py -q`
Expected: FAIL with `ModuleNotFoundError: ...collab.ws_auth`.

- [ ] **Step 3: Write the implementation**

```python
# src/call_manager/proposal_assist/collab/ws_auth.py
"""WebSocket-context auth + connect-time permission resolution.

The HTTP get_current_user dependency is Request/Cookie-bound and cannot run in
a WebSocket handshake. user_from_cookie() factors out the verification so both
the WS adapter here and tests can call it directly. resolve_access() computes
(is_member, can_write) once at connect; mid-session re-evaluation is Slice 2.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.auth.session_cookie import InvalidSession, SessionSigner
from call_manager.db.models import User, Workspace, WorkspaceViewer


class WSAuthError(Exception):
    """Connection cannot be authenticated/authorized; the route closes the socket."""


def user_from_cookie(cookie: str | None, *, signer: SessionSigner, session_factory) -> User:
    """Resolve the authenticated User from a raw session cookie value.

    Mirrors get_current_user's checks (verify -> active status -> epoch match)
    but is Request-free so it works from a WebSocket. Raises WSAuthError on any
    failure (the WS layer maps that to a close, not an HTTP status).
    """
    if not cookie:
        raise WSAuthError("no session cookie")
    try:
        data = signer.verify(cookie)
    except InvalidSession as e:
        raise WSAuthError("invalid session") from e
    with session_factory() as db:
        user = db.get(User, data.user_id)
        if user is None:
            raise WSAuthError("user not found")
        if user.status != "active":
            raise WSAuthError("account inactive")
        if user.session_epoch != data.session_epoch:
            raise WSAuthError("session revoked")
        db.expunge(user)
    return user


async def resolve_ws_user(websocket) -> User:
    """Adapter: read the cookie + app state off the WebSocket and resolve a User.

    Runs the sync DB lookup on a worker thread to avoid blocking the event loop.
    """
    from starlette.concurrency import run_in_threadpool

    cookie = websocket.cookies.get("session")
    signer: SessionSigner = websocket.app.state.session_signer
    session_factory = websocket.app.state.session_factory
    return await run_in_threadpool(
        user_from_cookie, cookie, signer=signer, session_factory=session_factory
    )


def check_origin(websocket) -> bool:
    """Cross-site WebSocket hijacking (CSWSH) defense.

    The HTTP CSRF middleware does not protect WS handshakes (the upgrade is a
    GET — a safe method — so it passes straight through). A cookie-authenticated
    WS would otherwise be a CSRF write hole: a malicious page could open
    ws(s)://our-app/... and the browser would attach the victim's session cookie.

    Mirror the CSRF middleware's Origin semantics EXACTLY by reusing its
    canonical host normalizer (single source of truth — do not reimplement
    origin parsing): reject a present-but-mismatched Origin (and `null`); allow
    a matching Origin; allow an ABSENT Origin (non-browser clients — TestClient,
    server-to-server, our pycrdt test clients — don't send one, and a browser
    always does on a cross-site handshake, so absence is not the attack vector).
    """
    from call_manager.auth.csrf import _hostport

    expected = _hostport(websocket.app.state.settings.app_base_url)
    if not expected:
        return True
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    if origin == "null":
        return False
    return _hostport(origin) == expected


def resolve_access(
    session: Session, *, user: User, workspace_id: uuid.UUID, artifact: str
) -> tuple[bool, bool]:
    """Return (is_member, can_write) for this user on this (workspace, artifact).

    is_member: owner or a workspace_viewers row. Non-members get no access.
    can_write (connect-time):
      - setup phase            -> owner only
      - locked spec            -> owner only
      - else (active draft /
        unlocked spec)         -> any member
    """
    ws = session.get(Workspace, workspace_id)
    if ws is None or ws.soft_deleted_at is not None:
        return (False, False)
    is_owner = ws.owner_id == user.id
    is_viewer = (
        session.execute(
            select(WorkspaceViewer.user_id).where(
                WorkspaceViewer.workspace_id == workspace_id,
                WorkspaceViewer.user_id == user.id,
            )
        ).first()
        is not None
    )
    is_member = is_owner or is_viewer
    if not is_member:
        return (False, False)

    if ws.current_phase == "setup":
        return (True, is_owner)
    if artifact == "spec" and ws.spec_locked_at is not None:
        return (True, is_owner)
    return (True, True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/proposal_assist/collab/test_collab_ws_auth.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/collab/ws_auth.py tests/proposal_assist/collab/test_collab_ws_auth.py
git commit -m "feat(collab): websocket auth + connect-time permission resolver"
```

---

### Task 6: FastAPI WebSocket route (`collab_ws.py`) + mount

**Files:**
- Create: `src/call_manager/proposal_assist/web/collab_ws.py`
- Modify: `src/call_manager/main.py` (include the router before `mount_spa`)
- Test: `tests/proposal_assist/collab/test_collab_ws_route.py`

**Interfaces:**
- Consumes: `resolve_ws_user`, `resolve_access` (Task 5); `MANAGER`, `Room` (Task 4); `store.append_update`/`load_updates` (Task 3); `protocol` (Task 1).
- Produces: `router` with `@router.websocket("/api/ws/workspaces/{workspace_id}/{artifact}")`.

Connection lifecycle: resolve user (close `1008` on `WSAuthError`) → `accept()` → resolve access on a worker thread (close `1008` if not member; cache `can_write`) → `MANAGER.acquire` with a DB-backed `load` → send `room.initial_message()` (SYNC_STEP1) → loop over `iter_bytes()` delegating to `room.handle` with a `persist` closure that offloads `append_update` → on disconnect, `MANAGER.release`.

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/collab/test_collab_ws_route.py
import uuid

from pycrdt import Doc, Text
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import User, WorkspaceViewer, YjsUpdate
from call_manager.proposal_assist.collab import protocol as P
from tests.proposal_assist.conftest import make_user, make_workspace


def _cookie(app, user_id: int) -> str:
    signer: SessionSigner = app.state.session_signer
    return signer.make(user_id=user_id)


def _update_frame(text: str) -> bytes:
    d = Doc()
    d["body"] = Text()
    cap: list[bytes] = []
    sub = d.observe(lambda e: cap.append(e.update))
    d["body"] += text
    d.unobserve(sub)
    return P.create_update_message(cap[-1])


def test_unauthenticated_ws_is_rejected(_app, pg_session_factory):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="route-noauth@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wid = ws.id
    client = TestClient(_app)
    client.__enter__()
    try:
        import pytest

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/api/ws/workspaces/{wid}/draft") as wsconn:
                wsconn.receive_bytes()
    finally:
        client.__exit__(None, None, None)


def test_owner_can_connect_and_persist_an_update(_app, pg_session_factory):
    from fastapi.testclient import TestClient

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="route-owner@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        s.commit()
        wid, oid = ws.id, owner.id
    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        with client.websocket_connect(f"/api/ws/workspaces/{wid}/draft") as wsconn:
            first = wsconn.receive_bytes()          # server SYNC_STEP1
            assert P.is_sync_step1(first)
            wsconn.send_bytes(_update_frame("owner edit"))
            # allow the server to apply+persist
        # after the context closes, the update is durable + actor-tagged
    finally:
        client.__exit__(None, None, None)
    with pg_session_factory() as s:
        rows = s.execute(select(YjsUpdate).where(YjsUpdate.workspace_id == wid)).scalars().all()
        assert len(rows) == 1
        assert rows[0].actor_id == oid
        # and the bytes reconstruct the edit
        d = Doc()
        d["body"] = Text()
        d.apply_update(rows[0].update_bytes)
        assert "owner edit" in str(d["body"])


def test_non_member_connection_is_rejected(_app, pg_session_factory):
    import pytest
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="route-owner2@ki.se")
        stranger = make_user(s, email="route-stranger@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wid, sid = ws.id, stranger.id
    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, sid))
    try:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/api/ws/workspaces/{wid}/draft") as wsconn:
                wsconn.receive_bytes()
    finally:
        client.__exit__(None, None, None)


def test_read_only_member_update_is_not_persisted(_app, pg_session_factory):
    # member on a SETUP-phase workspace is read-only for the draft
    from fastapi.testclient import TestClient

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="ro-owner@ki.se")
        member = make_user(s, email="ro-member@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="setup")
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=member.id))
        s.commit()
        wid, mid = ws.id, member.id
    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, mid))
    try:
        with client.websocket_connect(f"/api/ws/workspaces/{wid}/draft") as wsconn:
            assert P.is_sync_step1(wsconn.receive_bytes())  # can still read
            wsconn.send_bytes(_update_frame("should be dropped"))
    finally:
        client.__exit__(None, None, None)
    with pg_session_factory() as s:
        rows = s.execute(select(YjsUpdate).where(YjsUpdate.workspace_id == wid)).scalars().all()
        assert rows == []  # read-only write was dropped


def test_invalid_artifact_is_rejected(_app, pg_session_factory):
    import pytest
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="bad-art@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wid, oid = ws.id, owner.id
    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/api/ws/workspaces/{wid}/notes") as wsconn:
                wsconn.receive_bytes()
    finally:
        client.__exit__(None, None, None)


def _recv_until_write(wsconn, tries: int = 10) -> bytes:
    """Read frames until a SYNC write (STEP2/UPDATE) arrives; the server may
    send a STEP1 first."""
    for _ in range(tries):
        frame = wsconn.receive_bytes()
        if P.is_sync_write(frame):
            return frame
    raise AssertionError("no SYNC write frame received")


def test_foreign_origin_is_rejected(_app, pg_session_factory):
    import pytest
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="origin-owner@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wid, oid = ws.id, owner.id
    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                f"/api/ws/workspaces/{wid}/draft", headers={"origin": "http://evil.example"}
            ) as wsconn:
                wsconn.receive_bytes()
    finally:
        client.__exit__(None, None, None)


def test_oversized_frame_closes_the_connection(_app, pg_session_factory):
    import pytest
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="big-owner@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        s.commit()
        wid, oid = ws.id, owner.id
    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        with client.websocket_connect(f"/api/ws/workspaces/{wid}/draft") as wsconn:
            wsconn.receive_bytes()  # STEP1
            wsconn.send_bytes(b"\x00" * 1_000_001)  # over _MAX_FRAME_BYTES
            with pytest.raises(WebSocketDisconnect):
                wsconn.receive_bytes()
    finally:
        client.__exit__(None, None, None)


def test_two_connections_converge_through_the_route(_app, pg_session_factory):
    # Both connections use the SAME authenticated owner so they share one
    # TestClient event loop (two TestClients = two loops sharing an asyncio.Lock
    # = undefined). This proves the route's apply+broadcast path A->B; multi-user
    # authorization is covered by the ws_auth tests, and two-distinct-client
    # convergence by the socket-free room test.
    from fastapi.testclient import TestClient

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="conv-owner@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        s.commit()
        wid, oid = ws.id, owner.id
    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    url = f"/api/ws/workspaces/{wid}/draft"
    try:
        with client.websocket_connect(url) as a, client.websocket_connect(url) as b:
            a.receive_bytes()  # A's STEP1 (drain)
            b.receive_bytes()  # B's STEP1 (drain)
            a.send_bytes(_update_frame("shared edit"))
            frame = _recv_until_write(b)
            mirror = Doc()
            mirror["body"] = Text()
            mirror.apply_update(P.extract_update(frame))
            assert "shared edit" in str(mirror.get("body", type=Text))
    finally:
        client.__exit__(None, None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/proposal_assist/collab/test_collab_ws_route.py -q`
Expected: FAIL (route not mounted → connection rejected/404 in unexpected ways, or import error).

- [ ] **Step 3: Write the route**

```python
# src/call_manager/proposal_assist/web/collab_ws.py
"""Collaborative-editing WebSocket sync endpoint (Slice 1).

One room per (workspace, artifact). Connection-time cookie auth + cached
write-permission; per-message write gating; Postgres-persisted, actor-tagged
update log; restart recovery by replay. No git, no awareness/presence yet.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, WebSocket
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from call_manager.proposal_assist.collab import store
from call_manager.proposal_assist.collab.room import MANAGER, PersistError
from call_manager.proposal_assist.collab.ws_auth import (
    WSAuthError,
    check_origin,
    resolve_access,
    resolve_ws_user,
)

router = APIRouter()

_VALID_ARTIFACTS = {"draft", "spec"}
_WS_POLICY_VIOLATION = 1008  # RFC 6455: policy violation
_WS_MESSAGE_TOO_BIG = 1009  # RFC 6455: message too big
_WS_INTERNAL_ERROR = 1011  # RFC 6455: server error
# Cap a single inbound frame. A proposal doc is small; legitimate CRDT updates
# are far under this. HTTP body-size middleware does NOT protect WS frames, so
# bound them here to deny memory-exhaustion / abusive payloads.
_MAX_FRAME_BYTES = 1_000_000


@router.websocket("/api/ws/workspaces/{workspace_id}/{artifact}")
async def collab_ws(websocket: WebSocket, workspace_id: uuid.UUID, artifact: str) -> None:
    if artifact not in _VALID_ARTIFACTS:
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return

    # 0. CSWSH defense: reject cross-site Origin before doing anything else.
    if not check_origin(websocket):
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return

    # 1. Authenticate (before accept).
    try:
        user = await resolve_ws_user(websocket)
    except WSAuthError:
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return

    # 2. Authorize membership + resolve write permission (once, at connect).
    session_factory = websocket.app.state.session_factory

    def _access():
        with session_factory() as s:
            return resolve_access(s, user=user, workspace_id=workspace_id, artifact=artifact)

    is_member, can_write = await run_in_threadpool(_access)
    if not is_member:
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return

    await websocket.accept()

    key = (workspace_id, artifact)

    async def _load() -> list[bytes]:
        def _read():
            with session_factory() as s:
                return store.load_updates(s, workspace_id=workspace_id, artifact=artifact)

        return await run_in_threadpool(_read)

    async def _persist(actor_id: int, update: bytes) -> None:
        def _write():
            with session_factory() as s:
                store.append_update(
                    s,
                    workspace_id=workspace_id,
                    artifact=artifact,
                    actor_id=actor_id,
                    update=update,
                )

        await run_in_threadpool(_write)

    room = await MANAGER.acquire(key, load=_load, client=websocket)
    try:
        await websocket.send_bytes(room.initial_message())
        async for message in websocket.iter_bytes():
            if len(message) > _MAX_FRAME_BYTES:
                await websocket.close(code=_WS_MESSAGE_TOO_BIG)
                break
            await room.handle(
                sender=websocket,
                user_id=user.id,
                can_write=can_write,
                message=message,
                persist=_persist,
            )
    except WebSocketDisconnect:
        pass
    except PersistError:
        # Durable write failed: the in-memory doc is ahead of the log. Evict the
        # room so the next connection rebuilds from the log (the CRDT client
        # re-offers its update on reconnect). Close this socket as a server error.
        await MANAGER.evict(key)
        await websocket.close(code=_WS_INTERNAL_ERROR)
    finally:
        await MANAGER.release(key, client=websocket)
```

- [ ] **Step 4: Mount the router**

In `src/call_manager/main.py`, add the import near the other proposal-assist router imports and include it in `create_app()` **before** the `mount_spa(...)` call (the SPA catch-all must remain last):

```python
from call_manager.proposal_assist.web import collab_ws  # near other PA router imports

# ... inside create_app(), after the other app.include_router(...) calls and
# BEFORE mount_spa(app, ...):
app.include_router(collab_ws.router)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/proposal_assist/collab/test_collab_ws_route.py -q`
Expected: PASS (8 tests).

If `iter_bytes()` does not surface the sent update before the client context closes (a timing race in TestClient), have the server echo nothing but ensure the test sends the update and then performs one `wsconn.receive` with a short timeout OR closes the connection (the `finally`/`release` path drains). The committed test closes the context after `send_bytes`, which flushes the queued message to the server task before disconnect; if flakiness appears, add `wsconn.send_bytes(...)` followed by `wsconn.close()` explicitly before asserting on the DB.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/web/collab_ws.py src/call_manager/main.py tests/proposal_assist/collab/test_collab_ws_route.py
git commit -m "feat(collab): yjs sync WebSocket route + mount"
```

---

### Task 7: Restart-recovery integration test + Slice-1 notes

**Files:**
- Test: `tests/proposal_assist/collab/test_collab_recovery.py`
- Create: `docs/superpowers/notes/2026-06-30-collab-slice1-sync-server-notes.md`

**Interfaces:** Consumes the full stack (route + manager + store).

- [ ] **Step 1: Write the recovery integration test**

Proves the durable-log model: a write persists, the in-memory room is evicted (simulating a process restart / last-client-leave), and a fresh connection's SYNC_STEP1→STEP2 carries the recovered state reconstructed from Postgres.

```python
# tests/proposal_assist/collab/test_collab_recovery.py
from fastapi.testclient import TestClient
from pycrdt import Doc, Text

from call_manager.auth.session_cookie import SessionSigner
from call_manager.proposal_assist.collab import protocol as P
from call_manager.proposal_assist.collab.room import MANAGER
from tests.proposal_assist.conftest import make_user, make_workspace


def _cookie(app, uid):
    signer: SessionSigner = app.state.session_signer
    return signer.make(user_id=uid)


def _update_frame(text):
    d = Doc()
    d["body"] = Text()
    cap = []
    sub = d.observe(lambda e: cap.append(e.update))
    d["body"] += text
    d.unobserve(sub)
    return P.create_update_message(cap[-1])


def _recv_until_write(wsconn, tries: int = 10) -> bytes:
    for _ in range(tries):
        frame = wsconn.receive_bytes()
        if P.is_sync_write(frame):
            return frame
    raise AssertionError("no SYNC write frame received")


def test_state_survives_room_eviction_and_reconnect(_app, pg_session_factory):
    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="recover-owner@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        s.commit()
        wid, oid = ws.id, owner.id

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        with client.websocket_connect(f"/api/ws/workspaces/{wid}/draft") as wsconn:
            wsconn.receive_bytes()  # STEP1
            wsconn.send_bytes(_update_frame("durable edit"))
        # context exit -> last client leaves -> room evicted from MANAGER
        assert not MANAGER.has_room((wid, "draft"))

        # fresh connection: server must rebuild the doc from Postgres and
        # answer our STEP1 with a STEP2 carrying the recovered content.
        with client.websocket_connect(f"/api/ws/workspaces/{wid}/draft") as wsconn:
            wsconn.receive_bytes()  # server STEP1 (drain)
            mirror = Doc()
            mirror["body"] = Text()
            wsconn.send_bytes(P.create_sync_message(mirror))  # our STEP1
            reply = _recv_until_write(wsconn)                 # server STEP2
            mirror.apply_update(P.extract_update(reply))
            assert "durable edit" in str(mirror["body"])
    finally:
        client.__exit__(None, None, None)


def test_xmlfragment_doc_survives_recovery_via_the_bridge(_app, pg_session_factory):
    # Exercise the REAL Slice-0 bridge surface (the 'prosemirror' XmlFragment),
    # not a toy Text: seed a proposal doc, persist it through the route, evict
    # the room, reconnect, and confirm the recovered state reconstructs the
    # exact ProseMirror JSON.
    from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
    from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc, ydoc_to_pm_json

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="xml-recover@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        s.commit()
        wid, oid = ws.id, owner.id

    pm_json = markdown_to_pm("## Background\n\nHello **world**.\n")
    seed = pm_json_to_ydoc(pm_json)
    seed_frame = P.create_update_message(seed.get_update())

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    url = f"/api/ws/workspaces/{wid}/draft"
    try:
        with client.websocket_connect(url) as wsconn:
            wsconn.receive_bytes()       # STEP1
            wsconn.send_bytes(seed_frame)
        assert not MANAGER.has_room((wid, "draft"))  # evicted

        with client.websocket_connect(url) as wsconn:
            wsconn.receive_bytes()       # server STEP1 (drain)
            mirror = Doc()
            wsconn.send_bytes(P.create_sync_message(mirror))  # our STEP1
            reply = _recv_until_write(wsconn)                 # server STEP2 (recovered)
            mirror.apply_update(P.extract_update(reply))
            assert ydoc_to_pm_json(mirror) == pm_json
    finally:
        client.__exit__(None, None, None)
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/proposal_assist/collab/test_collab_recovery.py -q`
Expected: PASS. (If the second `receive_bytes` returns the STEP1 and the STEP2 arrives as a subsequent frame, adjust to read until a `is_sync_write` frame is received — loop `receive_bytes()` up to N times collecting frames, then apply the one that `is_sync_write`.)

- [ ] **Step 3: Write the Slice-1 notes doc**

Create `docs/superpowers/notes/2026-06-30-collab-slice1-sync-server-notes.md` capturing:
- What shipped: hand-rolled room on `pycrdt` (zero new deps — codec is core `pycrdt._sync`), `/api/ws/workspaces/{id}/{artifact}`, Postgres `yjs_updates` log (migration 0059), connect-time cookie auth + cached per-update write gate, restart recovery by replay, in-process registry with evict-on-empty.
- Why hand-rolled (the `YRoom.on_message`/`BaseYStore.write` limitations).
- Security/robustness (from the Codex pre-implementation review): **Origin check** (CSWSH defense, reusing the CSRF `_hostport` normalizer) before auth/accept; **apply-then-persist ordering with poison/evict** on persist failure so the durable log stays the source of truth (the CRDT client re-offers its update on reconnect); **broadcast error-isolation** (a dead socket is dropped, others still receive); **inbound frame-size bound** (HTTP body-size middleware doesn't cover WS frames).
- Concurrency model: per-room `asyncio.Lock` around apply+persist; broadcast outside the lock (CRDT tolerates reordering, slow clients don't block writers); sync DB offloaded via `run_in_threadpool`.
- **Deferred (named):** git→room hydration AND checkpoint-to-git → Slice 2; mid-session permission downgrade/notify → Slice 2; awareness/presence/cursors → Slice 3; Tiptap frontend → Slice 3; the spike's **write-path Node validation** (Python `pm_json_to_ydoc` → real browser `y-prosemirror` read) → Slice 2 (first git-hydration consumer); **Yjs update-log compaction/squash** (unbounded append-only growth) → follow-up optimization; **full poison-broadcast** (proactively disconnecting *all* clients of a poisoned room, not just the offending socket — other clients keep talking to the orphaned in-memory doc until they disconnect) → follow-up (rare DB-failure path; new connections already rebuild cleanly from the log).

- [ ] **Step 4: Commit**

```bash
git add tests/proposal_assist/collab/test_collab_recovery.py docs/superpowers/notes/2026-06-30-collab-slice1-sync-server-notes.md
git commit -m "test(collab): restart-recovery integration + Slice-1 notes"
```

---

## Final verification (run before the whole-branch review)

```bash
uv run pytest tests/proposal_assist/collab/ -q     # all collab tests (Slice 0 + Slice 1) green
uv run ruff check .                                 # CI parity
```

The whole testcontainers suite may flake under load; verify via the grouped `tests/proposal_assist/collab/` run above.

---

## Self-Review (author checklist — completed)

**1. Spec coverage (Slice-1 subset of the foundation spec):** sync server (Task 6) ✓; one room per (workspace, artifact) ✓; websocket cookie auth (Task 5) ✓; per-connection write permission by phase + spec-lock, connect-time (Task 5/6) ✓; Postgres update-log persistence with per-update actor (Tasks 2/3) ✓; two-client convergence (Task 4) ✓; restart recovery + late-joiner sync (Task 4/7) ✓; read-only viewer's update neither persisted nor broadcast (Tasks 4/6) ✓. Deferred-by-decision and documented: git hydration/checkpoint, mid-session downgrade, awareness/presence, frontend (Task 7 notes).

**2. Placeholder scan:** every code step has complete code; no TBD/"handle errors"/"similar to". The one conditional is the deps check already resolved (zero new deps).

**3. Type consistency:** `Room.handle` signature, `persist: Callable[[int, bytes], Awaitable[None]]`, `RoomManager.acquire(load=, client=)`, `store.append_update`/`load_updates` kwargs, and `resolve_access -> (is_member, can_write)` are used identically in Tasks 4/5/6. `artifact` is the string `"draft"`/`"spec"` throughout (table CHECK, route validation, room key, permission rules).

**4. Known risk flagged for the implementer:** Starlette `TestClient` websocket timing — the Task-6/7 steps use a `_recv_until_write` read-loop and explicit context close to avoid races against message delivery. The robust two-distinct-client *convergence* proof lives at the socket-free Room layer (Task 4); the route-level two-connection test (Task 6) shares ONE authenticated user / one TestClient event loop on purpose (two TestClients = two event loops sharing a `Room.asyncio.Lock` = undefined behavior).

**5. Codex pre-implementation review incorporated (round 1):** all five findings folded into the tasks — WS Origin check (Task 5 `check_origin` + Task 6 route + tests); apply→persist→broadcast ordering with poison/evict on persist failure (Task 4 `Room`/`PersistError`/`RoomManager.evict` + persist-failure test + Task 6 route handling); broadcast error-isolation (Task 4 `_broadcast` + failing-client test); inbound frame-size bound + malformed-frame drop (Task 6 `_MAX_FRAME_BYTES` + Task 4 malformed test); route-level two-connection convergence + an XmlFragment recovery case over the real Slice-0 bridge (Tasks 6/7).
