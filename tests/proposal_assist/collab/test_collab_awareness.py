import uuid

import pytest
from pycrdt import Awareness, Doc

from call_manager.proposal_assist.collab import protocol as P
from call_manager.proposal_assist.collab.room import ClientState, Room


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeClient:
    def __init__(self):
        self.sent: list[bytes] = []
        self.closed: int | None = None

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed = code


def _awareness_frame(user):
    d = Doc()
    a = Awareness(d)
    a.set_local_state(user)
    frame = P.create_awareness_message(a.encode_awareness_update([a.client_id]))
    return frame, a.client_id


async def _noop_persist(user_id, update):
    raise AssertionError("awareness must never be persisted")


@pytest.mark.anyio
async def test_awareness_relayed_to_others_not_persisted_not_to_sender():
    room = Room((uuid.uuid4(), "draft"))
    sender, other = FakeClient(), FakeClient()
    st = ClientState(user_id=7, can_write=True)
    room.clients[sender] = st
    room.clients[other] = ClientState(user_id=1, can_write=True)
    frame, cid = _awareness_frame({"user": {"name": "Alice"}})

    await room.handle(sender=sender, state=st, message=frame, persist=_noop_persist)

    assert other.sent and not sender.sent          # relayed to peer, not echoed
    assert P.is_awareness(other.sent[-1])
    assert cid in st.awareness_ids                  # attributed to the sender socket
    assert cid in room.awareness.states


@pytest.mark.anyio
async def test_awareness_is_ungated_read_only_sender_still_relays():
    room = Room((uuid.uuid4(), "draft"))
    reader, other = FakeClient(), FakeClient()
    st = ClientState(user_id=9, can_write=False)     # read-only
    room.clients[reader] = st
    room.clients[other] = ClientState(user_id=1, can_write=True)
    frame, cid = _awareness_frame({"user": {"name": "ReadOnly"}})

    await room.handle(sender=reader, state=st, message=frame, persist=_noop_persist)

    assert other.sent                                # presence relayed even for a non-writer
    assert cid in st.awareness_ids


@pytest.mark.anyio
async def test_same_clientid_reconnect_is_attributed_via_payload_parse():
    # A client id already present in room.awareness (its old state lingers); a NEW
    # socket sends an update for the SAME id. The states-diff yields nothing, so
    # attribution must come from parsing the payload (R2-2).
    room = Room((uuid.uuid4(), "draft"))
    first, second, other = FakeClient(), FakeClient(), FakeClient()
    frame, cid = _awareness_frame({"user": {"name": "Ghost"}})
    st1 = ClientState(user_id=1, can_write=True)
    room.clients[first] = st1
    room.clients[other] = ClientState(user_id=2, can_write=True)
    await room.handle(sender=first, state=st1, message=frame, persist=_noop_persist)
    assert cid in room.awareness.states              # old state lingers

    st2 = ClientState(user_id=1, can_write=True)      # reconnect, same doc/client id
    room.clients[second] = st2
    await room.handle(sender=second, state=st2, message=frame, persist=_noop_persist)
    assert cid in st2.awareness_ids                   # attributed despite empty diff


@pytest.mark.anyio
async def test_malformed_awareness_dropped_without_raising():
    room = Room((uuid.uuid4(), "draft"))
    sender, other = FakeClient(), FakeClient()
    st = ClientState(user_id=1, can_write=True)
    room.clients[sender] = st
    room.clients[other] = ClientState(user_id=2, can_write=True)
    bad = bytes([1, 0xFF, 0xFF, 0xFF])                # AWARENESS type + truncated payload
    await room.handle(sender=sender, state=st, message=bad, persist=_noop_persist)  # no raise
    assert other.sent == []


@pytest.mark.anyio
async def test_room_awareness_has_no_server_local_state():
    room = Room((uuid.uuid4(), "draft"))
    assert room.awareness.states == {}                # set_local_state(None) at init


