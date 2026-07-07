import logging
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
async def test_declaring_a_different_users_live_id_does_not_capture_it():
    # Griefing guard (Slice 3b-5b #2): a member must not capture an awareness id a
    # LIVE peer of a DIFFERENT user already owns — otherwise the attacker's
    # disconnect would scrub the victim's caret for everyone.
    room = Room((uuid.uuid4(), "draft"))
    victim, attacker = FakeClient(), FakeClient()
    st_victim = ClientState(user_id=1, can_write=True)
    st_attacker = ClientState(user_id=2, can_write=True)   # DIFFERENT user
    room.clients[victim] = st_victim
    room.clients[attacker] = st_attacker
    frame, cid = _awareness_frame({"user": {"name": "Victim"}})

    await room.handle(sender=victim, state=st_victim, message=frame, persist=_noop_persist)
    assert cid in st_victim.awareness_ids                  # victim owns its id

    # attacker (different user) REPLAYS the victim's frame, declaring the victim's id
    await room.handle(sender=attacker, state=st_attacker, message=frame, persist=_noop_persist)
    assert cid not in st_attacker.awareness_ids            # guard: not captured

    # the victim's caret survives the attacker's disconnect
    await room.remove_client_presence(st_attacker)
    assert cid in room.awareness.states


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


# ---------------------------------------------------------------------------
# 3b-5d.D2: awareness relay-spoof ownership filter
# ---------------------------------------------------------------------------

ROOM_LOGGER = "call_manager.proposal_assist.collab.room"


def _vu(n: int) -> bytes:
    """Base-128 varuint (y-protocols encoding) — test-local writer."""
    out = bytearray()
    while n > 0x7F:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    out.append(n)
    return bytes(out)


def _entry(cid: int, clock: int, state_json: bytes) -> bytes:
    """One awareness entry: [clientID varuint][clock varuint][varString state]."""
    return _vu(cid) + _vu(clock) + _vu(len(state_json)) + state_json


def _crafted_frame(*entries: bytes) -> bytes:
    """A full AWARENESS wire frame from hand-crafted entries (attacker's view)."""
    payload = _vu(len(entries)) + b"".join(entries)
    return P.create_awareness_message(payload)


@pytest.mark.anyio
async def test_spoof_hide_blocked_and_flagged(caplog):
    # THE regression (3b-5b REQUIRED deferral): user B sends a high-clock
    # non-null override of A's id — must be filtered before ANYONE (the victim
    # included) receives it, must not poison the server snapshot, and must fire
    # the WARNING spoof signature.
    room = Room((uuid.uuid4(), "draft"))
    victim, attacker, observer = FakeClient(), FakeClient(), FakeClient()
    st_v = ClientState(user_id=1, can_write=True)
    st_a = ClientState(user_id=2, can_write=True)
    room.clients[victim] = st_v
    room.clients[attacker] = st_a
    room.clients[observer] = ClientState(user_id=3, can_write=True)
    frame, cid = _awareness_frame({"user": {"name": "Victim"}})
    await room.handle(sender=victim, state=st_v, message=frame, persist=_noop_persist)
    observer.sent.clear()

    spoof = _crafted_frame(_entry(cid, 10**9, b'{"user":{"name":"Fake"}}'))
    with caplog.at_level(logging.INFO, logger=ROOM_LOGGER):
        await room.handle(sender=attacker, state=st_a, message=spoof, persist=_noop_persist)

    assert observer.sent == []  # not relayed to anyone
    assert room.awareness.states[cid]["user"]["name"] == "Victim"  # state intact
    assert cid not in st_a.awareness_ids  # not attributed to the attacker
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "collab.awareness_foreign_filtered" in warnings
    # A new joiner's snapshot is spoof-free.
    snapshot = await room.awareness_snapshot()
    joiner = Awareness(Doc())
    joiner.set_local_state(None)
    joiner.apply_awareness_update(P.extract_awareness_update(snapshot), "srv")
    assert joiner.states[cid]["user"]["name"] == "Victim"


