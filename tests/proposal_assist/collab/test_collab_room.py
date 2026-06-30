import uuid

import pytest
from pycrdt import Doc, Text

from call_manager.proposal_assist.collab import protocol as P
from call_manager.proposal_assist.collab.room import Room, RoomManager


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeClient:
    """Stand-in for a connected WebSocket; records what the room sends it."""

    def __init__(self):
        self.sent: list[bytes] = []

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)


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
    room.clients.update({writer, reader})
    log, persist = _persist_sink()

    frame = _update_frame("hello")

    await room.handle(sender=writer, user_id=7, can_write=True, message=frame, persist=persist)

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
    room.clients.update({bad, other})
    log, persist = _persist_sink()

    frame = _update_frame("nope")

    await room.handle(sender=bad, user_id=9, can_write=False, message=frame, persist=persist)

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
    room.clients.add(reader)
    log, persist = _persist_sink()

    client_doc = Doc()
    client_doc["body"] = Text()
    step1 = P.create_sync_message(client_doc)

    await room.handle(sender=reader, user_id=3, can_write=False, message=step1, persist=persist)

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
    room.clients.update({a, b})
    _, persist = _persist_sink()

    db = Doc()
    db["body"] = Text()
    frame = _update_frame("from A")

    await room.handle(sender=a, user_id=1, can_write=True, message=frame, persist=persist)
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
    room = await mgr.acquire(key, load=load, client=client)
    assert "recovered state" in str(room.doc.get("body", type=Text))
    assert client in room.clients


@pytest.mark.anyio
async def test_manager_evicts_room_when_last_client_leaves():
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    c1, c2 = FakeClient(), FakeClient()

    async def load():
        return []

    await mgr.acquire(key, load=load, client=c1)
    await mgr.acquire(key, load=load, client=c2)  # same room, second client
    await mgr.release(key, client=c1)
    assert mgr.has_room(key)            # still one client
    await mgr.release(key, client=c2)
    assert not mgr.has_room(key)        # evicted


@pytest.mark.anyio
async def test_persist_failure_poisons_room_and_does_not_broadcast():
    key = (uuid.uuid4(), "draft")
    room = Room(key)
    writer, other = FakeClient(), FakeClient()
    room.clients.update({writer, other})

    async def persist_boom(user_id, update):
        raise RuntimeError("db down")

    from call_manager.proposal_assist.collab.room import PersistError

    frame = _update_frame("doomed")
    with pytest.raises(PersistError):
        await room.handle(
            sender=writer, user_id=1, can_write=True, message=frame, persist=persist_boom
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
    room.clients.update({writer, other})
    log, persist = _persist_sink()

    garbage = P.create_update_message(b"\xde\xad\xbe\xef\xde\xad")  # not a real update
    await room.handle(
        sender=writer, user_id=1, can_write=True, message=garbage, persist=persist
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
    room.clients.update({writer, good, boom})
    _, persist = _persist_sink()

    await room.handle(
        sender=writer, user_id=1, can_write=True, message=_update_frame("x"), persist=persist
    )
    assert good.sent                  # healthy client still received the update
    assert boom not in room.clients   # broken client dropped from the room
