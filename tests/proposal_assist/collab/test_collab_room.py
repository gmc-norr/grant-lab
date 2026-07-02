import asyncio
import uuid

import pytest
from pycrdt import Doc, Text

from call_manager.proposal_assist.collab import protocol as P
from call_manager.proposal_assist.collab.room import ClientState, Room, RoomManager


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeClient:
    """Stand-in for a connected WebSocket; records what the room sends it."""

    def __init__(self):
        self.sent: list[bytes] = []
        self.closed: int | None = None   # records the close code, or None

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed = code


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
    st = ClientState(user_id=7, can_write=True)
    room.clients[writer] = st
    room.clients[reader] = ClientState(user_id=1, can_write=True)
    log, persist = _persist_sink()

    frame = _update_frame("hello")

    await room.handle(sender=writer, state=st, message=frame, persist=persist)

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
    st = ClientState(user_id=9, can_write=False)
    room.clients[bad] = st
    room.clients[other] = ClientState(user_id=1, can_write=True)
    log, persist = _persist_sink()

    frame = _update_frame("nope")

    await room.handle(sender=bad, state=st, message=frame, persist=persist)

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
    st = ClientState(user_id=3, can_write=False)
    room.clients[reader] = st
    log, persist = _persist_sink()

    client_doc = Doc()
    client_doc["body"] = Text()
    step1 = P.create_sync_message(client_doc)

    await room.handle(sender=reader, state=st, message=step1, persist=persist)

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
    st = ClientState(user_id=1, can_write=True)
    room.clients[a] = st
    room.clients[b] = ClientState(user_id=1, can_write=True)
    _, persist = _persist_sink()

    db = Doc()
    db["body"] = Text()
    frame = _update_frame("from A")

    await room.handle(sender=a, state=st, message=frame, persist=persist)
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
    room = await mgr.acquire(
        key, load=load, client=client, state=ClientState(user_id=1, can_write=True)
    )
    assert "recovered state" in str(room.doc.get("body", type=Text))
    assert client in room.clients


@pytest.mark.anyio
async def test_manager_evicts_room_when_last_client_leaves():
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    c1, c2 = FakeClient(), FakeClient()

    async def load():
        return []

    await mgr.acquire(key, load=load, client=c1, state=ClientState(user_id=1, can_write=True))
    await mgr.acquire(
        key, load=load, client=c2, state=ClientState(user_id=1, can_write=True)
    )  # same room, second client
    await mgr.release(key, client=c1)
    assert mgr.has_room(key)            # still one client
    await mgr.release(key, client=c2)
    assert not mgr.has_room(key)        # evicted


