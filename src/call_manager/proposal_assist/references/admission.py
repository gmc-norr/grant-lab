"""Process-wide fail-fast admission limiter shared by every reference ingest
entry point (web upload, web fetch, MCP fetch tool).

Uses threading.Semaphore — NOT asyncio.Semaphore — because turns and web
requests may run on different event loops/threads; a thread semaphore bounds
the real CPU/GPU work regardless. Acquisition is non-blocking: over capacity
raises OverCapacity (mapped to HTTP 429 / a tool 'busy' error)."""

from __future__ import annotations

import contextlib
import threading


class OverCapacity(Exception):
    """No ingest slot free."""


class IngestLimiter:
    def __init__(self, capacity: int):
        self._sem = threading.Semaphore(capacity)

    def try_acquire(self) -> bool:
        return self._sem.acquire(blocking=False)

    def release(self) -> None:
        self._sem.release()

    @contextlib.contextmanager
    def slot(self):
        if not self._sem.acquire(blocking=False):
            raise OverCapacity()
        try:
            yield
        finally:
            self._sem.release()
