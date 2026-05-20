# Base Spec B3 — Agent SDK Adapter + Per-Workspace Drafting Subprocess Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the `DraftingAgent` adapter contract, the subprocess registry that hands out per-workspace handles with idle-timeout teardown, the per-workspace advisory lock, the orphan sweep that cleans up `turn_jobs.status='running'` rows from crashed prior processes, and the resume-from-history serializer that rebuilds an SDK-compatible message stream from `chat_summaries` + `chat_turns`. End state: given a workspace, calling `WorkspaceController.launch(workspace_id)` returns a working `AgentHandle` (real or fake, behind the same Protocol) and tears it down after `WORKSPACE_IDLE_TIMEOUT_SECONDS` of inactivity.

**Architecture:** Two layers. Top layer (Tasks 3–8) is the `DraftingAgent` Protocol + `WorkspaceController` + registry/lock/sweep/resume helpers — entirely SDK-agnostic, exercised with a `FakeAgentAdapter` test double. Bottom layer (Task 9) is `ClaudeAgentSDKAdapter` — the only file in the codebase that imports `claude_agent_sdk`. An SDK upgrade only touches one file.

**Tech Stack:** Python 3.12, `claude-agent-sdk` (new dep — version verified at Task 1), Postgres advisory locks via SQLAlchemy `func.pg_advisory_lock` / `pg_advisory_unlock`, `asyncio` for the subprocess-streaming layer, pytest + testcontainers.

**Spec:** [docs/superpowers/specs/2026-04-18-call-manager-proposal-assist-design.md](../specs/2026-04-18-call-manager-proposal-assist-design.md), §Architecture (lines 42–85) + §Data model `chat_turns` / `chat_summaries` / `turn_jobs` design notes (lines 190–199).

**Overview:** [docs/superpowers/plans/2026-05-19-base-spec-overview.md](2026-05-19-base-spec-overview.md), §B3.

---

## Prerequisites and scope honesty

**Already on master** (from B1 + Slice A): `workspaces` table with `owner_id`/`call_id`/`profile_id` + phased columns; `chat_turns`, `turn_jobs`, `chat_summaries` tables + models; `workspace_setup_jobs` + `WorkspaceSetupJob`; the Phase 0 worker that produces a workspace ready for drafting.

**What B3 does NOT do:**
- Turn submission endpoint (POST /workspaces/<id>/turns) — that's **B4**.
- Compaction logic that writes `chat_summaries` rows — that's also **B4**.
- Any agent tool implementation (`edit_proposal`, etc.) — that's **B5**.
- Chat UI (HTMX transcript, SSE bridge to the browser) — that's **B6**.

B3 produces the *adapter and lifecycle plumbing* that B4/B5/B6 plug into. The end state is testable in isolation: a fake adapter satisfies the Protocol; the real adapter wraps the SDK; the controller wires them.

**SDK-API caveat (Task 9 only):** The `claude-agent-sdk` API surface evolves frequently. Tasks 1–8 are written against a stable `DraftingAgent` Protocol the codebase owns; they do not depend on the SDK's exact shape. Task 9's discovery step requires the implementer to read the SDK README/quickstart and confirm `query()` / `Options` / streaming-message types before writing adapter code. If the SDK has changed materially since this plan was written, Task 9 is the only one that needs adjustment.

---

## Conventions reference

(Same testcontainers + `pg_session_factory` infrastructure as B1, Slice A Part 1, Slice A Part 2. New helpers in `tests/proposal_assist/conftest.py`. Migrations chain off `0027`. New code goes under `src/call_monitor/proposal_assist/drafting/`.)

Specifically:
- All new test files under `tests/proposal_assist/`.
- Don't sweep up pre-existing `models.py` ruff format violations.
- `from sqlalchemy.exc import IntegrityError` for constraint-tests.
- `_settings_env` fixture supplies `DATABASE_URL` + friends.

---

## File map

**Created:**
- `src/call_monitor/proposal_assist/drafting/__init__.py`
- `src/call_monitor/proposal_assist/drafting/contract.py` — `AgentHandle` + `DraftingAgent` Protocol + stream event types
- `src/call_monitor/proposal_assist/drafting/fake_adapter.py` — `FakeAgentAdapter` test double
- `src/call_monitor/proposal_assist/drafting/registry.py` — `SubprocessRegistry` (in-memory map + idle timer)
- `src/call_monitor/proposal_assist/drafting/locking.py` — per-workspace `pg_advisory_lock` helpers
- `src/call_monitor/proposal_assist/drafting/orphan_sweep.py` — one-shot SQL that transitions stale `running` `turn_jobs` to `failed`
- `src/call_monitor/proposal_assist/drafting/resume.py` — builds SDK-compatible message list from history
- `src/call_monitor/proposal_assist/drafting/controller.py` — `WorkspaceController` composes registry + lock + sweep + resume
- `src/call_monitor/proposal_assist/drafting/claude_adapter.py` — real `ClaudeAgentSDKAdapter` (the SDK-touching file)
- `tests/proposal_assist/test_drafting_contract.py`
- `tests/proposal_assist/test_drafting_fake_adapter.py`
- `tests/proposal_assist/test_drafting_registry.py`
- `tests/proposal_assist/test_drafting_locking.py`
- `tests/proposal_assist/test_drafting_orphan_sweep.py`
- `tests/proposal_assist/test_drafting_resume.py`
- `tests/proposal_assist/test_drafting_controller.py`
- `tests/proposal_assist/test_drafting_claude_adapter.py` (skipped unless `ANTHROPIC_API_KEY` set in env)

**Modified:**
- `pyproject.toml` — add `claude-agent-sdk` dependency
- `src/call_monitor/config.py` — add `workspace_idle_timeout_seconds` setting
- `tests/proposal_assist/conftest.py` — add `make_chat_turn` + `make_chat_summary` helpers

---

## Task 1: Add `claude-agent-sdk` dependency + smoke test

**Files:**
- Modify: `pyproject.toml`
- Test: `tests/proposal_assist/test_drafting_sdk_dep.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_drafting_sdk_dep.py`:

```python
"""Smoke test: claude-agent-sdk is installed and importable.

This is the only test in B3 that touches the real SDK during Tasks 1-8.
If the SDK isn't pinned or moves to a different package name, this test
fails fast (rather than at Task 9 when we'd otherwise discover it)."""


def test_claude_agent_sdk_is_importable():
    import claude_agent_sdk  # noqa: F401
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_drafting_sdk_dep.py -v
```

Expected: `ModuleNotFoundError: No module named 'claude_agent_sdk'`.

- [ ] **Step 3: Add the dependency**

In `pyproject.toml`, find the `[project] dependencies = [...]` list. Add `"claude-agent-sdk"` (don't pin a version yet — the next step's `uv sync` records the resolved version in `uv.lock`).

```toml
dependencies = [
    # ... existing entries ...
    "claude-agent-sdk",
]
```

- [ ] **Step 4: Sync + run**

```bash
uv sync
uv run pytest tests/proposal_assist/test_drafting_sdk_dep.py -v
```

Expected: 1 passed. Note the resolved version that landed in `uv.lock` (search for `claude-agent-sdk`); record it in the commit message body.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock tests/proposal_assist/test_drafting_sdk_dep.py
git commit -m "feat(b3): add claude-agent-sdk dependency

Smoke test verifies import. Resolved version: <fill in from uv.lock>."
```

---

## Task 2: Add `WORKSPACE_IDLE_TIMEOUT_SECONDS` setting

**Files:**
- Modify: `src/call_monitor/config.py`
- Test: extend `tests/proposal_assist/test_config.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/proposal_assist/test_config.py`:

```python
def test_workspace_idle_timeout_seconds_default(monkeypatch, _settings_env):
    monkeypatch.delenv("WORKSPACE_IDLE_TIMEOUT_SECONDS", raising=False)
    assert Settings().workspace_idle_timeout_seconds == 600


def test_workspace_idle_timeout_seconds_override(monkeypatch, _settings_env):
    monkeypatch.setenv("WORKSPACE_IDLE_TIMEOUT_SECONDS", "120")
    assert Settings().workspace_idle_timeout_seconds == 120
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_config.py::test_workspace_idle_timeout_seconds_default -v
```

Expected: `AttributeError: 'Settings' object has no attribute 'workspace_idle_timeout_seconds'`.

- [ ] **Step 3: Add the setting**

In `src/call_monitor/config.py`, inside `class Settings(BaseSettings):` near the other proposal-assist settings:

```python
    workspace_idle_timeout_seconds: int = Field(
        default=600,
        alias="WORKSPACE_IDLE_TIMEOUT_SECONDS",
        description=(
            "Seconds of inactivity after which a drafting subprocess is "
            "torn down. The next user turn relaunches it (with resume-"
            "from-history). Lower = less idle memory; higher = fewer "
            "cold starts on bursty usage."
        ),
    )
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_config.py -v
uv run ruff format src/call_monitor/config.py tests/proposal_assist/test_config.py
git add src/call_monitor/config.py tests/proposal_assist/test_config.py
git commit -m "feat(b3): add WORKSPACE_IDLE_TIMEOUT_SECONDS setting"
```

---

## Task 3: `AgentHandle` + `DraftingAgent` Protocol + stream-event types

The contract every adapter implements. Defines the events that flow back from a turn so the web layer (B6) and turn-queue logic (B4) can consume them without knowing whether they came from the real SDK or a test fake.

**Files:**
- Create: `src/call_monitor/proposal_assist/drafting/__init__.py` (empty)
- Create: `src/call_monitor/proposal_assist/drafting/contract.py`
- Test: `tests/proposal_assist/test_drafting_contract.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_drafting_contract.py`:

```python
import uuid

