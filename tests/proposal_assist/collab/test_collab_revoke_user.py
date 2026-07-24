"""Session-revocation: user-keyed socket teardown (revoke_user_sockets),
RoomManager.snapshot_rooms, and the strong bounded barrier signal_user_revocation."""
import asyncio
import threading
import uuid

import pytest

from call_manager.proposal_assist.collab import permissions as perms
from call_manager.proposal_assist.collab.room import MANAGER, ClientState


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeClient:
    """Stand-in socket that records what it was sent and its close code."""

    def __init__(self):
        self.sent: list[bytes] = []
        self.closed: int | None = None

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed = code


class BoomCloseClient(FakeClient):
    """close() records the code then raises — proves durability (Codex #2): the
    socket must STILL be deregistered when close() fails."""

    async def close(self, code: int = 1000) -> None:
        self.closed = code
        raise RuntimeError("close failed")


async def _register(key, client, user_id, can_write=True):
    async def _load():
        return []

    await MANAGER.acquire(
        key, load=_load, client=client,
        state=ClientState(user_id=user_id, can_write=can_write),
    )


# ---------------------------------------------------------------------------
# snapshot_rooms
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_snapshot_rooms_returns_live_rooms():
    key = (uuid.uuid4(), "draft")
    c = FakeClient()
    await _register(key, c, user_id=1)
    try:
        rooms = await MANAGER.snapshot_rooms()
        assert any(r.key == key for r in rooms)
    finally:
        await MANAGER.drop_client(key, c)


# ---------------------------------------------------------------------------
# revoke_user_sockets
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_revoke_closes_and_deregisters_target_leaves_others(pg_session_factory, tmp_path):
    # Two rooms; user 7 has a socket in each; user 9 shares a room. Revoking 7
    # tears down BOTH of 7's sockets and removes them from room.clients; 9 stays.
    k1, k2 = (uuid.uuid4(), "draft"), (uuid.uuid4(), "draft")
    a1, a2, other = FakeClient(), FakeClient(), FakeClient()
    await _register(k1, a1, user_id=7)
    await _register(k1, other, user_id=9)
    await _register(k2, a2, user_id=7)
    try:
        n = await perms.revoke_user_sockets(pg_session_factory, str(tmp_path), 7)
        assert n == 2
        r1, r2 = MANAGER.get(k1), MANAGER.get(k2)
        assert a1 not in r1.clients               # target torn down
        assert a2 not in (r2.clients if r2 else {})  # k2 emptied -> evicted
        assert other in r1.clients                # user 9 untouched
        assert a1.closed == 1008 and a2.closed == 1008
    finally:
        await MANAGER.drop_client(k1, other)
        MANAGER._rooms.pop(k1, None)
        MANAGER._rooms.pop(k2, None)


@pytest.mark.anyio
async def test_revoke_flips_can_write_and_drops_inflight_write(pg_session_factory, tmp_path):
    key = (uuid.uuid4(), "draft")
    victim = FakeClient()
    await _register(key, victim, user_id=7, can_write=True)
    room = MANAGER.get(key)
    state = room.clients[victim]
    try:
        await perms.revoke_user_sockets(pg_session_factory, str(tmp_path), 7)
        assert state.can_write is False           # security barrier flipped
        assert victim not in room.clients          # durable: deregistered
    finally:
        MANAGER._rooms.pop(key, None)


@pytest.mark.anyio
async def test_close_failure_does_not_skip_deregistration(pg_session_factory, tmp_path):
    # Codex #2 durability: if client.close() raises, the socket must STILL be
    # deregistered (else a later reconcile re-grants write).
    key = (uuid.uuid4(), "draft")
    boom = BoomCloseClient()
    await _register(key, boom, user_id=7)
    room = MANAGER.get(key)
    try:
        n = await perms.revoke_user_sockets(pg_session_factory, str(tmp_path), 7)
        assert n == 1
        assert boom not in room.clients            # deregistered despite close() raising
    finally:
        MANAGER._rooms.pop(key, None)


