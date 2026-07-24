"""Per-generation live-client bookkeeping (Slice A). Pure in-memory logic."""

import uuid

from call_manager.proposal_assist.drafting.live_clients import LiveClientRegistry


def test_register_returns_distinct_generations_for_one_workspace():
    reg = LiveClientRegistry()
    ws = uuid.uuid4()
    a = reg.register(ws, client="client-a")
    b = reg.register(ws, client="client-b")
    assert a != b
    assert {r.launch_id for r in reg.find_by_workspace(ws)} == {a, b}
    assert reg.unconfirmed_count() == 2


def test_clearing_is_by_identity_not_by_workspace():
    # Orphan/replacement: a stale teardown must NOT clear the new client.
    reg = LiveClientRegistry()
    ws = uuid.uuid4()
    orphan = reg.register(ws, client="orphan")
    reg.mark_retiring(orphan)
    fresh = reg.register(ws, client="fresh")

    reg.confirm_dead(orphan)
    assert reg.get(orphan) is None
    assert reg.get(fresh) is not None
    assert reg.get(fresh).state == "live"


def test_retiring_count_tracks_unconfirmed_orphans_only():
    reg = LiveClientRegistry()
    live = reg.register(uuid.uuid4(), client="live")
    orphan = reg.register(uuid.uuid4(), client="orphan")
    assert reg.retiring_count() == 0

    reg.mark_retiring(orphan)
    assert reg.retiring_count() == 1
    assert reg.unconfirmed_count() == 2

    reg.confirm_dead(orphan)
    assert reg.retiring_count() == 0
    assert reg.unconfirmed_count() == 1
    assert reg.get(live) is not None


def test_mark_retiring_is_idempotent_and_tolerates_unknown_ids():
    reg = LiveClientRegistry()
    lid = reg.register(uuid.uuid4(), client="c")
    assert reg.mark_retiring(lid).state == "retiring"
    assert reg.mark_retiring(lid).state == "retiring"
    assert reg.mark_retiring(uuid.uuid4()) is None
    reg.confirm_dead(uuid.uuid4())  # no raise