from call_monitor.proposal_assist.drafting.contract import (
    AgentHandle,
    AssistantTextEvent,
    DraftingAgent,
    StreamEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnDoneEvent,
)


def test_agent_handle_carries_workspace_id_and_arbitrary_state():
    """AgentHandle is a small dataclass: workspace_id (uuid) + opaque
    `state` dict for adapter-specific bookkeeping (process pid for
    Claude, recorder for Fake, etc.)."""
    wsid = uuid.uuid4()
    h = AgentHandle(workspace_id=wsid, state={"pid": 12345})
    assert h.workspace_id == wsid
    assert h.state["pid"] == 12345


def test_stream_event_types_carry_expected_fields():
    """The four event variants the contract guarantees."""
    a = AssistantTextEvent(text="hello")
    assert a.text == "hello"

    u = ToolUseEvent(tool_name="edit_proposal",
                     tool_use_id="tu_1",
                     input={"old": "foo", "new": "bar"})
    assert u.tool_name == "edit_proposal"
    assert u.tool_use_id == "tu_1"
    assert u.input == {"old": "foo", "new": "bar"}

    r = ToolResultEvent(tool_use_id="tu_1",
                        content="ok",
                        is_error=False)
    assert r.is_error is False

    d = TurnDoneEvent(tokens_in=100, tokens_out=200, model="claude-opus-4-7")
    assert d.tokens_in == 100
    assert d.tokens_out == 200
    assert d.model == "claude-opus-4-7"


