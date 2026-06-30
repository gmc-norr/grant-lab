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
