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