def test_drafting_agent_protocol_methods_exist():
    """The Protocol declares launch/send_turn/terminate. Anything
    that quacks like it should satisfy isinstance check (runtime_checkable)."""

    class _Dummy:
        async def launch(self, workspace_id: uuid.UUID) -> AgentHandle:
            return AgentHandle(workspace_id=workspace_id, state={})

        async def send_turn(
            self, handle: AgentHandle, user_message: str,
        ) -> "AsyncIterator[StreamEvent]":
            return  # type: ignore[return-value]

        async def terminate(self, handle: AgentHandle) -> None:
            return None

    assert isinstance(_Dummy(), DraftingAgent)
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_drafting_contract.py -v
```

Expected: `ModuleNotFoundError: No module named 'call_monitor.proposal_assist.drafting'`.

- [ ] **Step 3: Implement**

Create `src/call_monitor/proposal_assist/drafting/__init__.py` as an empty file.

Create `src/call_monitor/proposal_assist/drafting/contract.py`:

```python
"""Adapter contract for the drafting agent.

Two concrete adapters live in this package:
  - FakeAgentAdapter (fake_adapter.py) — scriptable test double
  - ClaudeAgentSDKAdapter (claude_adapter.py) — wraps claude-agent-sdk

Everything else in the codebase talks to a DraftingAgent — never directly
to the SDK. An SDK upgrade should touch only claude_adapter.py.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class AgentHandle:
    """Opaque token returned by launch(); passed back to send_turn /
    terminate. The `state` dict is adapter-internal (subprocess pid,
    recorder, etc.) — controller code MUST NOT introspect it."""
    workspace_id: uuid.UUID
    state: dict[str, Any] = field(default_factory=dict)


# Stream event variants. send_turn yields these in order during a turn.
# B4 will persist these into chat_turns rows; B6 will forward them via SSE.


@dataclass
class AssistantTextEvent:
    """A chunk of assistant text (the SDK may stream multiple)."""
    text: str


@dataclass
class ToolUseEvent:
    """Agent invoked a tool. The adapter has already routed the call
    to the registered handler; this event is informational so the UI
    can show 'calling tool X...'."""
    tool_name: str
    tool_use_id: str
    input: dict[str, Any]


@dataclass
class ToolResultEvent:
    """Result of a tool invocation, fed back into the model loop."""
    tool_use_id: str
    content: str
    is_error: bool


@dataclass
class TurnDoneEvent:
    """Terminal event for a turn. Carries token + model telemetry so
    chat_turns rows can be stamped."""
    tokens_in: int | None
    tokens_out: int | None
    model: str | None


StreamEvent = (
    AssistantTextEvent | ToolUseEvent | ToolResultEvent | TurnDoneEvent
)


@runtime_checkable
class DraftingAgent(Protocol):
    """The adapter contract.

    `launch` is awaited once per (workspace, idle-window). It does whatever
    initialization the adapter needs — for Claude this means spawning the
    subprocess and resuming from history; for Fake it just records the call.

    `send_turn` is an async generator: caller iterates with `async for
    event in adapter.send_turn(handle, msg)`. The final event is always
    TurnDoneEvent.

    `terminate` is idempotent: calling on an already-torn-down handle is
    a no-op (logs but doesn't raise)."""

    async def launch(self, workspace_id: uuid.UUID) -> AgentHandle: ...

    async def send_turn(
        self, handle: AgentHandle, user_message: str,
    ) -> AsyncIterator[StreamEvent]: ...

    async def terminate(self, handle: AgentHandle) -> None: ...
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_drafting_contract.py -v
uv run ruff format src/call_monitor/proposal_assist/drafting/contract.py tests/proposal_assist/test_drafting_contract.py
git add src/call_monitor/proposal_assist/drafting/__init__.py src/call_monitor/proposal_assist/drafting/contract.py tests/proposal_assist/test_drafting_contract.py
git commit -m "feat(b3): DraftingAgent Protocol + AgentHandle + StreamEvent types"
```

---

## Task 4: `FakeAgentAdapter` test double

The fake that every subsequent task in this plan tests against. It records `launch`/`send_turn`/`terminate` calls and lets tests script the stream events a turn produces. No subprocess, no SDK, no async I/O of any consequence.

**Files:**
- Create: `src/call_monitor/proposal_assist/drafting/fake_adapter.py`
- Test: `tests/proposal_assist/test_drafting_fake_adapter.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_drafting_fake_adapter.py`:

```python
import uuid

import pytest

from call_monitor.proposal_assist.drafting.contract import (
    AgentHandle,
    AssistantTextEvent,
    DraftingAgent,
    TurnDoneEvent,
)
from call_monitor.proposal_assist.drafting.fake_adapter import (
    FakeAgentAdapter,
)


async def _collect(stream):
    """Async-for the stream into a list."""
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_fake_adapter_implements_protocol():
    fake = FakeAgentAdapter()
    assert isinstance(fake, DraftingAgent)


@pytest.mark.asyncio
async def test_fake_adapter_records_launch():
    fake = FakeAgentAdapter()
    wsid = uuid.uuid4()
    handle = await fake.launch(wsid)
    assert isinstance(handle, AgentHandle)
    assert handle.workspace_id == wsid
    assert fake.launches == [wsid]


@pytest.mark.asyncio
async def test_fake_adapter_scripts_turn_events():
    """Test scripts the events a turn will produce; adapter replays them
    in order. Final event is always TurnDoneEvent."""
    fake = FakeAgentAdapter()
    wsid = uuid.uuid4()
    handle = await fake.launch(wsid)

    fake.script_next_turn([
        AssistantTextEvent(text="Hello, world!"),
        TurnDoneEvent(tokens_in=10, tokens_out=20, model="fake-model"),
    ])

    events = await _collect(fake.send_turn(handle, "user message"))
    assert len(events) == 2
    assert isinstance(events[0], AssistantTextEvent)
    assert events[0].text == "Hello, world!"
    assert isinstance(events[1], TurnDoneEvent)


@pytest.mark.asyncio
async def test_fake_adapter_records_turn_history():
    fake = FakeAgentAdapter()
    wsid = uuid.uuid4()
    handle = await fake.launch(wsid)
    fake.script_next_turn([TurnDoneEvent(tokens_in=1, tokens_out=1, model="m")])
    fake.script_next_turn([TurnDoneEvent(tokens_in=2, tokens_out=2, model="m")])

    await _collect(fake.send_turn(handle, "first"))
    await _collect(fake.send_turn(handle, "second"))

    assert fake.turns == [(handle, "first"), (handle, "second")]


@pytest.mark.asyncio
async def test_fake_adapter_terminate_is_idempotent():
    fake = FakeAgentAdapter()
    wsid = uuid.uuid4()
    handle = await fake.launch(wsid)
    await fake.terminate(handle)
    await fake.terminate(handle)  # second call doesn't raise
    assert fake.terminations == [handle, handle]


@pytest.mark.asyncio
async def test_fake_adapter_send_turn_without_script_returns_terminal_only():
    """If a test forgot to script a turn, default to a TurnDoneEvent so
    the harness doesn't hang on `async for`."""
    fake = FakeAgentAdapter()
    wsid = uuid.uuid4()
    handle = await fake.launch(wsid)
    events = await _collect(fake.send_turn(handle, "unscripted"))
    assert len(events) == 1
    assert isinstance(events[0], TurnDoneEvent)
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_drafting_fake_adapter.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`src/call_monitor/proposal_assist/drafting/fake_adapter.py`:

```python
"""Test double for DraftingAgent. Records calls; lets tests script
the stream of events each turn produces."""
from __future__ import annotations

import uuid
from collections import deque
from collections.abc import AsyncIterator

from call_monitor.proposal_assist.drafting.contract import (
    AgentHandle,
    StreamEvent,
    TurnDoneEvent,
)


class FakeAgentAdapter:
    """Records launch/send_turn/terminate; lets tests script per-turn
    event streams via `script_next_turn(events)`.

    Inspection attributes for assertions:
      - launches: list[uuid.UUID]
      - turns: list[tuple[AgentHandle, str]]
      - terminations: list[AgentHandle]
    """

    def __init__(self) -> None:
        self.launches: list[uuid.UUID] = []
        self.turns: list[tuple[AgentHandle, str]] = []
        self.terminations: list[AgentHandle] = []
        self._scripted: deque[list[StreamEvent]] = deque()

    def script_next_turn(self, events: list[StreamEvent]) -> None:
        """Queue the events the next send_turn() invocation should yield.
        If you don't script, send_turn yields a single bare TurnDoneEvent."""
        self._scripted.append(events)

    async def launch(self, workspace_id: uuid.UUID) -> AgentHandle:
        self.launches.append(workspace_id)
        return AgentHandle(workspace_id=workspace_id, state={"fake": True})

    async def send_turn(
        self, handle: AgentHandle, user_message: str,
    ) -> AsyncIterator[StreamEvent]:
        self.turns.append((handle, user_message))
        if self._scripted:
            scripted = self._scripted.popleft()
        else:
            scripted = [TurnDoneEvent(
                tokens_in=None, tokens_out=None, model=None,
            )]
        for event in scripted:
            yield event

    async def terminate(self, handle: AgentHandle) -> None:
        self.terminations.append(handle)
```

- [ ] **Step 4: Verify pytest-asyncio config**

The tests use `@pytest.mark.asyncio`. The project's `pyproject.toml` already has `asyncio_mode = "auto"` (per Slice A — confirm with `grep asyncio_mode pyproject.toml`). If yes, the `@pytest.mark.asyncio` decorator is harmless but redundant; either form works. If `auto` mode is on, the decorator can be removed; if not, keep it.

- [ ] **Step 5: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_drafting_fake_adapter.py -v
uv run ruff format src/call_monitor/proposal_assist/drafting/fake_adapter.py tests/proposal_assist/test_drafting_fake_adapter.py
git add src/call_monitor/proposal_assist/drafting/fake_adapter.py tests/proposal_assist/test_drafting_fake_adapter.py
git commit -m "feat(b3): FakeAgentAdapter test double"
```

---

## Task 5: Per-workspace advisory lock helpers

Postgres `pg_advisory_lock(bigint)` gives us session-scoped mutual exclusion. The spec ([line 195](docs/superpowers/specs/2026-04-18-call-manager-proposal-assist-design.md#L195)) mandates this for "one subprocess per workspace": each handle holds `pg_advisory_lock(hashtext('workspace:' || workspace_id::text))` while live; a racing handle blocks on it.

We wrap this in small helpers so the lock-acquisition pattern is testable in isolation and reusable by both the registry (Task 6) and any future code that needs per-workspace exclusion.

**Files:**
- Create: `src/call_monitor/proposal_assist/drafting/locking.py`
- Test: `tests/proposal_assist/test_drafting_locking.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_drafting_locking.py`:

```python
import uuid

from sqlalchemy import text

from call_monitor.proposal_assist.drafting.locking import (
    acquire_workspace_lock,
    release_workspace_lock,
    workspace_lock_key,
)


def test_workspace_lock_key_is_stable_and_workspace_specific():
    """Same workspace → same key; different workspaces → different keys.
    Key is a bigint (Postgres' pg_advisory_lock signature)."""
    wsid1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
    wsid2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
    key1 = workspace_lock_key(wsid1)
    key2 = workspace_lock_key(wsid2)
    assert isinstance(key1, int)
    # Fits in a Postgres bigint (signed 64-bit).
    assert -(2**63) <= key1 < 2**63
    assert key1 == workspace_lock_key(wsid1)  # stable
    assert key1 != key2  # workspace-specific


def test_acquire_and_release_workspace_lock(pg_session_factory):
    """Acquire returns True when the lock is free; release returns True
    when we held it."""
    wsid = uuid.uuid4()
    with pg_session_factory() as s:
        assert acquire_workspace_lock(s, wsid) is True
        assert release_workspace_lock(s, wsid) is True


def test_acquire_blocks_second_session(pg_session_factory):
    """Two sessions can't hold the same workspace's lock at the same
    time. We use pg_try_advisory_lock semantics (non-blocking) — second
    attempt returns False, then succeeds after the first releases."""
    wsid = uuid.uuid4()
    with pg_session_factory() as s1, pg_session_factory() as s2:
        assert acquire_workspace_lock(s1, wsid) is True
        assert acquire_workspace_lock(s2, wsid) is False
        assert release_workspace_lock(s1, wsid) is True
        # Now s2 can acquire.
        assert acquire_workspace_lock(s2, wsid) is True
        assert release_workspace_lock(s2, wsid) is True


def test_different_workspaces_do_not_contend(pg_session_factory):
    wsid_a = uuid.uuid4()
    wsid_b = uuid.uuid4()
    with pg_session_factory() as s1, pg_session_factory() as s2:
        assert acquire_workspace_lock(s1, wsid_a) is True
        assert acquire_workspace_lock(s2, wsid_b) is True
        assert release_workspace_lock(s1, wsid_a) is True
        assert release_workspace_lock(s2, wsid_b) is True
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_drafting_locking.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`src/call_monitor/proposal_assist/drafting/locking.py`:

```python
"""Per-workspace Postgres advisory locks.

Spec mandate (2026-04-18 design line 195): each drafting subprocess
holds `pg_advisory_lock(hashtext('workspace:' || workspace_id::text))`
for its lifetime. The unique partial index on `turn_jobs.workspace_id
WHERE status='running'` (Slice A Part 2 migration 0026) is the database-
level safety net, but advisory locks let the application see contention
without trying-and-failing on an INSERT.

We use the try-variant (`pg_try_advisory_lock`) so a second attempt
returns False rather than blocking. The registry (registry.py) treats
False as "another process is already handling this workspace — back
off".
"""
from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session


def workspace_lock_key(workspace_id: uuid.UUID) -> int:
    """Stable 64-bit signed integer derived from the workspace UUID.

    We can't use Postgres' hashtext() at advisory-lock-key time because
    we want the same key from every connection (hashtext is text-input,
    not deterministic across Postgres versions). Use a Python-side
    sha256 truncated to 64 bits, then map into the signed-int range.
    """
    digest = hashlib.sha256(
        f"workspace:{workspace_id}".encode("utf-8"),
    ).digest()
    # First 8 bytes → unsigned 64-bit → signed 64-bit by subtracting 2**63
    # if the top bit is set.
    unsigned = int.from_bytes(digest[:8], byteorder="big", signed=False)
    if unsigned >= 2**63:
        return unsigned - 2**64
    return unsigned


def acquire_workspace_lock(session: Session, workspace_id: uuid.UUID) -> bool:
    """Non-blocking acquire. Returns True on success, False if held by
    another session.

    The session holds the lock until release_workspace_lock() OR until the
    session ends (Postgres releases all session-level advisory locks on
    disconnect / rollback-after-error)."""
    key = workspace_lock_key(workspace_id)
    result = session.execute(
        text("SELECT pg_try_advisory_lock(:k)"),
        {"k": key},
    ).scalar()
    return bool(result)


def release_workspace_lock(session: Session, workspace_id: uuid.UUID) -> bool:
    """Returns True if we held the lock and successfully released it;
    False if we didn't hold it (Postgres returns false in that case)."""
    key = workspace_lock_key(workspace_id)
    result = session.execute(
        text("SELECT pg_advisory_unlock(:k)"),
        {"k": key},
    ).scalar()
    return bool(result)
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_drafting_locking.py -v
uv run ruff format src/call_monitor/proposal_assist/drafting/locking.py tests/proposal_assist/test_drafting_locking.py
git add src/call_monitor/proposal_assist/drafting/locking.py tests/proposal_assist/test_drafting_locking.py
git commit -m "feat(b3): per-workspace advisory lock helpers"
```

---

## Task 6: Orphan sweep — clean up `turn_jobs` rows from crashed prior processes

Spec mandate (lines 196–197): when a subprocess launches (or `/admin` runs "recover workspace"), a single SQL pass transitions any `running` `turn_jobs` for that workspace to `failed` with `error='orphaned'`. This cleans up after crashes and redeploys.

Standalone, pure-SQL helper. Run from `WorkspaceController.launch()` before handing back a fresh handle.

**Files:**
- Create: `src/call_monitor/proposal_assist/drafting/orphan_sweep.py`
- Test: `tests/proposal_assist/test_drafting_orphan_sweep.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_drafting_orphan_sweep.py`:

```python
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from call_monitor.db.models import TurnJob
from call_monitor.proposal_assist.drafting.orphan_sweep import (
    sweep_orphaned_turn_jobs,
)
from tests.proposal_assist.conftest import make_workspace


def test_sweep_marks_running_turn_jobs_as_failed(pg_session_factory):
    """A turn_jobs row left in status='running' for this workspace is
    transitioned to 'failed' with error='orphaned'. Returns the count
    of swept rows."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(TurnJob(
            workspace_id=ws.id,
            status="running",
            started_at=datetime.now(UTC),
        ))
        s.commit()
        wsid = ws.id

        swept = sweep_orphaned_turn_jobs(s, wsid)
        s.commit()

        assert swept == 1
        job = s.scalar(
            select(TurnJob).where(TurnJob.workspace_id == wsid)
        )
        assert job.status == "failed"
        assert job.error == "orphaned"
        assert job.completed_at is not None


def test_sweep_leaves_other_statuses_alone(pg_session_factory):
    """queued / completed / failed / cancelled rows are not touched."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        for status in ("queued", "completed", "failed", "cancelled"):
            s.add(TurnJob(workspace_id=ws.id, status=status))
        s.commit()
        wsid = ws.id

        swept = sweep_orphaned_turn_jobs(s, wsid)
        s.commit()

        assert swept == 0
        # Verify each status row is unchanged.
        for status in ("queued", "completed", "failed", "cancelled"):
            row = s.scalar(
                select(TurnJob).where(
                    TurnJob.workspace_id == wsid,
                    TurnJob.status == status,
                )
            )
            assert row is not None, f"row with status {status} vanished"


def test_sweep_is_workspace_scoped(pg_session_factory):
    """A 'running' row on workspace_A doesn't affect a sweep on
    workspace_B."""
    with pg_session_factory() as s:
        ws_a = make_workspace(s, title="A")
        ws_b = make_workspace(s, title="B")
        s.add(TurnJob(workspace_id=ws_a.id, status="running"))
        s.add(TurnJob(workspace_id=ws_b.id, status="running"))
        s.commit()
        a_id, b_id = ws_a.id, ws_b.id

        swept_b = sweep_orphaned_turn_jobs(s, b_id)
        s.commit()
        assert swept_b == 1

        # A's row is unchanged.
        a_row = s.scalar(select(TurnJob).where(TurnJob.workspace_id == a_id))
        assert a_row.status == "running"


def test_sweep_returns_zero_when_nothing_to_do(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        assert sweep_orphaned_turn_jobs(s, ws.id) == 0
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_drafting_orphan_sweep.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`src/call_monitor/proposal_assist/drafting/orphan_sweep.py`:

```python
"""Orphan sweep — transition stale `turn_jobs.status='running'` rows
for a workspace to 'failed' with error='orphaned'.

Called from WorkspaceController.launch() before handing back a fresh
handle. Cleans up after a crashed prior subprocess or a redeploy
that killed an in-flight turn."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from call_monitor.db.models import TurnJob


def sweep_orphaned_turn_jobs(session: Session, workspace_id: uuid.UUID) -> int:
    """Mark any 'running' turn_jobs for this workspace as 'failed' with
    error='orphaned'. Returns the count of rows swept.

    Caller commits. Workspace-scoped — does not touch other workspaces
    even if they also have stale 'running' rows."""
    result = session.execute(
        update(TurnJob)
        .where(
            TurnJob.workspace_id == workspace_id,
            TurnJob.status == "running",
        )
        .values(
            status="failed",
            error="orphaned",
            completed_at=datetime.now(UTC),
        )
    )
    return result.rowcount or 0
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_drafting_orphan_sweep.py -v
uv run ruff format src/call_monitor/proposal_assist/drafting/orphan_sweep.py tests/proposal_assist/test_drafting_orphan_sweep.py
git add src/call_monitor/proposal_assist/drafting/orphan_sweep.py tests/proposal_assist/test_drafting_orphan_sweep.py
git commit -m "feat(b3): orphan sweep for stale running turn_jobs"
```

---

## Task 7: Resume-from-history serializer

Spec mandate (line 197): "Subprocess resume = `latest chat_summaries row + all chat_turns after summarized_through`. This caps cold-start cost and context size at a known ceiling regardless of workspace age."

This task implements the **serializer**: given a `workspace_id`, return a list of message dicts in the SDK's expected format (user/assistant/tool roles, content blocks). The Claude adapter (Task 9) passes the output to `claude_agent_sdk` at session start.

The serializer is pure Python — testable without spinning a subprocess. SDK-format details are encapsulated; if the SDK changes the message shape, only this file (and Task 9) need updating.

**Files:**
- Modify: `tests/proposal_assist/conftest.py` (add `make_chat_turn` + `make_chat_summary` helpers)
- Create: `src/call_monitor/proposal_assist/drafting/resume.py`
- Test: `tests/proposal_assist/test_drafting_resume.py`

- [ ] **Step 1: Add conftest helpers**

Append to `tests/proposal_assist/conftest.py`:

```python
def make_chat_turn(
    session,
    workspace,
    *,
    role: str,
    content: dict,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    model: str | None = None,
):
    """Build a ChatTurn row. Returns the flushed row (with .id populated)."""
    from call_monitor.db.models import ChatTurn

    turn = ChatTurn(
        workspace_id=workspace.id,
        role=role,
        content=content,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        model=model,
    )
    session.add(turn)
    session.flush()
    return turn


def make_chat_summary(
    session,
    workspace,
    summarized_through,
    *,
    summary_text: str = "previous turns summarized here.",
    tokens: int = 100,
    model: str = "claude-haiku-4-5",
):
    """Build a ChatSummary row covering all turns up to and including
    `summarized_through` (a ChatTurn instance)."""
    from call_monitor.db.models import ChatSummary

    cs = ChatSummary(
        workspace_id=workspace.id,
        summary=summary_text,
        summarized_through=summarized_through.id,
        tokens=tokens,
        model=model,
    )
    session.add(cs)
    session.flush()
    return cs
```

- [ ] **Step 2: Write failing test**

`tests/proposal_assist/test_drafting_resume.py`:

```python
from call_monitor.proposal_assist.drafting.resume import (
    build_resume_messages,
)
from tests.proposal_assist.conftest import (
    make_chat_summary,
    make_chat_turn,
    make_workspace,
)


def test_resume_with_no_history_returns_empty_list(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        assert build_resume_messages(s, ws.id) == []


def test_resume_with_only_chat_turns_returns_all_in_order(pg_session_factory):
    """No summary → include every chat_turn, oldest-first."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        make_chat_turn(s, ws, role="user",
                       content={"type": "text", "text": "hi"})
        make_chat_turn(s, ws, role="assistant",
                       content={"type": "text", "text": "hello"})
        s.commit()

        messages = build_resume_messages(s, ws.id)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == {"type": "text", "text": "hi"}
        assert messages[1]["role"] == "assistant"