@pytest.mark.anyio
async def test_persist_failure_poisons_room_and_does_not_broadcast():
    key = (uuid.uuid4(), "draft")
    room = Room(key)
    writer, other = FakeClient(), FakeClient()
    st = ClientState(user_id=1, can_write=True)
    room.clients[writer] = st
    room.clients[other] = ClientState(user_id=1, can_write=True)

    async def persist_boom(user_id, update):
        raise RuntimeError("db down")

    from call_manager.proposal_assist.collab.room import PersistError

    frame = _update_frame("doomed")
    with pytest.raises(PersistError):
        await room.handle(
            sender=writer, state=st, message=frame, persist=persist_boom
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
    st = ClientState(user_id=1, can_write=True)
    room.clients[writer] = st
    room.clients[other] = ClientState(user_id=1, can_write=True)
    log, persist = _persist_sink()

    garbage = P.create_update_message(b"\xde\xad\xbe\xef\xde\xad")  # not a real update
    await room.handle(
        sender=writer, state=st, message=garbage, persist=persist
    )
    assert log == []
    assert other.sent == []


@pytest.mark.anyio
async def test_malformed_frame_is_dropped_without_persist_or_broadcast():
    # A frame that classifies as a SYNC write ([SYNC][SYNC_UPDATE] header) but whose
    # varuint-prefixed payload is truncated: extract_update raises. The room must drop
    # it (no persist, no broadcast, no exception out of handle).
    key = (uuid.uuid4(), "draft")
    room = Room(key)
    writer, other = FakeClient(), FakeClient()
    st = ClientState(user_id=1, can_write=True)
    room.clients[writer] = st
    room.clients[other] = ClientState(user_id=1, can_write=True)
    log, persist = _persist_sink()

    bad_frame = bytes([0, 2, 0xFF, 0xFF, 0xFF])  # [SYNC][SYNC_UPDATE][truncated varuint]
    await room.handle(
        sender=writer, state=st, message=bad_frame, persist=persist
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
    st = ClientState(user_id=1, can_write=True)
    room.clients[writer] = st
    room.clients[good] = ClientState(user_id=1, can_write=True)
    room.clients[boom] = ClientState(user_id=1, can_write=True)
    _, persist = _persist_sink()

    await room.handle(
        sender=writer, state=st, message=_update_frame("x"), persist=persist
    )
    assert good.sent                  # healthy client still received the update
    assert boom not in room.clients   # broken client dropped from the room


@pytest.mark.anyio
async def test_malformed_step1_frame_is_dropped_without_raising():
    # A frame that classifies as SYNC_STEP1 (starts \x00\x00) but has a
    # truncated/garbage state-vector body must be dropped, NOT raised out of
    # handle (which would crash the connection). Reachable by any member — the
    # read path is ungated.
    key = (uuid.uuid4(), "draft")
    room = Room(key)
    reader = FakeClient()
    st = ClientState(user_id=1, can_write=False)
    room.clients[reader] = st
    _, persist = _persist_sink()

    for bad in (b"\x00\x00", b"\x00\x00\xff\xff\xff", b"\x00\x00\x01"):
        await room.handle(
            sender=reader, state=st, message=bad, persist=persist
        )  # must not raise
    assert reader.sent == []  # malformed STEP1 produced no reply


@pytest.mark.anyio
async def test_acquire_does_not_serialize_unrelated_rooms():
    # A slow cold-start for room A must NOT block acquisition of unrelated room
    # B (no process-wide head-of-line blocking).
    mgr = RoomManager()
    a_started = asyncio.Event()
    a_release = asyncio.Event()

    async def slow_load_a():
        a_started.set()
        await a_release.wait()
        return []

    async def fast_load_b():
        return []

    key_a = (uuid.uuid4(), "draft")
    key_b = (uuid.uuid4(), "draft")
    ca, cb = FakeClient(), FakeClient()

    st = ClientState(user_id=1, can_write=True)
    task_a = asyncio.create_task(
        mgr.acquire(key_a, load=slow_load_a, client=ca, state=st)
    )
    await a_started.wait()  # A is mid-load, blocked on a_release
    # B must complete without waiting for A's load:
    room_b = await asyncio.wait_for(
        mgr.acquire(key_b, load=fast_load_b, client=cb, state=st),
        timeout=1.0,
    )
    assert room_b.key == key_b

    a_release.set()
    room_a = await task_a
    assert room_a.key == key_a


@pytest.mark.anyio
async def test_same_key_concurrent_acquire_loads_once():
    # Two concurrent acquirers of the SAME new room hydrate it exactly once and
    # both get the same Room instance.
    mgr = RoomManager()
    calls = 0
    gate = asyncio.Event()

    async def load():
        nonlocal calls
        calls += 1
        await gate.wait()
        return []

    key = (uuid.uuid4(), "draft")
    c1, c2 = FakeClient(), FakeClient()
    t1 = asyncio.create_task(
        mgr.acquire(key, load=load, client=c1, state=ClientState(user_id=1, can_write=True))
    )
    t2 = asyncio.create_task(
        mgr.acquire(key, load=load, client=c2, state=ClientState(user_id=1, can_write=True))
    )
    await asyncio.sleep(0)  # let both enter acquire
    gate.set()
    r1 = await t1
    r2 = await t2
    assert r1 is r2
    assert calls == 1
    assert {c1, c2} <= r1.clients.keys()


@pytest.mark.anyio
async def test_acquire_stores_client_state_and_state_is_mutable():
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    client = FakeClient()
    state = ClientState(user_id=7, can_write=False)

    async def _load():
        return []

    room = await mgr.acquire(key, load=_load, client=client, state=state)
    # the stored state is the SAME object the caller holds
    assert room.clients[client] is state
    # a later flip is visible via the room mapping (reconciliation relies on this)
    state.can_write = True
    assert room.clients[client].can_write is True


@pytest.mark.anyio
async def test_write_blocked_on_lock_rechecks_live_permission():
    key = (uuid.uuid4(), "draft")
    room = Room(key)
    client = FakeClient()
    state = ClientState(user_id=7, can_write=True)
    room.clients[client] = state
    log, persist = _persist_sink()
    frame = _update_frame("late")
    # Hold the room lock so the write frame blocks INSIDE handle at `async with self._lock`.
    async with room.lock:
        task = asyncio.create_task(
            room.handle(sender=client, state=state, message=frame, persist=persist)
        )
        await asyncio.sleep(0)      # let handle pass the early check and block on the lock
        state.can_write = False     # revoke WHILE the frame waits for the lock
    await task                      # lock released → handle proceeds, must re-check live state
    assert log == []                                            # NOT persisted
    assert "late" not in str(room.doc.get("body", type=Text))  # NOT applied


@pytest.mark.anyio
async def test_evict_closes_all_client_sockets():
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    a, b = FakeClient(), FakeClient()

    async def _load():
        return []

    await mgr.acquire(key, load=_load, client=a, state=ClientState(user_id=1, can_write=True))
    await mgr.acquire(key, load=_load, client=b, state=ClientState(user_id=2, can_write=True))
    await mgr.evict(key)
    assert a.closed == 1011 and b.closed == 1011      # server-error close on poison evict
    assert mgr.get(key) is None                        # room removed