@pytest.mark.anyio
async def test_foreign_null_scrub_filtered_benignly_at_info(caplog):
    # Legit clients DO send foreign null-state frames all day: y-protocols' 3s
    # local timeout-scrub of a backgrounded tab ships with origin 'timeout',
    # which the provider's origin guard does not suppress. Filtered (each peer
    # scrubs locally — the relay is redundant) but logged INFO, NOT the WARNING
    # spoof signature. Guards the log-level split.
    room = Room((uuid.uuid4(), "draft"))
    a, b = FakeClient(), FakeClient()
    st_a = ClientState(user_id=1, can_write=True)
    st_b = ClientState(user_id=2, can_write=True)
    room.clients[a] = st_a
    room.clients[b] = st_b
    frame, cid = _awareness_frame({"user": {"name": "Backgrounded"}})
    await room.handle(sender=a, state=st_a, message=frame, persist=_noop_persist)
    a.sent.clear()

    scrub = _crafted_frame(_entry(cid, 1, b"null"))
    with caplog.at_level(logging.INFO, logger=ROOM_LOGGER):
        await room.handle(sender=b, state=st_b, message=scrub, persist=_noop_persist)

    assert a.sent == []  # not relayed
    assert cid in room.awareness.states  # A's presence intact
    stale = [
        r for r in caplog.records if r.getMessage() == "collab.awareness_stale_scrub_filtered"
    ]
    assert stale and all(r.levelno == logging.INFO for r in stale)
    assert not [
        r for r in caplog.records if r.getMessage() == "collab.awareness_foreign_filtered"
    ]


@pytest.mark.anyio
async def test_mixed_frame_stripped_to_own_entries():
    room = Room((uuid.uuid4(), "draft"))
    a, b, peer = FakeClient(), FakeClient(), FakeClient()
    st_a = ClientState(user_id=1, can_write=True)
    st_b = ClientState(user_id=2, can_write=True)
    room.clients[a] = st_a
    room.clients[b] = st_b
    room.clients[peer] = ClientState(user_id=3, can_write=True)
    frame, cid_a = _awareness_frame({"user": {"name": "A"}})
    await room.handle(sender=a, state=st_a, message=frame, persist=_noop_persist)
    peer.sent.clear()

    own_id = 555
    mixed = _crafted_frame(
        _entry(own_id, 1, b'{"user":{"name":"B"}}'),
        _entry(cid_a, 10**9, b'{"user":{"name":"Fake"}}'),
    )
    await room.handle(sender=b, state=st_b, message=mixed, persist=_noop_persist)

    assert peer.sent  # something WAS relayed
    relayed = P.extract_awareness_update(peer.sent[-1])
    assert P.read_awareness_client_ids(relayed) == [own_id]  # foreign entry stripped
    assert room.awareness.states[cid_a]["user"]["name"] == "A"
    assert room.awareness.states[own_id]["user"]["name"] == "B"
    assert st_b.awareness_ids == {own_id}


@pytest.mark.anyio
async def test_binding_outlives_socket_blocks_inversion_allows_same_user_reclaim():
    # Attack #2: B cannot claim a blipped peer's id (room-lifetime USER
    # binding); the victim's same-user reconnect re-claims it freely (R2-2).
    room = Room((uuid.uuid4(), "draft"))
    a1, b, peer = FakeClient(), FakeClient(), FakeClient()
    st_a1 = ClientState(user_id=1, can_write=True)
    st_b = ClientState(user_id=2, can_write=True)
    room.clients[a1] = st_a1
    room.clients[b] = st_b
    room.clients[peer] = ClientState(user_id=3, can_write=True)
    frame, cid = _awareness_frame({"user": {"name": "A"}})
    await room.handle(sender=a1, state=st_a1, message=frame, persist=_noop_persist)

    room.clients.pop(a1)  # A's socket blips
    await room.remove_client_presence(st_a1)
    peer.sent.clear()

    steal = _crafted_frame(_entry(cid, 10**9, b'{"user":{"name":"NotA"}}'))
    await room.handle(sender=b, state=st_b, message=steal, persist=_noop_persist)
    assert peer.sent == []  # filtered: the binding outlives the socket
    assert cid not in st_b.awareness_ids

    a2 = FakeClient()  # same user reconnects (network blip → same doc.clientID)
    st_a2 = ClientState(user_id=1, can_write=True)
    room.clients[a2] = st_a2
    renew = _crafted_frame(_entry(cid, 10, b'{"user":{"name":"A"}}'))
    await room.handle(sender=a2, state=st_a2, message=renew, persist=_noop_persist)
    assert peer.sent and P.is_awareness(peer.sent[-1])  # relayed
    assert cid in st_a2.awareness_ids  # re-attributed to the reconnect
    assert room.awareness.states[cid]["user"]["name"] == "A"


