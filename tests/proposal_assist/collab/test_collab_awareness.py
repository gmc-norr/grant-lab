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
