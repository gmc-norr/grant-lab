import pytest
from pycrdt import Doc, Text

from call_manager.proposal_assist.collab import protocol as P


def _make_update_message(text: str) -> bytes:
    """A real SYNC_UPDATE wire frame produced by editing a Doc."""
    src = Doc()
    src["body"] = Text()
    sub = []
    src.observe(lambda e: sub.append(e.update))
    src["body"] += text
    # create_update_message wraps the raw update in a [SYNC][UPDATE][len] frame
    return P.create_update_message(sub[-1])


def test_classifies_step1_message():
    msg = P.create_sync_message(Doc())  # SYNC_STEP1
    assert P.is_sync_step1(msg) is True
    assert P.is_sync_write(msg) is False
    assert P.is_awareness(msg) is False


def test_classifies_update_message_as_write():
    msg = _make_update_message("hello")
    assert P.is_sync_write(msg) is True
    assert P.is_sync_step1(msg) is False


def test_extract_update_round_trips_into_a_fresh_doc():
    msg = _make_update_message("hello world")
    update = P.extract_update(msg)
    dst = Doc()
    dst["body"] = Text()
    dst.apply_update(update)
    assert str(dst["body"]) == "hello world"


def test_step1_drives_convergence_via_handle_sync_message():
    # A populated server doc replies to a fresh client's STEP1 with a STEP2 the
    # client applies to converge. Proves we understand the codec end to end.
    server = Doc()
    server["body"] = Text()
    server["body"] += "server content"
    client = Doc()
    client["body"] = Text()
    step1_from_client = P.create_sync_message(client)
    reply = P.handle_sync_message(step1_from_client[1:], server)  # -> SYNC_STEP2
    assert reply is not None
    client.apply_update(P.extract_update(reply))
    assert str(client["body"]) == "server content"


def test_empty_message_is_not_a_write():
    assert P.is_sync_write(b"") is False
    assert P.is_sync_step1(b"") is False


from pycrdt import Doc as _Doc  # noqa: E402


def _awareness_payload(user):
    d = _Doc()
    a = P.Awareness(d)
    a.set_local_state(user)
    return a.encode_awareness_update([a.client_id]), a.client_id


def test_extract_awareness_update_roundtrips_create_awareness_message():
    payload, _ = _awareness_payload({"user": {"name": "Alice"}})
    frame = P.create_awareness_message(payload)
    assert P.is_awareness(frame)
    assert P.extract_awareness_update(frame) == payload


def test_read_awareness_client_ids_single():
    payload, cid = _awareness_payload({"user": {"name": "Alice", "color": "#f00"}})
    assert P.read_awareness_client_ids(payload) == [cid]


def test_read_awareness_client_ids_multi_and_removal():
    # Merge two clients into one server awareness, encode a multi-client snapshot.
    d = _Doc()
    srv = P.Awareness(d)
    srv.set_local_state(None)
    p1, c1 = _awareness_payload({"user": {"name": "A"}})
    p2, c2 = _awareness_payload({"user": {"name": "B"}})
    srv.apply_awareness_update(p1, "x")
    srv.apply_awareness_update(p2, "x")
    snap = srv.encode_awareness_update(list(srv.states))
    assert sorted(P.read_awareness_client_ids(snap)) == sorted([c1, c2])
    # A removal frame (state=null) STILL declares the id.
    srv.remove_awareness_states([c1], "srv")
    rem = srv.encode_awareness_update([c1])
    assert P.read_awareness_client_ids(rem) == [c1]


def test_read_awareness_client_ids_rejects_overlong_varuint_promptly():
    # SECURITY REGRESSION: a long run of continuation bytes must NOT shift a
    # multi-megabit bignum (an O(n^2) event-loop stall — a whole-process DoS,
    # since awareness is ungated). The bounded parser raises after <=10 bytes,
    # so this returns in microseconds even for a ~1 MB payload (the unbounded
    # parser stalled ~66s on this input).
    import time

    payload = b"\xff" * 900_000  # < the 1 MB WS frame cap; all continuation bytes
    t0 = time.perf_counter()
    with pytest.raises(ValueError):
        P.read_awareness_client_ids(payload)
    assert time.perf_counter() - t0 < 2.0  # bounded — nowhere near the ~66s stall


def test_read_awareness_client_ids_rejects_truncated_body():
    # count=3 but no client bytes follow → truncated, raises (caller drops).
    with pytest.raises(ValueError):
        P.read_awareness_client_ids(b"\x03")


def test_read_awareness_client_ids_rejects_state_len_overshoot():
    # count=1, clientID=1, clock=0, state_len=200 but no state bytes → out of range.
    with pytest.raises(ValueError):
        P.read_awareness_client_ids(bytes([1, 1, 0, 200]))


