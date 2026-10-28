"""Review 2026-10-26 A4: pool exhaustion froze the whole event loop.

`web/api/turns.py` `_json_turn_stream` checked out a SYNC Session on the event
loop every 0.2 s. With the pool exhausted, that checkout blocks the loop
thread for the whole of `pool_timeout` — the reviewer's probe held all 30
connections of the production engine, ran one stream step beside a 50 ms
asyncio ticker, and measured `TimeoutError after 30.0s | ticker max gap
30.05s`. Every collab WebSocket, every SSE stream and every pass coroutine in
the process stalls with it, when only the requests that wanted a connection
should fail.

Two halves to the fix, tested separately:

* the stream's DB work (its prologue AND its poll body) runs in
  `asyncio.to_thread`, so a blocking checkout happens on a worker thread and
  the loop keeps running;
* `pool_timeout=5` on `make_engine`, so the request that cannot get a
  connection fails in seconds instead of parking for half a minute.

Every gap assertion is paired with two others: that the pool really was
exhausted (the step raises the pool's own TimeoutError) and that the ticker
really ticked more than once. A "max gap under 1 s" assertion on a step that
never ran, or beside a ticker that never ran, is green whatever the code does.

Deliberately NOT driven through `with TestClient(app):` — the lifespan portal
serializes threads and would mask exactly the blocking this measures.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import TimeoutError as SAPoolTimeout

from grant_lab.db.session import make_engine, make_session_factory

# Long enough that a blocked loop is unmistakable against the 1 s ceiling,
# short enough that the test costs a few seconds.
_TINY_POOL_TIMEOUT = 3.0


async def _run_beside_a_ticker(awaitable, *, tick: float = 0.05, settle: float = 0.15):
    """Await `awaitable` while a real 50 ms asyncio ticker runs on the same loop.

    Returns `(("ok", value) | ("raised", exc), gaps)` where `gaps` holds the
    ticker's observed intervals DURING the awaited step only.
    """
    gaps: list[float] = []
    stop = asyncio.Event()

    async def _ticker() -> None:
        prev = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(tick)
            now = time.monotonic()
            gaps.append(now - prev)
            prev = now

    ticker = asyncio.create_task(_ticker())
    await asyncio.sleep(settle)  # the ticker is already running before we start
    baseline = len(gaps)
    try:
        outcome: tuple[str, object] = ("ok", await awaitable)
    except Exception as exc:  # noqa: BLE001 — the pool TimeoutError IS the result
        outcome = ("raised", exc)
    finally:
        stop.set()
        await ticker
    return outcome, gaps[baseline:]


class _Grabber:
    """A background thread that takes a pool connection the moment one is free
    and holds it.

    Needed because the stream competes for the pool with OTHER requests, which
    is what exhaustion means. Simply holding N connections before the step is
    not enough: before the fix the generator yields from INSIDE
    `with session_factory()`, so it is holding a connection of its own while
    suspended and would just get that same one back on the next poll. A thread
    already blocked in `engine.connect()` is first in the pool's wait queue, so
    it takes the connection the stream releases — which makes "the next poll
    has to wait" deterministic instead of a race.
    """

    def __init__(self, engine) -> None:
        self._engine = engine
        self.got = threading.Event()
        self._release = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="pool-grabber", daemon=True)

    def _run(self) -> None:
        try:
            conn = self._engine.connect()
        except BaseException as exc:  # noqa: BLE001 — reported back to the test
            self._error = exc
            self.got.set()
            return
        try:
            self.got.set()
            self._release.wait(60)
        finally:
            conn.close()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._release.set()
        self._thread.join(timeout=10)

    def check(self) -> None:
        assert self._error is None, f"the grabber thread failed: {self._error!r}"
        assert self.got.is_set(), "the grabber never took a connection"


def _seed_running_job(pg_session_factory):
    """A workspace with one user turn and a RUNNING job, so the stream polls
    forever instead of emitting `done` on its first step."""
    from grant_lab.db.models import ChatTurn, TurnJob, User, Workspace

    with pg_session_factory() as s:
        owner = User(status="active", email="pool-probe-owner@umu.se")
        s.add(owner)
        s.flush()
        ws = Workspace(title="Pool probe", owner_id=owner.id, current_phase="active")
        s.add(ws)
        s.flush()
        turn = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(turn)
        s.flush()
        job = TurnJob(workspace_id=ws.id, user_turn_id=turn.id, status="running")
        s.add(job)
        s.commit()
        return ws.id, job.id, turn.position


def _open_stream(session_factory, ws_id, job_id, since):
    from grant_lab.web.api.turns import _json_turn_stream

    return _json_turn_stream(
        session_factory=session_factory,
        workspace_id=ws_id,
        turn_job_id=job_id,
        since_position=since,
        poll_seconds=0.01,
        request=None,
    )


async def test_the_event_loop_keeps_ticking_while_a_turn_poll_waits_for_a_connection(
    pg_url, pg_session_factory
):
    """The A4 finding itself: the 0.2 s poll body."""
    ws_id, job_id, since = _seed_running_job(pg_session_factory)

    engine = create_engine(pg_url, pool_size=1, max_overflow=0, pool_timeout=_TINY_POOL_TIMEOUT)
    session_factory = make_session_factory(engine)
    gen = _open_stream(session_factory, ws_id, job_id, since)
    grabber = _Grabber(engine)
    try:
        # Positive control: with the pool free, a poll still does its job. A
        # "fix" that stopped polling the database would pass every gap
        # assertion below.
        first = await anext(gen)
        assert first == ": keepalive\n\n", first

        # Take the pool away from the stream's NEXT poll.
        grabber.start()
        grabber.got.wait(1.0)  # already set if the stream holds nothing

        outcome, gaps = await _run_beside_a_ticker(anext(gen))
        kind, payload = outcome

        # Proof the pool really was exhausted for this poll, and that the
        # exhaustion is SURFACED rather than swallowed into a silent `done`.
        grabber.check()
        assert engine.pool.checkedout() == engine.pool.size() == 1, engine.pool.status()
        assert kind == "raised", f"the poll did not hit pool exhaustion: {payload!r}"
        assert isinstance(payload, SAPoolTimeout), repr(payload)
        assert "QueuePool limit" in str(payload), str(payload)

        assert gaps, "the ticker never completed an interval; nothing was measured"
        assert max(gaps) < 1.0, (
            f"the event loop was blocked for {max(gaps):.2f}s while a turn-stream "
            f"poll waited for a DB connection (A4 measured a 30.05s gap); "
            f"gaps={[round(g, 3) for g in gaps]}"
        )
        assert len(gaps) > 1, f"the ticker did not tick more than once: {gaps}"
    finally:
        await gen.aclose()
        grabber.stop()
        engine.dispose()


async def test_the_stream_prologue_also_stays_off_the_loop(pg_url, pg_session_factory):
    """The first DB access of a stream is its own checkout. If only the poll
    loop moved to a thread, OPENING a stream under an exhausted pool still
    freezes every other coroutine in the process."""
    ws_id, job_id, since = _seed_running_job(pg_session_factory)

    engine = create_engine(pg_url, pool_size=1, max_overflow=0, pool_timeout=_TINY_POOL_TIMEOUT)
    session_factory = make_session_factory(engine)
    gen = _open_stream(session_factory, ws_id, job_id, since)
    held: list = []
    try:
        held.append(engine.connect())
        assert held[0].execute(text("SELECT 1")).scalar() == 1
        assert engine.pool.checkedout() == engine.pool.size() == 1, engine.pool.status()

        outcome, gaps = await _run_beside_a_ticker(anext(gen))
        kind, payload = outcome

        assert kind == "raised", f"the prologue did not hit pool exhaustion: {payload!r}"
        assert isinstance(payload, SAPoolTimeout), repr(payload)

        assert gaps, "the ticker never completed an interval; nothing was measured"
        assert max(gaps) < 1.0, (
            f"the event loop was blocked for {max(gaps):.2f}s while a turn "
            f"stream opened; gaps={[round(g, 3) for g in gaps]}"
        )
        assert len(gaps) > 1, f"the ticker did not tick more than once: {gaps}"
    finally:
        await gen.aclose()
        for conn in held:
            conn.close()
        engine.dispose()


def test_make_engine_fails_the_request_after_five_seconds_not_thirty(pg_url):
    """`pool_timeout=5` (A4 fix): exhaustion must fail the one request that
    wanted a connection, not park it for SQLAlchemy's 30 s default."""
    engine = make_engine(pg_url)
    held: list = []
    try:
        # Positive control: a normal request gets a connection and works.
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1

        assert engine.pool.size() == 10, engine.pool.status()
        for _ in range(30):  # pool_size 10 + max_overflow 20
            held.append(engine.connect())
        assert engine.pool.checkedout() == 30, engine.pool.status()

        started = time.monotonic()
        with pytest.raises(SAPoolTimeout) as excinfo:
            engine.connect()
        elapsed = time.monotonic() - started
        assert "QueuePool limit" in str(excinfo.value), str(excinfo.value)
        assert 4.0 <= elapsed < 15.0, (
            f"a request waited {elapsed:.1f}s for a connection; pool_timeout is "
            "not the configured 5s (SQLAlchemy's default is 30s)"
        )

        # Positive control the other way: once a connection is returned, the
        # next request gets one immediately.
        held.pop().close()
        started = time.monotonic()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
        assert time.monotonic() - started < 2.0
    finally:
        for conn in held:
            conn.close()
        engine.dispose()