def test_resume_with_summary_prepends_summary_skips_summarized_turns(
    pg_session_factory,
):
    """Resume = [synthetic summary message] + [turns AFTER summarized_through].

    The summary itself becomes a synthetic system-style assistant message
    that the SDK accepts as the conversation prefix."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        t1 = make_chat_turn(s, ws, role="user",
                            content={"type": "text", "text": "old user"})
        t2 = make_chat_turn(s, ws, role="assistant",
                            content={"type": "text", "text": "old assistant"})
        # Summary covers t1 and t2.
        make_chat_summary(s, ws, t2, summary_text="prev conversation: ...")
        # Newer turns after the summary boundary.
        make_chat_turn(s, ws, role="user",
                       content={"type": "text", "text": "new user"})
        s.commit()

        messages = build_resume_messages(s, ws.id)
        # First message is the synthesized summary, then the un-summarized turn.
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert "prev conversation" in messages[0]["content"][0]["text"]
        assert "[Summary of prior conversation]" in (
            messages[0]["content"][0]["text"]
        )
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == {"type": "text", "text": "new user"}


def test_resume_uses_latest_summary_when_multiple_exist(pg_session_factory):
    """If a workspace has had compaction run twice, only the most recent
    summary is included; older summaries are subsumed."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        t1 = make_chat_turn(s, ws, role="user",
                            content={"type": "text", "text": "v1 user"})
        make_chat_summary(s, ws, t1, summary_text="first summary")
        t2 = make_chat_turn(s, ws, role="user",
                            content={"type": "text", "text": "v2 user"})
        make_chat_summary(s, ws, t2, summary_text="second summary")
        make_chat_turn(s, ws, role="user",
                       content={"type": "text", "text": "newest"})
        s.commit()

        messages = build_resume_messages(s, ws.id)
        assert len(messages) == 2
        # Only the second summary is present; first is omitted.
        text = messages[0]["content"][0]["text"]
        assert "second summary" in text
        assert "first summary" not in text