# ---------------------------------------------------------------------------
# 3b-5d.D2: entry-span parser (read_awareness_entries)
# ---------------------------------------------------------------------------


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


def test_read_awareness_entries_spans_reconstruct_payload():
    # Re-concatenating each entry's [start:end) span behind a fresh count varuint
    # must reproduce a real multi-client snapshot byte-for-byte.
    d = _Doc()
    srv = P.Awareness(d)
    srv.set_local_state(None)
    p1, c1 = _awareness_payload({"user": {"name": "A"}})
    p2, c2 = _awareness_payload({"user": {"name": "B"}})
    srv.apply_awareness_update(p1, "x")
    srv.apply_awareness_update(p2, "x")
    snap = srv.encode_awareness_update(list(srv.states))
    entries = P.read_awareness_entries(snap)
    assert sorted(e.client_id for e in entries) == sorted([c1, c2])
    rebuilt = _vu(len(entries)) + b"".join(snap[e.start : e.end] for e in entries)
    assert rebuilt == snap


def test_read_awareness_entries_is_null_flags_removals_only():
    payload, cid = _awareness_payload({"user": {"name": "Alice"}})
    (announce,) = P.read_awareness_entries(payload)
    assert announce.client_id == cid and announce.is_null is False
    # A removal frame (state = JSON null) declares the id with is_null=True.
    d = _Doc()
    srv = P.Awareness(d)
    srv.set_local_state(None)
    srv.apply_awareness_update(payload, "x")
    srv.remove_awareness_states([cid], "srv")
    rem = srv.encode_awareness_update([cid])
    (removal,) = P.read_awareness_entries(rem)
    assert removal.client_id == cid and removal.is_null is True


def test_read_awareness_entries_preserves_dos_hardening():
    # The f535f6c adversarial cases, now against the refactored parser: the
    # refactor must not lose the bounds (capped varuints, prompt ValueError).
    import time

    t0 = time.perf_counter()
    with pytest.raises(ValueError):
        P.read_awareness_entries(b"\xff" * 900_000)  # all-continuation varuint run
    assert time.perf_counter() - t0 < 2.0  # bounded — nowhere near the ~66s stall
    with pytest.raises(ValueError):
        P.read_awareness_entries(b"\x03")  # lying count / truncated body
    with pytest.raises(ValueError):
        P.read_awareness_entries(bytes([1, 1, 0, 200]))  # state_len overshoot


def test_read_awareness_client_ids_is_a_wrapper_over_entries():
    # Hand-crafted two-entry payload; the big id spans multiple varuint groups.
    payload = _vu(2) + _entry(7, 1, b'{"x":1}') + _entry(2**40 + 3, 5, b"null")
    entries = P.read_awareness_entries(payload)
    assert P.read_awareness_client_ids(payload) == [e.client_id for e in entries]
    assert [e.client_id for e in entries] == [7, 2**40 + 3]
    assert [e.is_null for e in entries] == [False, True]
    # state_start:end is exactly the state bytes (the room pre-validates these).
    assert payload[entries[0].state_start : entries[0].end] == b'{"x":1}'
    assert payload[entries[1].state_start : entries[1].end] == b"null"


# ---------------------------------------------------------------------------
# 3b-5d.D2: filter_awareness_update
# ---------------------------------------------------------------------------


def test_filter_awareness_update_all_allowed_is_byte_identical_fast_path():
    payload = _vu(2) + _entry(7, 1, b'{"a":1}') + _entry(9, 1, b'{"b":2}')
    entries = P.read_awareness_entries(payload)
    # `is`: the fast path must not copy — zero re-encode in normal operation.
    assert P.filter_awareness_update(payload, entries, entries) is payload


def test_filter_awareness_update_none_allowed_returns_none():
    payload = _vu(1) + _entry(7, 1, b'{"a":1}')
    entries = P.read_awareness_entries(payload)
    assert P.filter_awareness_update(payload, entries, []) is None


def test_filter_awareness_update_mixed_rebuilds_kept_spans():
    # A real two-client snapshot filtered to one id must parse to exactly the
    # kept id AND apply cleanly to a fresh pycrdt Awareness.
    d = _Doc()
    srv = P.Awareness(d)
    srv.set_local_state(None)
    p1, c1 = _awareness_payload({"user": {"name": "Keep"}})
    p2, _c2 = _awareness_payload({"user": {"name": "Drop"}})
    srv.apply_awareness_update(p1, "x")
    srv.apply_awareness_update(p2, "x")
    snap = srv.encode_awareness_update(list(srv.states))
    entries = P.read_awareness_entries(snap)
    kept = [e for e in entries if e.client_id == c1]
    out = P.filter_awareness_update(snap, entries, kept)
    assert out is not None and out != snap
    assert P.read_awareness_client_ids(out) == [c1]
    fresh = P.Awareness(_Doc())
    fresh.set_local_state(None)
    fresh.apply_awareness_update(out, "x")
    assert set(fresh.states) == {c1}
    assert fresh.states[c1]["user"]["name"] == "Keep"