@pytest.mark.anyio
async def test_first_announce_binds_and_logs(caplog):
    room = Room((uuid.uuid4(), "draft"))
    a, b = FakeClient(), FakeClient()
    st = ClientState(user_id=7, can_write=True)
    room.clients[a] = st
    room.clients[b] = ClientState(user_id=1, can_write=True)
    frame, cid = _awareness_frame({"user": {"name": "New"}})
    with caplog.at_level(logging.INFO, logger=ROOM_LOGGER):
        await room.handle(sender=a, state=st, message=frame, persist=_noop_persist)
    assert room._awareness_bindings == {cid: 7}
    assert b.sent  # relayed
    assert "collab.awareness_bind" in [r.getMessage() for r in caplog.records]


@pytest.mark.anyio
async def test_all_own_frame_relays_byte_identical():
    # The fast path: zero re-encode — the relayed frame IS the inbound frame.
    room = Room((uuid.uuid4(), "draft"))
    a, b = FakeClient(), FakeClient()
    st = ClientState(user_id=1, can_write=True)
    room.clients[a] = st
    room.clients[b] = ClientState(user_id=2, can_write=True)
    frame, _ = _awareness_frame({"user": {"name": "Own"}})
    await room.handle(sender=a, state=st, message=frame, persist=_noop_persist)
    assert b.sent == [frame]


@pytest.mark.anyio
async def test_empty_awareness_frame_passthrough():
    # count=0: trivially all-allowed → relayed unchanged (behavior-preserving
    # vs pre-D2), no bindings created.
    room = Room((uuid.uuid4(), "draft"))
    a, b = FakeClient(), FakeClient()
    st = ClientState(user_id=1, can_write=True)
    room.clients[a] = st
    room.clients[b] = ClientState(user_id=2, can_write=True)
    empty = _crafted_frame()  # payload = b"\x00"
    await room.handle(sender=a, state=st, message=empty, persist=_noop_persist)
    assert b.sent == [empty]
    assert room._awareness_bindings == {}


@pytest.mark.anyio
async def test_own_null_removal_still_relays_and_clears():
    # Legit self-removal (provider destroy/unload path) must keep working: an
    # own-bound null entry is allowed, relayed, and clears the server state.
    room = Room((uuid.uuid4(), "draft"))
    a, b = FakeClient(), FakeClient()
    st = ClientState(user_id=1, can_write=True)
    room.clients[a] = st
    room.clients[b] = ClientState(user_id=2, can_write=True)
    frame, cid = _awareness_frame({"user": {"name": "Leaving"}})
    await room.handle(sender=a, state=st, message=frame, persist=_noop_persist)
    b.sent.clear()
    bye = _crafted_frame(_entry(cid, 5, b"null"))
    await room.handle(sender=a, state=st, message=bye, persist=_noop_persist)
    assert b.sent and P.is_awareness(b.sent[-1])  # removal relayed
    assert cid not in room.awareness.states


@pytest.mark.anyio
async def test_failed_apply_does_not_reserve_bindings():
    # Codex High (a): a span-valid frame whose apply FAILS (invalid JSON state)
    # must not bind — else an attacker could stealth-pre-claim an id (no visible
    # caret, since nothing applied) and the real user's later announce would be
    # filtered as foreign for the room's life.
    room = Room((uuid.uuid4(), "draft"))
    a, b = FakeClient(), FakeClient()
    st_a = ClientState(user_id=1, can_write=True)
    st_b = ClientState(user_id=2, can_write=True)
    room.clients[a] = st_a
    room.clients[b] = st_b
    x = 424242
    junk = _crafted_frame(_entry(x, 10**9, b"{not json"))  # span-valid, apply fails
    await room.handle(sender=b, state=st_b, message=junk, persist=_noop_persist)
    assert x not in room._awareness_bindings
    assert a.sent == []  # dropped, not relayed
    # The real user announces X afterwards: bound to THEM, relayed.
    announce = _crafted_frame(_entry(x, 1, b'{"user":{"name":"RealA"}}'))
    await room.handle(sender=a, state=st_a, message=announce, persist=_noop_persist)
    assert room._awareness_bindings[x] == 1
    assert b.sent and P.is_awareness(b.sent[-1])