def test_resume_skips_summary_boundary_correctly_under_id_ordering(
    pg_session_factory,
):
    """Boundary semantics: 'summarized_through' is INCLUSIVE — the turn
    referenced is summarized, not retained. We rely on chat_turns.created_at
    ordering; turns created after summarized_through's created_at survive."""
    import time

    with pg_session_factory() as s:
        ws = make_workspace(s)
        t_old = make_chat_turn(s, ws, role="user",
                               content={"type": "text", "text": "old"})
        s.commit()
        time.sleep(0.01)  # ensure created_at is strictly later
        t_boundary = make_chat_turn(s, ws, role="user",
                                    content={"type": "text", "text": "boundary"})
        s.commit()
        time.sleep(0.01)
        t_new = make_chat_turn(s, ws, role="user",
                               content={"type": "text", "text": "new"})
        # Summary covers everything up through t_boundary.
        make_chat_summary(s, ws, t_boundary)
        s.commit()

        messages = build_resume_messages(s, ws.id)
        # 1 summary + 1 surviving turn (t_new).
        assert len(messages) == 2
        assert messages[1]["content"] == {"type": "text", "text": "new"}
```

- [ ] **Step 3: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_drafting_resume.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 4: Implement**

`src/call_monitor/proposal_assist/drafting/resume.py`:

```python
"""Resume-from-history serializer.

Spec (2026-04-18 design line 197): subprocess resume =
  latest chat_summaries row + all chat_turns AFTER summarized_through.

Encoded output: a list of message dicts the Claude Agent SDK can replay
as the conversation prefix when a fresh subprocess starts. The format
is `[{"role": "user"|"assistant"|"tool", "content": ...}]`. We DO NOT
wrap tool_use / tool_result content blocks here — chat_turns.content
already holds them in SDK-native shape (the design note on line 192
mandates that)."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_monitor.db.models import ChatSummary, ChatTurn


def build_resume_messages(
    session: Session,
    workspace_id: uuid.UUID,
) -> list[dict]:
    """Build the SDK-compatible message list for resuming a workspace's
    conversation.

    If there is a chat_summaries row, the most recent one is rendered as
    a synthetic prefix message and only turns AFTER its summarized_through
    boundary are appended. Otherwise, all chat_turns are returned in
    created_at order.

    Returns [] for a workspace with no chat history at all.
    """
    # Find the most recent summary, if any.
    summary = session.scalar(
        select(ChatSummary)
        .where(ChatSummary.workspace_id == workspace_id)
        .order_by(ChatSummary.created_at.desc())
        .limit(1)
    )

    if summary is None:
        # No summary — include every turn.
        turns = session.scalars(
            select(ChatTurn)
            .where(ChatTurn.workspace_id == workspace_id)
            .order_by(ChatTurn.created_at)
        ).all()
        return [
            {"role": turn.role, "content": turn.content}
            for turn in turns
        ]

    # Resolve the boundary turn's created_at; only turns strictly after
    # it (created_at greater than the boundary) survive.
    boundary = session.get(ChatTurn, summary.summarized_through)
    if boundary is None:
        # Shouldn't happen (FK ON DELETE CASCADE), but be defensive:
        # treat as no summary.
        survivors = session.scalars(
            select(ChatTurn)
            .where(ChatTurn.workspace_id == workspace_id)
            .order_by(ChatTurn.created_at)
        ).all()
    else:
        survivors = session.scalars(
            select(ChatTurn)
            .where(
                ChatTurn.workspace_id == workspace_id,
                ChatTurn.created_at > boundary.created_at,
            )
            .order_by(ChatTurn.created_at)
        ).all()

    # Synthesize the prefix message from the summary. Use a 'user' role
    # so the SDK accepts it without complaint about consecutive assistant
    # messages (the next turn will be an assistant turn after the user
    # sends their next message).
    summary_prefix = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "[Summary of prior conversation]\n\n"
                    f"{summary.summary}"
                ),
            },
        ],
    }

    return [summary_prefix] + [
        {"role": turn.role, "content": turn.content}
        for turn in survivors
    ]
```

- [ ] **Step 5: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_drafting_resume.py -v
uv run ruff format src/call_monitor/proposal_assist/drafting/resume.py tests/proposal_assist/test_drafting_resume.py tests/proposal_assist/conftest.py
git add src/call_monitor/proposal_assist/drafting/resume.py tests/proposal_assist/test_drafting_resume.py tests/proposal_assist/conftest.py
git commit -m "feat(b3): resume-from-history serializer (summary + un-summarized turns)"
```

---

## Task 8: `SubprocessRegistry` + `WorkspaceController`

The composition layer. `SubprocessRegistry` keeps an in-memory `{workspace_id → AgentHandle}` map with last-touched timestamps for idle-timeout teardown. `WorkspaceController.launch(workspace_id)` acquires the advisory lock, runs the orphan sweep, builds resume messages, and asks the adapter to launch.

We test the controller end-to-end against `FakeAgentAdapter` — that exercises every code path in registry/lock/sweep/resume without needing real subprocesses.

**Files:**
- Create: `src/call_monitor/proposal_assist/drafting/registry.py`
- Create: `src/call_monitor/proposal_assist/drafting/controller.py`
- Test: `tests/proposal_assist/test_drafting_registry.py`
- Test: `tests/proposal_assist/test_drafting_controller.py`

### Subtask 8a: `SubprocessRegistry`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_drafting_registry.py`:

```python
import asyncio
import uuid

import pytest

from call_monitor.proposal_assist.drafting.contract import AgentHandle
from call_monitor.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_monitor.proposal_assist.drafting.registry import SubprocessRegistry


@pytest.mark.asyncio
async def test_registry_get_or_launch_returns_new_handle():
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    wsid = uuid.uuid4()

    handle = await registry.get_or_launch(wsid)
    assert isinstance(handle, AgentHandle)
    assert handle.workspace_id == wsid
    assert adapter.launches == [wsid]


@pytest.mark.asyncio
async def test_registry_returns_same_handle_on_second_call():
    """Within the idle window, get_or_launch returns the existing handle
    without relaunching."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    wsid = uuid.uuid4()

    h1 = await registry.get_or_launch(wsid)
    h2 = await registry.get_or_launch(wsid)

    assert h1 is h2
    assert len(adapter.launches) == 1  # only one launch


@pytest.mark.asyncio
async def test_registry_terminate_removes_handle():
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    wsid = uuid.uuid4()

    h = await registry.get_or_launch(wsid)
    await registry.terminate(wsid)

    assert adapter.terminations == [h]
    # Next get_or_launch creates a fresh handle.
    h2 = await registry.get_or_launch(wsid)
    assert h2 is not h
    assert len(adapter.launches) == 2


@pytest.mark.asyncio
async def test_registry_sweep_idle_terminates_stale_handles():
    """Handles untouched for longer than idle_seconds are terminated by
    sweep_idle()."""
    adapter = FakeAgentAdapter()
    # idle_seconds=0 so any handle older than 'now' is stale.
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=0)
    wsid = uuid.uuid4()

    await registry.get_or_launch(wsid)
    # Yield to give the timestamp a tiny gap.
    await asyncio.sleep(0.01)
    swept = await registry.sweep_idle()
    assert swept == 1
    assert len(adapter.terminations) == 1


@pytest.mark.asyncio
async def test_registry_sweep_idle_leaves_fresh_handles_alone():
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    wsid = uuid.uuid4()
    await registry.get_or_launch(wsid)
    swept = await registry.sweep_idle()
    assert swept == 0
    assert adapter.terminations == []


@pytest.mark.asyncio
async def test_registry_terminate_unknown_workspace_is_noop():
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    await registry.terminate(uuid.uuid4())  # no raise
    assert adapter.terminations == []
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_drafting_registry.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement registry**

`src/call_monitor/proposal_assist/drafting/registry.py`:

```python
"""In-memory subprocess registry. Maps workspace_id → (AgentHandle,
last_touched_at). Drives idle-timeout teardown."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass

from call_monitor.proposal_assist.drafting.contract import (
    AgentHandle,
    DraftingAgent,
)


@dataclass
class _Entry:
    handle: AgentHandle
    last_touched_monotonic: float


class SubprocessRegistry:
    """One-instance-per-process registry of live drafting subprocesses.

    Concurrency: get_or_launch / terminate / sweep_idle are async and
    serialize through a single asyncio.Lock — there's no win from
    parallelism here (the bottleneck is the SDK's launch, which is
    sub-second). The lock also prevents the launch-vs-sweep race where
    a workspace is launched while sweep_idle is iterating.
    """

    def __init__(self, *, adapter: DraftingAgent, idle_seconds: int) -> None:
        self._adapter = adapter
        self._idle_seconds = idle_seconds
        self._entries: dict[uuid.UUID, _Entry] = {}
        self._lock = asyncio.Lock()

    async def get_or_launch(self, workspace_id: uuid.UUID) -> AgentHandle:
        async with self._lock:
            existing = self._entries.get(workspace_id)
            if existing is not None:
                existing.last_touched_monotonic = time.monotonic()
                return existing.handle

            handle = await self._adapter.launch(workspace_id)
            self._entries[workspace_id] = _Entry(
                handle=handle,
                last_touched_monotonic=time.monotonic(),
            )
            return handle

    async def touch(self, workspace_id: uuid.UUID) -> None:
        """Mark a handle as just-used (so it doesn't get swept). Called
        by the controller after each turn completes."""
        async with self._lock:
            entry = self._entries.get(workspace_id)
            if entry is not None:
                entry.last_touched_monotonic = time.monotonic()

    async def terminate(self, workspace_id: uuid.UUID) -> None:
        """Explicitly tear down a workspace's handle. No-op if unknown."""
        async with self._lock:
            entry = self._entries.pop(workspace_id, None)
        if entry is not None:
            await self._adapter.terminate(entry.handle)

    async def sweep_idle(self) -> int:
        """Terminate any handle untouched for >= idle_seconds. Returns
        the count of swept handles. Intended for periodic scheduler
        invocation."""
        now = time.monotonic()
        cutoff = now - self._idle_seconds

        async with self._lock:
            stale_ids = [
                wsid
                for wsid, entry in self._entries.items()
                if entry.last_touched_monotonic <= cutoff
            ]
            stale_entries = [self._entries.pop(wsid) for wsid in stale_ids]

        # Terminate outside the lock — adapter.terminate may take a while
        # (subprocess teardown) and we don't want to block get_or_launch.
        for entry in stale_entries:
            await self._adapter.terminate(entry.handle)
        return len(stale_entries)
```

