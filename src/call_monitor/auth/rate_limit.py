import threading
import time
from collections import deque
from collections.abc import Callable


class SlidingWindowLimiter:
    """In-memory sliding-window counter.

    Single-instance deploy assumption — safe because APScheduler/FastAPI run
    as exactly one process. For multi-instance we'd promote this to a
    Postgres table or Redis.

    Thread-safe: FastAPI sync handlers run in anyio's thread pool, so two
    `/login` POSTs can race the read-then-modify pattern below. A single
    lock around the whole body costs essentially nothing at our request
    rates and removes the race.

    `name` is a human label used in logs/metrics so a future observability
    pass can tell the email- and IP-limiter counters apart without spelunking.
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: int,
        *,
        name: str = "unnamed",
        clock: Callable[[], float] = time.monotonic,
    ):
        self._max = max_requests
        self._window = window_seconds
        self._clock = clock
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self.name = name

    def allow(self, key: str) -> bool:
        with self._lock:
            now = self._clock()
            q = self._events.get(key)
            cutoff = now - self._window
            if q is not None:
                while q and q[0] < cutoff:
                    q.popleft()
            if q is not None and len(q) >= self._max:
                return False
            if q is None:
                q = deque()
                self._events[key] = q
            q.append(now)
            return True

    def sweep(self) -> int:
        """Drop keys whose entire event window has expired. Callable from
        a periodic job if memory pressure becomes a concern; not wired up
        now since the dict is already bounded by client-IP + per-email."""
        with self._lock:
            now = self._clock()
            cutoff = now - self._window
            removed = 0
            for key in list(self._events.keys()):
                q = self._events[key]
                while q and q[0] < cutoff:
                    q.popleft()
                if not q:
                    del self._events[key]
                    removed += 1
            return removed
