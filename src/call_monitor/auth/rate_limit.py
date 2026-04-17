import threading
import time
from collections import defaultdict, deque
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
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._max = max_requests
        self._window = window_seconds
        self._clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        with self._lock:
            now = self._clock()
            q = self._events[key]
            cutoff = now - self._window
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self._max:
                return False
            q.append(now)
            return True