def test_filter_awareness_update_empty_frame_is_all_allowed():
    # count=0: vacuously all-kept → same object back (the caller decides whether
    # an empty frame is worth relaying at all).
    payload = _vu(0)
    assert P.filter_awareness_update(payload, [], []) is payload


# --- Every door into the varuint reader, at the WebSocket frame cap ----------
#
# `_MAX_FRAME_BYTES` (collab_ws.py) is 1_000_000 and the check is `>`, so a frame
# of EXACTLY that size reaches Room.handle. pycrdt's `read_var_uint` is an
# unbounded `while True` shifting a Python int, so a run of continuation bytes is
# O(n^2): measured here at 0.115 s (32 KB), 1.77 s (128 KB), 7.4 s (256 KB) and
# ~106 s at the cap — a whole-instance freeze, since this project runs one
# asyncio loop under a single-instance constraint.
#
# `f535f6c` bounded ONE function (`read_awareness_entries`) and left the reader
# reachable through three others. This table is the regression gate for all of
# them at once: adding a new decode path without routing it through
# `safe_read_message` should mean adding a row here that fails.
_ADVERSARIAL = b"\xff" * 999_998


@pytest.mark.parametrize(
    ("door", "header"),
    [
        # room.py STEP1 branch — ungated by can_write: ANY member reaches it.
        ("guard_sync_frame", b"\x00\x00"),
        # room.py SYNC write branch — writers only, still a whole-instance freeze.
        ("extract_update", b"\x00\x02"),
        # room.py awareness branch — ungated, and the door f535f6c missed: its
        # hardened entry parser never runs, because this extractor raises first.
        ("extract_awareness_update", b"\x01"),
    ],
)
def test_every_decode_door_is_bounded_at_the_frame_cap(door, header):
    import time

    frame = header + _ADVERSARIAL
    assert len(frame) <= 1_000_000, "the frame must be one the WS cap lets through"
    t0 = time.perf_counter()
    with pytest.raises(ValueError):
        getattr(P, door)(frame)
    assert time.perf_counter() - t0 < 2.0


def test_safe_read_message_matches_pycrdt_on_well_formed_frames():
    # The bound must not change what a legitimate frame decodes to. Round-trip a
    # real update frame and an empty payload through both offsets the room uses.
    msg = _make_update_message("hello world")
    assert P.extract_update(msg) == P.safe_read_message(msg, 2)
    payload = b'\x01\x2a\x01\x07{"x":1}'
    assert P.extract_awareness_update(P.create_awareness_message(payload)) == payload
    assert P.safe_read_message(b"\x00", 0) == b""  # declared length 0


def test_safe_read_message_refuses_a_length_longer_than_the_payload():
    # pycrdt's reader slices past the end and SILENTLY returns a short message.
    # A declared length the frame cannot satisfy is malformed; say so.
    with pytest.raises(ValueError):
        P.safe_read_message(b"\x05ab", 0)


def test_read_message_is_not_reachable_through_this_module():
    # The unbounded reader must have no door left. Re-importing it here — or
    # re-exporting it — re-opens the DoS for the next caller who reaches for it.
    assert not hasattr(P, "read_message")
    assert "read_message" not in P.__all__


# --- Document epoch frame (spec 2026-09-02-collab-doc-epoch-design.md §3.4) ---


@pytest.mark.parametrize("epoch", [0, 1, 127, 128, 2**32, 2**62])
def test_doc_epoch_message_round_trips_through_the_bounded_reader(epoch):
    frame = P.create_doc_epoch_message(epoch)
    assert frame[0] == P.MESSAGE_DOC_EPOCH == 4
    assert P.read_doc_epoch_message(frame) == epoch


def test_doc_epoch_frame_is_neither_sync_nor_awareness():
    frame = P.create_doc_epoch_message(3)
    assert not P.is_sync_step1(frame)
    assert not P.is_sync_write(frame)
    assert not P.is_awareness(frame)


def test_read_doc_epoch_message_refuses_other_types_and_malformed_bodies():
    with pytest.raises(ValueError):
        P.read_doc_epoch_message(bytes([0, 0]))  # a SYNC frame
    with pytest.raises(ValueError):
        P.read_doc_epoch_message(bytes([4]))  # no epoch at all
    with pytest.raises(ValueError):
        P.read_doc_epoch_message(bytes([4]) + b"\x80" * 11)  # overlong varuint
    with pytest.raises(ValueError):
        P.read_doc_epoch_message(bytes([4, 5, 9]))  # trailing bytes after the epoch