@pytest.mark.anyio
async def test_clock_ignored_entry_does_not_reserve_binding():
    # Codex High (b): a valid entry pycrdt IGNORES on apply (clock 0 is never
    # accepted: 0 < 0 is false) must not bind either — bind-after-accept, not
    # bind-on-parse.
    room = Room((uuid.uuid4(), "draft"))
    a, b = FakeClient(), FakeClient()
    st_a = ClientState(user_id=1, can_write=True)
    st_b = ClientState(user_id=2, can_write=True)
    room.clients[a] = st_a
    room.clients[b] = st_b
    x = 424243
    stale = _crafted_frame(_entry(x, 0, b'{"user":{"name":"Ghost"}}'))
    await room.handle(sender=b, state=st_b, message=stale, persist=_noop_persist)
    assert x not in room._awareness_bindings  # ignored → not bound
    assert x not in room.awareness.states
    # The real user's announce still binds to them.
    announce = _crafted_frame(_entry(x, 1, b'{"user":{"name":"RealA"}}'))
    await room.handle(sender=a, state=st_a, message=announce, persist=_noop_persist)
    assert room._awareness_bindings[x] == 1
    assert x in room.awareness.states


@pytest.mark.anyio
async def test_unbound_null_preclaim_is_not_attributed_to_sender():
    # A null entry for an UNBOUND id is allowed through (harmless removal, never
    # enters states) but neither binds nor attributes — else the sender's later
    # disconnect would scrub the id's REAL owner (who binds it afterwards) for
    # everyone: the 3b-5b griefing vector resurrected through the new code.
    room = Room((uuid.uuid4(), "draft"))
    a, b = FakeClient(), FakeClient()
    st_a = ClientState(user_id=1, can_write=True)
    st_b = ClientState(user_id=2, can_write=True)
    room.clients[a] = st_a
    room.clients[b] = st_b
    x = 424244
    await room.handle(
        sender=b, state=st_b,
        message=_crafted_frame(_entry(x, 5, b"null")), persist=_noop_persist,
    )
    assert x not in st_b.awareness_ids and x not in room._awareness_bindings
    # Real owner announces afterwards (clock above the stale null's meta clock)…
    await room.handle(
        sender=a, state=st_a,
        message=_crafted_frame(_entry(x, 6, b'{"user":{"name":"RealA"}}')),
        persist=_noop_persist,
    )
    assert room._awareness_bindings[x] == 1
    assert x in room.awareness.states
    # …and B's departure cannot scrub it.
    await room.remove_client_presence(st_b)
    assert x in room.awareness.states


@pytest.mark.anyio
async def test_binding_quota_is_per_user_not_per_room(caplog):
    # Finding 2: the quota must confine a binding-spammer to exhausting only
    # its OWN presence — another user's first-announce still binds and relays.
    from call_manager.proposal_assist.collab.room import _MAX_AWARENESS_BINDINGS_PER_USER

    room = Room((uuid.uuid4(), "draft"))
    b, c, peer = FakeClient(), FakeClient(), FakeClient()
    st_b = ClientState(user_id=2, can_write=True)
    st_c = ClientState(user_id=3, can_write=True)
    room.clients[b] = st_b
    room.clients[c] = st_c
    room.clients[peer] = ClientState(user_id=1, can_write=True)

    fill = _crafted_frame(
        *[
            _entry(1000 + i, 1, b'{"user":{"name":"B"}}')
            for i in range(_MAX_AWARENESS_BINDINGS_PER_USER)
        ]
    )
    await room.handle(sender=b, state=st_b, message=fill, persist=_noop_persist)
    assert len(st_b.awareness_ids) == _MAX_AWARENESS_BINDINGS_PER_USER
    peer.sent.clear()

    with caplog.at_level(logging.INFO, logger=ROOM_LOGGER):
        one_more = _crafted_frame(_entry(9999, 1, b'{"user":{"name":"B"}}'))
        await room.handle(sender=b, state=st_b, message=one_more, persist=_noop_persist)
    assert peer.sent == []  # nothing left after dropping the new id → not relayed
    assert 9999 not in room._awareness_bindings
    capped = [
        r for r in caplog.records if r.getMessage() == "collab.awareness_bindings_capped"
    ]
    assert capped and capped[0].levelno == logging.WARNING

    # User C is unaffected: a fresh first-announce still binds and relays.
    frame_c, cid_c = _awareness_frame({"user": {"name": "C"}})
    await room.handle(sender=c, state=st_c, message=frame_c, persist=_noop_persist)
    assert peer.sent and cid_c in st_c.awareness_ids
    assert room._awareness_bindings[cid_c] == 3