@pytest.mark.anyio
async def test_awareness_snapshot_primes_a_new_joiner():
    room = Room((uuid.uuid4(), "draft"))
    a = FakeClient()
    st = ClientState(user_id=1, can_write=True)
    room.clients[a] = st
    frame, cid = _awareness_frame({"user": {"name": "Present"}})
    await room.handle(sender=a, state=st, message=frame, persist=_noop_persist)

    snapshot = await room.awareness_snapshot()
    assert snapshot is not None and P.is_awareness(snapshot)
    # a fresh awareness applying the snapshot sees the present user
    joiner = Awareness(Doc())
    joiner.set_local_state(None)
    joiner.apply_awareness_update(P.extract_awareness_update(snapshot), "srv")
    assert cid in joiner.states


@pytest.mark.anyio
async def test_awareness_snapshot_is_none_when_empty():
    room = Room((uuid.uuid4(), "draft"))
    assert await room.awareness_snapshot() is None


# ---------------------------------------------------------------------------
# Task 6: presence removal on teardown paths
# ---------------------------------------------------------------------------

from call_manager.proposal_assist.collab.room import RoomManager  # noqa: E402


@pytest.mark.anyio
async def test_release_removes_departed_presence_for_survivors():
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    a, b = FakeClient(), FakeClient()

    async def _load():
        return []

    sta = ClientState(user_id=1, can_write=True)
    await mgr.acquire(key, load=_load, client=a, state=sta)
    await mgr.acquire(key, load=_load, client=b, state=ClientState(user_id=2, can_write=True))
    room = mgr.get(key)
    frame, cid = _awareness_frame({"user": {"name": "Alice"}})
    await room.handle(sender=a, state=sta, message=frame, persist=_noop_persist)
    b.sent.clear()

    await mgr.release(key, client=a)                 # A leaves, B remains
    assert b.sent and P.is_awareness(b.sent[-1])     # B told to drop A's caret
    assert cid not in room.awareness.states


@pytest.mark.anyio
async def test_drop_client_removes_departed_presence_for_survivors():
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    a, b = FakeClient(), FakeClient()

    async def _load():
        return []

    sta = ClientState(user_id=1, can_write=True)
    await mgr.acquire(key, load=_load, client=a, state=sta)
    await mgr.acquire(key, load=_load, client=b, state=ClientState(user_id=2, can_write=True))
    room = mgr.get(key)
    frame, cid = _awareness_frame({"user": {"name": "Bob"}})
    await room.handle(sender=a, state=sta, message=frame, persist=_noop_persist)
    b.sent.clear()

    await mgr.drop_client(key, a)                     # revoke path
    assert b.sent and P.is_awareness(b.sent[-1])
    assert cid not in room.awareness.states


@pytest.mark.anyio
async def test_release_last_client_no_presence_broadcast_no_crash():
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    a = FakeClient()

    async def _load():
        return []

    sta = ClientState(user_id=1, can_write=True)
    await mgr.acquire(key, load=_load, client=a, state=sta)
    room = mgr.get(key)
    frame, _ = _awareness_frame({"user": {"name": "Solo"}})
    await room.handle(sender=a, state=sta, message=frame, persist=_noop_persist)
    await mgr.release(key, client=a)                   # last client → room evicted, no survivor
    assert not mgr.has_room(key)


@pytest.mark.anyio
async def test_broadcast_drop_of_failing_client_scrubs_its_presence():
    # A client whose send raises is popped by _broadcast DIRECTLY. Its ClientState
    # is then unrecoverable by a later release()/drop_client(), so _broadcast must
    # scrub its awareness from room.awareness — else the stale id leaks into every
    # future new-joiner snapshot.
    room = Room((uuid.uuid4(), "draft"))

    class BoomClient(FakeClient):
        async def send_bytes(self, data):
            raise RuntimeError("socket closed")

    good, boom = FakeClient(), BoomClient()
    st_good = ClientState(user_id=1, can_write=True)
    st_boom = ClientState(user_id=2, can_write=True)
    room.clients[good] = st_good
    room.clients[boom] = st_boom
    frame, cid = _awareness_frame({"user": {"name": "Boom"}})
    await room.handle(sender=boom, state=st_boom, message=frame, persist=_noop_persist)
    assert cid in room.awareness.states

    # Any broadcast that targets boom drops it (send raises) and scrubs its presence.
    await room.broadcast(P.create_awareness_message(frame))
    assert boom not in room.clients
    assert cid not in room.awareness.states
    assert await room.awareness_snapshot() is None      # no leak into snapshots
