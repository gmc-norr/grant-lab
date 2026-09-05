"""Per-generation inventory of live SDK clients (Slice A).

The durable ownership anchor for subprocess teardown. Every path that discards a
handle — capacity eviction, idle sweep, retirement teardown, and the launcher's
own cleanup (which suppresses exceptions) — drops its reference BEFORE
termination is confirmed, and the adapter pops handle.state["client"] before
disconnect. Without this inventory a failed teardown leaves nothing to retry and
nothing to count.

Records are keyed by LAUNCH GENERATION, never by workspace: an unconfirmed
teardown releases the workspace's advisory lock, so a fresh launch for the same
workspace can legitimately coexist with an orphan. Clearing is by identity.

Pure bookkeeping — no SDK types, no I/O, no locking (single event loop).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

LIVE = "live"
RETIRING = "retiring"


@dataclass
class LiveClientRecord:
    launch_id: uuid.UUID
    workspace_id: uuid.UUID
    client: Any
    state: str = LIVE
    teardown: asyncio.Task | None = field(default=None, repr=False)


class LiveClientRegistry:
    """Tracks every SDK client from before connect() until confirmed death."""

    def __init__(self) -> None:
        self._records: dict[uuid.UUID, LiveClientRecord] = {}

    def register(self, workspace_id: uuid.UUID, client: Any) -> uuid.UUID:
        """Record a new generation and return its launch_id. Called BEFORE
        client.connect() so a connect-time failure is still discoverable."""
        launch_id = uuid.uuid4()
        self._records[launch_id] = LiveClientRecord(
            launch_id=launch_id, workspace_id=workspace_id, client=client
        )
        return launch_id

    def mark_retiring(self, launch_id: uuid.UUID) -> LiveClientRecord | None:
        """Flag a generation as being torn down. Idempotent; None if unknown."""
        record = self._records.get(launch_id)
        if record is None:
            return None
        record.state = RETIRING
        return record

    def confirm_dead(self, launch_id: uuid.UUID) -> None:
        """Drop a generation — ONLY after its process is confirmed gone. Clearing
        by identity means a late teardown of an orphan can never remove the
        replacement generation for the same workspace."""
        self._records.pop(launch_id, None)

    def get(self, launch_id: uuid.UUID) -> LiveClientRecord | None:
        return self._records.get(launch_id)

    def find_by_workspace(self, workspace_id: uuid.UUID) -> list[LiveClientRecord]:
        return [r for r in self._records.values() if r.workspace_id == workspace_id]

    def records(self) -> list[LiveClientRecord]:
        """Snapshot — safe to iterate while callers mutate the inventory."""
        return list(self._records.values())

    def retiring_count(self) -> int:
        """Generations whose teardown was attempted but not confirmed — the ones
        the registry's _entries no longer knows about, so capacity accounting must
        add them explicitly."""
        return sum(1 for r in self._records.values() if r.state == RETIRING)

    def unconfirmed_count(self) -> int:
        return len(self._records)