@pytest.mark.anyio
async def test_renewal_of_bound_ids_is_not_quota_checked():
    # A user AT the cap can still renew already-bound ids: only NEW bindings
    # count against the quota (heartbeats/caret moves must never be dropped).
    from call_manager.proposal_assist.collab.room import _MAX_AWARENESS_BINDINGS_PER_USER

    room = Room((uuid.uuid4(), "draft"))
    b, peer = FakeClient(), FakeClient()
    st_b = ClientState(user_id=2, can_write=True)
    room.clients[b] = st_b
    room.clients[peer] = ClientState(user_id=1, can_write=True)
    fill = _crafted_frame(
        *[
            _entry(1000 + i, 1, b'{"user":{"name":"B"}}')
            for i in range(_MAX_AWARENESS_BINDINGS_PER_USER)
        ]
    )
    await room.handle(sender=b, state=st_b, message=fill, persist=_noop_persist)
    peer.sent.clear()
    renew = _crafted_frame(_entry(1000, 2, b'{"user":{"name":"B2"}}'))
    await room.handle(sender=b, state=st_b, message=renew, persist=_noop_persist)
    assert peer.sent  # renewal relayed
    assert room.awareness.states[1000]["user"]["name"] == "B2"


@pytest.mark.anyio
async def test_quota_drop_preserves_renewals_and_foreign_logging(caplog):
    # Codex Medium: at the cap, a mixed frame [bound renewal + over-cap new id +
    # foreign spoof] must NOT be whole-dropped — the renewal still relays, only
    # the new id is dropped, and the foreign entry STILL fires the WARNING
    # spoof signature (foreign logging precedes the quota).
    from call_manager.proposal_assist.collab.room import _MAX_AWARENESS_BINDINGS_PER_USER

    room = Room((uuid.uuid4(), "draft"))
    victim, b, peer = FakeClient(), FakeClient(), FakeClient()
    st_v = ClientState(user_id=1, can_write=True)
    st_b = ClientState(user_id=2, can_write=True)
    room.clients[victim] = st_v
    room.clients[b] = st_b
    room.clients[peer] = ClientState(user_id=3, can_write=True)
    v_frame, cid_v = _awareness_frame({"user": {"name": "Victim"}})
    await room.handle(sender=victim, state=st_v, message=v_frame, persist=_noop_persist)
    fill = _crafted_frame(
        *[
            _entry(1000 + i, 1, b'{"user":{"name":"B"}}')
            for i in range(_MAX_AWARENESS_BINDINGS_PER_USER)
        ]
    )
    await room.handle(sender=b, state=st_b, message=fill, persist=_noop_persist)
    peer.sent.clear()

    mixed = _crafted_frame(
        _entry(1000, 2, b'{"user":{"name":"B2"}}'),  # bound renewal
        _entry(9999, 1, b'{"user":{"name":"B"}}'),  # over-cap new id
        _entry(cid_v, 10**9, b'{"user":{"name":"Fake"}}'),  # foreign spoof
    )
    with caplog.at_level(logging.INFO, logger=ROOM_LOGGER):
        await room.handle(sender=b, state=st_b, message=mixed, persist=_noop_persist)

    relayed = P.extract_awareness_update(peer.sent[-1])
    assert P.read_awareness_client_ids(relayed) == [1000]  # renewal survives
    assert room.awareness.states[1000]["user"]["name"] == "B2"
    assert 9999 not in room._awareness_bindings
    assert room.awareness.states[cid_v]["user"]["name"] == "Victim"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "collab.awareness_bindings_capped" in warnings
    assert "collab.awareness_foreign_filtered" in warnings  # not suppressed