- [ ] **Step 4: Run registry tests**

```bash
uv run pytest tests/proposal_assist/test_drafting_registry.py -v
```

Expected: 6 tests pass.

### Subtask 8b: `WorkspaceController`

- [ ] **Step 5: Write failing test**

`tests/proposal_assist/test_drafting_controller.py`:

```python
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from call_monitor.db.models import TurnJob
from call_monitor.proposal_assist.drafting.controller import (
    WorkspaceController,
)
from call_monitor.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_monitor.proposal_assist.drafting.registry import SubprocessRegistry
from tests.proposal_assist.conftest import make_chat_turn, make_workspace


@pytest.mark.asyncio
async def test_controller_launch_returns_handle(pg_session_factory):
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    handle = await controller.launch(wsid)
    assert handle.workspace_id == wsid
    assert adapter.launches == [wsid]


@pytest.mark.asyncio
async def test_controller_launch_runs_orphan_sweep(pg_session_factory):
    """A stale running turn_jobs row is transitioned to failed before
    the new handle is handed back."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(TurnJob(
            workspace_id=ws.id,
            status="running",
            started_at=datetime.now(UTC),
        ))
        s.commit()
        wsid = ws.id

    await controller.launch(wsid)

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
        assert job.status == "failed"
        assert job.error == "orphaned"


@pytest.mark.asyncio
async def test_controller_launch_passes_resume_messages_to_adapter(
    pg_session_factory,
):
    """The controller computes resume messages from history and stuffs
    them into AgentHandle.state['resume_messages']. The adapter is
    responsible for actually replaying them at SDK startup; for the
    fake adapter, we just verify they were attached."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        make_chat_turn(s, ws, role="user",
                       content={"type": "text", "text": "old turn"})
        s.commit()
        wsid = ws.id

    handle = await controller.launch(wsid)

    resume = handle.state.get("resume_messages")
    assert resume is not None
    assert len(resume) == 1
    assert resume[0]["content"] == {"type": "text", "text": "old turn"}


@pytest.mark.asyncio
async def test_controller_launch_acquires_advisory_lock(pg_session_factory):
    """A second launch of the same workspace, while the first handle is
    still live, just returns the cached handle (no relaunch) — the
    advisory-lock acquire on the first launch is the gate. The lock is
    *held by the controller's launch-time session*, which is released
    when the session exits the with-block."""
    # This test asserts that launch() does NOT crash on lock contention;
    # the cached-handle path means concurrent launches resolve to the
    # same handle. (Lock contention as such is hard to provoke from a
    # single test process; the locking module's own tests cover the
    # two-session case.)
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    h1 = await controller.launch(wsid)
    h2 = await controller.launch(wsid)
    assert h1 is h2


@pytest.mark.asyncio
async def test_controller_send_turn_yields_events(pg_session_factory):
    """Controller.send_turn iterates the adapter's stream and touches
    the registry's idle timer along the way."""
    from call_monitor.proposal_assist.drafting.contract import (
        AssistantTextEvent,
        TurnDoneEvent,
    )

    adapter = FakeAgentAdapter()
    adapter.script_next_turn([
        AssistantTextEvent(text="reply"),
        TurnDoneEvent(tokens_in=5, tokens_out=10, model="fake"),
    ])
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    events = [
        event async for event in controller.send_turn(wsid, "hi")
    ]
    assert len(events) == 2
    assert isinstance(events[0], AssistantTextEvent)
    assert isinstance(events[1], TurnDoneEvent)
```

- [ ] **Step 6: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_drafting_controller.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 7: Implement controller**

`src/call_monitor/proposal_assist/drafting/controller.py`:

```python
"""WorkspaceController composes the drafting machinery:

  - acquire per-workspace advisory lock
  - sweep orphaned turn_jobs rows
  - build resume-from-history messages
  - launch (or reuse) the subprocess via the registry
  - expose send_turn / terminate as a thin pass-through

This is the only object the web layer (B6) needs to construct."""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Callable

from sqlalchemy.orm import sessionmaker

from call_monitor.proposal_assist.drafting.contract import (
    AgentHandle,
    StreamEvent,
)
from call_monitor.proposal_assist.drafting.locking import (
    acquire_workspace_lock,
    release_workspace_lock,
)
from call_monitor.proposal_assist.drafting.orphan_sweep import (
    sweep_orphaned_turn_jobs,
)
from call_monitor.proposal_assist.drafting.registry import SubprocessRegistry
from call_monitor.proposal_assist.drafting.resume import (
    build_resume_messages,
)


class WorkspaceController:
    def __init__(
        self,
        *,
        registry: SubprocessRegistry,
        session_factory: sessionmaker,
    ) -> None:
        self._registry = registry
        self._session_factory = session_factory

    async def launch(self, workspace_id: uuid.UUID) -> AgentHandle:
        """Launch (or return cached) handle. Each fresh launch sweeps
        orphans and attaches resume messages."""
        # If we already have a live handle, just hand it back — no need
        # to re-sweep or rebuild resume.
        cached = await self._registry.get_or_launch_if_exists(workspace_id)
        if cached is not None:
            return cached

        # First launch in this idle window: do the full prelude.
        with self._session_factory() as session:
            acquired = acquire_workspace_lock(session, workspace_id)
            if not acquired:
                # Another instance / process holds the lock. The cached-
                # handle path above is the in-process case; this branch
                # is for multi-instance deployments (out of scope for v1
                # per the spec). For now, raise — multi-instance setup
                # is explicitly deferred per design line 79.
                raise RuntimeError(
                    f"workspace {workspace_id} advisory lock held by "
                    "another process; multi-instance routing not implemented"
                )

            try:
                sweep_orphaned_turn_jobs(session, workspace_id)
                resume_messages = build_resume_messages(session, workspace_id)
                session.commit()
            except Exception:
                release_workspace_lock(session, workspace_id)
                raise
            release_workspace_lock(session, workspace_id)

        handle = await self._registry.get_or_launch(workspace_id)
        handle.state["resume_messages"] = resume_messages
        return handle

    async def send_turn(
        self, workspace_id: uuid.UUID, user_message: str,
    ) -> AsyncIterator[StreamEvent]:
        """Send a turn and stream events. Touches the registry's idle
        timer on completion so the handle doesn't get swept mid-flight."""
        handle = await self.launch(workspace_id)
        async for event in self._registry.send_turn(handle, user_message):
            yield event
        await self._registry.touch(workspace_id)

    async def terminate(self, workspace_id: uuid.UUID) -> None:
        await self._registry.terminate(workspace_id)
```

The `SubprocessRegistry` needs two additional methods that this controller
uses: `get_or_launch_if_exists` and `send_turn`. Update `registry.py`:

```python
    async def get_or_launch_if_exists(
        self, workspace_id: uuid.UUID,
    ) -> AgentHandle | None:
        """Return the cached handle if one exists (and refresh its touch
        timestamp); else None. Does NOT call adapter.launch."""
        async with self._lock:
            entry = self._entries.get(workspace_id)
            if entry is None:
                return None
            entry.last_touched_monotonic = time.monotonic()
            return entry.handle

    async def send_turn(
        self, handle: AgentHandle, user_message: str,
    ) -> "AsyncIterator[StreamEvent]":
        """Pass-through to the adapter's send_turn. The controller calls
        touch() after the iterator is fully consumed."""
        async for event in self._adapter.send_turn(handle, user_message):
            yield event
```

Add the `StreamEvent` import to registry.py's imports.

- [ ] **Step 8: Run + commit**

```bash
uv run pytest tests/proposal_assist/test_drafting_registry.py tests/proposal_assist/test_drafting_controller.py -v
uv run ruff format src/call_monitor/proposal_assist/drafting/registry.py src/call_monitor/proposal_assist/drafting/controller.py tests/proposal_assist/test_drafting_registry.py tests/proposal_assist/test_drafting_controller.py
git add src/call_monitor/proposal_assist/drafting/registry.py src/call_monitor/proposal_assist/drafting/controller.py tests/proposal_assist/test_drafting_registry.py tests/proposal_assist/test_drafting_controller.py
git commit -m "feat(b3): SubprocessRegistry + WorkspaceController"
```