@pytest.mark.anyio
async def test_revoke_last_client_runs_checkpoint(pg_session_factory, tmp_path, monkeypatch):
    # A revoke that empties a LIVE room goes through release(on_evict=checkpoint),
    # preserving the normal last-leave checkpoint semantics (mirrors
    # test_refresh_live_last_client_revoke_checkpoints).
    seen = {"factory": 0, "evict": 0}

    def _spy_factory(sf, wd, wid, art):
        seen["factory"] += 1

        async def _cb(_room):
            seen["evict"] += 1

        return _cb

    monkeypatch.setattr(perms, "make_checkpoint_on_evict", _spy_factory)
    key = (uuid.uuid4(), "draft")
    victim = FakeClient()
    await _register(key, victim, user_id=7)
    n = await perms.revoke_user_sockets(pg_session_factory, str(tmp_path), 7)
    assert n == 1
    assert MANAGER.get(key) is None                      # last client -> evicted
    assert seen["factory"] == 1 and seen["evict"] == 1   # checkpoint path taken


@pytest.mark.anyio
async def test_revoke_no_match_is_noop(pg_session_factory, tmp_path):
    # A user with no live sockets: sweep touches nothing, returns 0.
    key = (uuid.uuid4(), "draft")
    other = FakeClient()
    await _register(key, other, user_id=9)
    try:
        n = await perms.revoke_user_sockets(pg_session_factory, str(tmp_path), 7)
        assert n == 0
        assert other in MANAGER.get(key).clients
    finally:
        await MANAGER.drop_client(key, other)


# ---------------------------------------------------------------------------
# signal_user_revocation — strong bounded barrier
# ---------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self, loop, session_factory, workspaces_dir):
        self.app = type("A", (), {})()
        self.app.state = type("S", (), {})()
        self.app.state.app_loop = loop
        self.app.state.session_factory = session_factory
        self.app.state.settings = type("Cfg", (), {"workspaces_dir": workspaces_dir})()


def test_signal_user_revocation_no_loop_is_noop_no_raise():
    # No captured app loop (e.g. a route hit before lifespan / in a headless run):
    # returns before touching session_factory, never raises into the route.
    req = _FakeRequest(None, None, "/tmp")
    perms.signal_user_revocation(req, 7)  # must not raise


def test_signal_user_revocation_blocks_until_teardown_completes(
    pg_session_factory, tmp_path, monkeypatch
):
    # Strong barrier (Codex #3): the sync call must not return until the scheduled
    # coroutine has finished. A blocking fut.result() cannot run on its own loop's
    # thread, so the loop lives in a SEPARATE thread — the production topology
    # (sync route thread -> event loop thread).
    #
    # CRITICAL (self-review M1): do NOT reuse the module-global MANAGER here. Its
    # asyncio.Lock was bound to the pytest-anyio loop by earlier tests, so using
    # it from this background loop raises "got Future attached to a different
    # loop". Use a FRESH RoomManager monkeypatched into permissions.MANAGER and do
    # ALL manager ops on the background loop, so its lock binds to THIS loop.
    from call_manager.proposal_assist.collab.room import RoomManager

    fresh = RoomManager()
    monkeypatch.setattr(perms, "MANAGER", fresh)

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    key = (uuid.uuid4(), "draft")
    c = FakeClient()

    async def _register_on_loop():
        async def _load():
            return []

        await fresh.acquire(
            key, load=_load, client=c,
            state=ClientState(user_id=7, can_write=True),
        )

    asyncio.run_coroutine_threadsafe(_register_on_loop(), loop).result(5)
    try:
        req = _FakeRequest(loop, pg_session_factory, str(tmp_path))
        perms.signal_user_revocation(req, 7)     # blocks on the barrier
        # By the time signal returns, the socket is already torn down.
        room = fresh.get(key)
        assert room is None or c not in room.clients
        assert c.closed == 1008
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)
        loop.close()
