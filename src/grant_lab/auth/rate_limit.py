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
        if self._max <= 0:
            return False
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
        """Drop keys whose entire event window has expired.

        IS wired to a scheduler: `main.py` builds every limiter passed to
        `build_scheduler(limiters=[...])`, and `scheduler.py`'s
        `_rate_limit_sweep` job calls `sweep()` on each of them hourly, so
        memory stays bounded in a long-running process. `allow()` above
        never deletes a key on its own -- it only pops expired timestamps
        off the deque -- so a limiter left OUT of that `limiters=[...]` list
        leaks one dict entry per distinct key forever. That is exactly the
        board (public, unauthenticated) route's limiter: it must be in the
        list too, not just the email/IP pair used by authenticated routes.
        """
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