---

## Task 9: `ClaudeAgentSDKAdapter` — real SDK integration

**This is the only task that imports `claude_agent_sdk`.** Treat it as a small discovery exercise first, then code.

**Files:**
- Create: `src/call_monitor/proposal_assist/drafting/claude_adapter.py`
- Test: `tests/proposal_assist/test_drafting_claude_adapter.py`

- [ ] **Step 1: Discovery — read the SDK quickstart**

```bash
# In the worktree, look at what claude-agent-sdk actually exposes.
uv run python -c "import claude_agent_sdk; print(dir(claude_agent_sdk))"
uv run python -c "from claude_agent_sdk import query; help(query)" 2>&1 | head -40
```

Capture for your records (paste into the eventual commit message body):
- The top-level entry point (likely `query()` or `ClaudeSDKClient` — the SDK has changed names).
- The shape of the options/config object (`ClaudeAgentOptions` or `Options`).
- The streaming-message types (likely a tagged union in `types.py`: `AssistantMessage`, `ToolUseBlock`, `ToolResultBlock`, `ResultMessage`).
- How tools are registered (likely `@tool` decorator + `create_sdk_mcp_server()` for in-process MCP).
- How to pre-seed conversation history (likely a `messages=` or similar Option).

If the SDK README isn't enough, the official examples repo (search GitHub for "claude-agent-sdk python") has runnable samples.

- [ ] **Step 2: Write a smoke test that talks to the real SDK**

`tests/proposal_assist/test_drafting_claude_adapter.py`:

```python
"""Real-SDK adapter tests. Skipped unless ANTHROPIC_API_KEY is set in
env — CI without a key shouldn't hit the API."""
import os
import uuid

import pytest

from call_monitor.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    DraftingAgent,
    TurnDoneEvent,
)
from call_monitor.proposal_assist.drafting.claude_adapter import (
    ClaudeAgentSDKAdapter,
)


pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="real SDK tests require ANTHROPIC_API_KEY",
)


@pytest.mark.asyncio
async def test_claude_adapter_implements_protocol():
    adapter = ClaudeAgentSDKAdapter(
        model="claude-haiku-4-5", workspaces_dir="/tmp/test_workspaces",
    )
    assert isinstance(adapter, DraftingAgent)


@pytest.mark.asyncio
async def test_claude_adapter_can_round_trip_a_simple_turn():
    """End-to-end: launch, send a one-line turn, receive at least one
    AssistantTextEvent and a TurnDoneEvent. Uses Haiku for speed/cost."""
    adapter = ClaudeAgentSDKAdapter(
        model="claude-haiku-4-5", workspaces_dir="/tmp/test_workspaces",
    )
    wsid = uuid.uuid4()
    handle = await adapter.launch(wsid)
    try:
        events = []
        async for event in adapter.send_turn(handle, "Say 'hello'."):
            events.append(event)
        assert any(isinstance(e, AssistantTextEvent) for e in events)
        assert isinstance(events[-1], TurnDoneEvent)
    finally:
        await adapter.terminate(handle)
```

- [ ] **Step 3: Implement the adapter**

`src/call_monitor/proposal_assist/drafting/claude_adapter.py` — the structure below is a starting point. **Verify each `claude_agent_sdk` reference against what your Step 1 discovery showed; if the SDK's API differs, adapt this file accordingly.** This is the only file in the codebase that imports `claude_agent_sdk`, so changes are contained.

```python
"""Real Claude Agent SDK adapter.

This file is the only place in the codebase that imports
claude_agent_sdk. An SDK upgrade should touch only this file plus
its test. The DraftingAgent Protocol (contract.py) is stable.

NOTE FOR THE IMPLEMENTER: the SDK API has evolved. The skeleton below
matches the SDK shape as of plan-writing time (May 2026). Cross-check
with `uv run python -c "import claude_agent_sdk; help(...)"` before
trusting any specific call.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

# The exact import set depends on the SDK version. Likely candidates:
#   from claude_agent_sdk import query, ClaudeAgentOptions
#   from claude_agent_sdk.types import (
#       AssistantMessage, ToolUseBlock, ToolResultBlock, ResultMessage,
#   )
# Run the discovery commands from Step 1 to confirm.
import claude_agent_sdk

from call_monitor.proposal_assist.drafting.contract import (
    AgentHandle,
    AssistantTextEvent,
    StreamEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnDoneEvent,
)


class ClaudeAgentSDKAdapter:
    """Wraps claude-agent-sdk in a per-workspace subprocess.

    For each workspace:
      - launch() opens a session against the SDK, seeded with resume
        messages from AgentHandle.state['resume_messages'] (set by
        WorkspaceController).
      - send_turn() streams the SDK's response messages, translating
        each to a StreamEvent the rest of the codebase understands.
      - terminate() closes the session.
    """

    def __init__(
        self,
        *,
        model: str,
        workspaces_dir: str,
        max_tokens: int = 4096,
    ) -> None:
        self._model = model
        self._workspaces_dir = Path(workspaces_dir)
        self._max_tokens = max_tokens

    async def launch(self, workspace_id: uuid.UUID) -> AgentHandle:
        """Create an SDK session for this workspace. The session itself
        is stored on AgentHandle.state so send_turn can talk to it.

        IMPLEMENTER: depending on the SDK shape, you might:
          (a) Hold a long-lived ClaudeSDKClient and call .query() per turn
          (b) Spawn a subprocess and pipe stdio
          (c) Just configure ClaudeAgentOptions and create a fresh query()
              call per send_turn (simplest; lose conversation continuity
              unless you replay history each turn)

        Option (a) matches the spec's 'one agent loop per session' model
        best. The skeleton below assumes (a).
        """
        repo_dir = self._workspaces_dir / str(workspace_id) / "repo"
        # Options is the v0.0.x name; later versions may use ClaudeAgentOptions.
        options = claude_agent_sdk.ClaudeAgentOptions(
            cwd=str(repo_dir),
            model=self._model,
            max_turns=1,
        )
        # Real client construction may differ; verify in Step 1.
        client = claude_agent_sdk.ClaudeSDKClient(options=options)
        return AgentHandle(
            workspace_id=workspace_id,
            state={"client": client, "options": options},
        )

    async def send_turn(
        self, handle: AgentHandle, user_message: str,
    ) -> AsyncIterator[StreamEvent]:
        client = handle.state["client"]
        # The exact call pattern depends on the SDK. As a typical shape:
        #   async with client:
        #       await client.query(user_message)
        #       async for message in client.receive_response():
        #           yield translate(message)
        tokens_in = 0
        tokens_out = 0
        model = self._model

        async with client:
            await client.query(user_message)
            async for message in client.receive_response():
                # Translate SDK message types → our StreamEvent types.
                # The exact attribute names depend on the SDK. Verify
                # with the discovery output from Step 1.
                if isinstance(message, claude_agent_sdk.AssistantMessage):
                    for block in message.content:
                        if hasattr(block, "text"):
                            yield AssistantTextEvent(text=block.text)
                        elif hasattr(block, "tool_use_id"):
                            yield ToolUseEvent(
                                tool_name=block.name,
                                tool_use_id=block.id,
                                input=block.input,
                            )
                elif isinstance(message, claude_agent_sdk.UserMessage):
                    # Tool results come back as UserMessage(content=[ToolResultBlock])
                    for block in message.content:
                        if hasattr(block, "tool_use_id"):
                            yield ToolResultEvent(
                                tool_use_id=block.tool_use_id,
                                content=str(block.content),
                                is_error=bool(getattr(block, "is_error", False)),
                            )
                elif isinstance(message, claude_agent_sdk.ResultMessage):
                    usage = getattr(message, "usage", None)
                    if usage is not None:
                        tokens_in = getattr(usage, "input_tokens", 0)
                        tokens_out = getattr(usage, "output_tokens", 0)
                    yield TurnDoneEvent(
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        model=model,
                    )
                    return

        # Defensive: if the SDK never yielded a ResultMessage, emit a
        # bare TurnDoneEvent so the consumer's loop exits cleanly.
        yield TurnDoneEvent(
            tokens_in=tokens_in or None,
            tokens_out=tokens_out or None,
            model=model,
        )

    async def terminate(self, handle: AgentHandle) -> None:
        client = handle.state.pop("client", None)
        if client is not None and hasattr(client, "close"):
            await client.close()
```

- [ ] **Step 4: Run smoke tests (only with API key)**

```bash
# Without the key, the tests skip:
uv run pytest tests/proposal_assist/test_drafting_claude_adapter.py -v
# Expected: 2 skipped

# With the key set in env (do NOT commit the key):
ANTHROPIC_API_KEY=sk-... uv run pytest tests/proposal_assist/test_drafting_claude_adapter.py -v
# Expected: 2 passed
```

If the second invocation fails: read the error, return to Step 1 (discovery), update the adapter to match the actual SDK API, repeat.

- [ ] **Step 5: Ruff + commit**

```bash
uv run ruff format src/call_monitor/proposal_assist/drafting/claude_adapter.py tests/proposal_assist/test_drafting_claude_adapter.py
uv run ruff check src/call_monitor/proposal_assist/drafting/claude_adapter.py tests/proposal_assist/test_drafting_claude_adapter.py
git add src/call_monitor/proposal_assist/drafting/claude_adapter.py tests/proposal_assist/test_drafting_claude_adapter.py
git commit -m "feat(b3): real ClaudeAgentSDKAdapter

Discovery notes:
- SDK version: <fill in from uv.lock>
- Entry point used: <fill in: query / ClaudeSDKClient / etc.>
- Message types: <fill in>
- Deviations from plan: <fill in if any>
"
```

---

## Task 10: DI factory + milestone

A small factory function that returns a `WorkspaceController` wired up with the right adapter for the environment (Fake in tests; Claude in production). Plus the final milestone (full suite + lint + surface).

**Files:**
- Create: `src/call_monitor/proposal_assist/drafting/factory.py`
- Test: `tests/proposal_assist/test_drafting_factory.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_drafting_factory.py`:

```python
import pytest

from call_monitor.proposal_assist.drafting.contract import DraftingAgent
from call_monitor.proposal_assist.drafting.controller import (
    WorkspaceController,
)
from call_monitor.proposal_assist.drafting.factory import (
    build_workspace_controller,
)


def test_factory_with_fake_adapter_returns_controller(
    pg_session_factory, _settings_env, monkeypatch,
):
    """Setting the env hook for fake adapter yields a working controller
    backed by FakeAgentAdapter (no SDK required)."""
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")
    monkeypatch.setenv("WORKSPACES_DIR", "/tmp/test_workspaces")

    from call_monitor.config import Settings
    settings = Settings()

    controller = build_workspace_controller(
        settings=settings, session_factory=pg_session_factory,
    )
    assert isinstance(controller, WorkspaceController)


def test_factory_with_claude_adapter_returns_controller(
    pg_session_factory, _settings_env, monkeypatch,
):
    """Claude adapter (default) also returns a controller. No real SDK
    calls happen during construction — just object wiring."""
    monkeypatch.delenv("DRAFTING_ADAPTER", raising=False)  # default = claude
    monkeypatch.setenv("WORKSPACES_DIR", "/tmp/test_workspaces")

    from call_monitor.config import Settings
    settings = Settings()

    controller = build_workspace_controller(
        settings=settings, session_factory=pg_session_factory,
    )
    assert isinstance(controller, WorkspaceController)
```

- [ ] **Step 2: Add `drafting_adapter` setting + run failing test**

In `src/call_monitor/config.py`:

```python
    drafting_adapter: str = Field(
        default="claude",
        alias="DRAFTING_ADAPTER",
        description=(
            "Which DraftingAgent implementation to wire up. "
            "'claude' (default) → ClaudeAgentSDKAdapter; "
            "'fake' → FakeAgentAdapter (for integration tests + local "
            "demos without an API key)."
        ),
    )
```

Then:

```bash
uv run pytest tests/proposal_assist/test_drafting_factory.py -v
```

Expected: `ModuleNotFoundError: No module named 'call_monitor.proposal_assist.drafting.factory'`.

- [ ] **Step 3: Implement factory**

`src/call_monitor/proposal_assist/drafting/factory.py`:

```python
"""Build a WorkspaceController for the running environment.

The choice between FakeAgentAdapter and ClaudeAgentSDKAdapter is made
via the DRAFTING_ADAPTER setting. Defaults to 'claude'; tests / demos
set 'fake' to avoid hitting the real API."""
from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from call_monitor.config import Settings
from call_monitor.proposal_assist.drafting.contract import DraftingAgent
from call_monitor.proposal_assist.drafting.controller import (
    WorkspaceController,
)
from call_monitor.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_monitor.proposal_assist.drafting.registry import SubprocessRegistry


def build_workspace_controller(
    *,
    settings: Settings,
    session_factory: sessionmaker,
) -> WorkspaceController:
    adapter = _build_adapter(settings)
    registry = SubprocessRegistry(
        adapter=adapter,
        idle_seconds=settings.workspace_idle_timeout_seconds,
    )
    return WorkspaceController(
        registry=registry, session_factory=session_factory,
    )


def _build_adapter(settings: Settings) -> DraftingAgent:
    choice = settings.drafting_adapter.lower()
    if choice == "fake":
        return FakeAgentAdapter()
    if choice == "claude":
        # Lazy-import the claude adapter so 'fake' deployments don't pay
        # the SDK import cost.
        from call_monitor.proposal_assist.drafting.claude_adapter import (
            ClaudeAgentSDKAdapter,
        )
        return ClaudeAgentSDKAdapter(
            model=settings.proposal_assist_llm_model,
            workspaces_dir=settings.workspaces_dir,
        )
    raise ValueError(
        f"unknown DRAFTING_ADAPTER choice: {choice!r} "
        "(expected 'claude' or 'fake')"
    )
```

- [ ] **Step 4: Run + lint + commit**

```bash
uv run pytest tests/proposal_assist/test_drafting_factory.py -v
uv run ruff format src/call_monitor/proposal_assist/drafting/factory.py tests/proposal_assist/test_drafting_factory.py src/call_monitor/config.py
git add src/call_monitor/proposal_assist/drafting/factory.py tests/proposal_assist/test_drafting_factory.py src/call_monitor/config.py
git commit -m "feat(b3): DraftingAdapter factory + DRAFTING_ADAPTER setting"
```

- [ ] **Step 5: B3 milestone — full suite + cross-cutting checks**

```bash
uv run pytest -q                       # expect ~660+ passing
uv run ruff check src tests
uv run ruff format --check src/call_monitor/proposal_assist/drafting
```

Verify:
- The drafting package's public surface is exactly `DraftingAgent`, `AgentHandle`, the four `StreamEvent` variants, `WorkspaceController`, and `build_workspace_controller`. Nothing else in the codebase should import from `claude_adapter.py` directly.
- A grep confirms it: `grep -rn 'from call_monitor.proposal_assist.drafting.claude_adapter' src tests` returns only `factory.py` (and possibly the adapter's own test file).

Surface to the user:

> B3 complete. The drafting package gives the rest of the codebase a `WorkspaceController` with `launch` / `send_turn` / `terminate`. Real Claude SDK integration lives in one file (`claude_adapter.py`); tests use `FakeAgentAdapter`. Slice A / B / C / D / E and B4–B8 can now build on the contract without re-deriving the subprocess/lock/sweep/resume plumbing.
>
> **Not yet wired:** scheduler tick for periodic idle sweep, FastAPI startup hook that constructs the controller, SSE bridge for streaming events to the browser. Those land in B4 (turn queue) or B6 (web UX).

---

## Self-review notes (for the engineer running this plan)

1. **`pytest-asyncio` mode**: All tests use `async def` + `@pytest.mark.asyncio`. The project's `pyproject.toml` should already have `asyncio_mode = "auto"` (verify with `grep asyncio_mode pyproject.toml`). If auto-mode is enabled, the decorators are redundant but harmless; either form runs.

2. **Advisory lock release on session error**: `locking.release_workspace_lock` is called in a `try/finally`-style pattern in `controller.py`. Postgres also auto-releases on disconnect, but explicit release is cleaner. If the controller's session raises before reaching the unlock call, the session's exit handler closes the connection and Postgres releases the lock at that point — no leak.

3. **Multi-instance routing**: Per the spec, multi-instance deployments are out of scope for v1. The controller's `acquire_workspace_lock` returning False raises a `RuntimeError` with an explicit "not implemented" message — this surfaces the limitation rather than hiding it. When the org goes multi-instance (the spec mentions `workspaces.served_by_instance` as the future fix), revisit this branch.

4. **Resume-message format**: Task 7 produces a list of `{"role": ..., "content": ...}` dicts. The exact `content` shape (string vs list-of-blocks) depends on the SDK's `messages=` option format. Cross-reference with Task 9's discovery. If the SDK wants strings instead of dicts, `resume.py` needs a small flattening pass — track as a Task 9 follow-up.

5. **Tool registration deferred**: B3 produces the plumbing for `ToolUseEvent` / `ToolResultEvent` to flow through. B5 implements actual tool handlers (`edit_proposal`, etc.) and registers them with the Claude adapter at construction time. Until B5 lands, the Claude adapter's tool-routing path is exercised only by the smoke test (which doesn't trigger any tool calls).

6. **No FastAPI integration**: B3 is data-layer + service-layer only. B6 (web UX) will:
   - Construct `build_workspace_controller(...)` in `create_app()`.
   - Stash it on `app.state.workspace_controller`.
   - Add an SSE endpoint that wraps `controller.send_turn(...)`.

7. **Scheduler idle-sweep**: B3 doesn't register the `SubprocessRegistry.sweep_idle` call as a scheduler tick. That's a follow-up (probably in B4 alongside the chat-machinery integration, or in B8 alongside other ops hygiene). For now, idle handles persist until process exit — acceptable for v1 since workspaces are torn down on app restart anyway.
