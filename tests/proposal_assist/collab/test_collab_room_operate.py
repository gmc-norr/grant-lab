# tests/proposal_assist/collab/test_collab_room_operate.py
"""Pure-asyncio unit tests for RoomManager.operate / release ops-refcount (Codex H1).

No DB, no Postgres — deterministic and fast.

asyncio_mode = "auto" is configured in pyproject.toml, so bare `async def test_*`
functions run under pytest-asyncio automatically (no decorator needed).
"""

import uuid

from call_manager.proposal_assist.collab.room import RoomManager


async def _load() -> list[bytes]:
    """No-op loader: cold-start hydration returns an empty update list."""
    return []


async def test_overlapping_ops_share_one_room_and_deferred_evict():
    """Two overlapping operate() contexts on the same key must:

    - yield the SAME Room object
    - leave room.ops == 2 while both are active
    - NOT evict the room when the first exits (ops drops to 1, room still registered)
    - evict the room only after the second exits (ops drops to 0, no clients)

    Uses explicit __aenter__ / __aexit__ calls so the interleaving is
    deterministic and not subject to async-with nesting order.
    """
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")

    # Enter both operate contexts
    cm1 = mgr.operate(key, load=_load)
    cm2 = mgr.operate(key, load=_load)

    room1 = await cm1.__aenter__()
    room2 = await cm2.__aenter__()

    # Both must be the same Room object
    assert room1 is room2, "both operate calls must yield the same Room"
    assert room1.ops == 2, f"expected ops=2 while both contexts are active, got {room1.ops}"

    # Exit the first context — room must survive (ops == 1, still in flight)
    await cm1.__aexit__(None, None, None)
    assert room1.ops == 1, f"expected ops=1 after first exit, got {room1.ops}"
    assert mgr.has_room(key), "room must NOT be evicted while second op is still in flight"

    # Exit the second context — now both are done, no clients → evict
    await cm2.__aexit__(None, None, None)
    assert room1.ops == 0, f"expected ops=0 after both exits, got {room1.ops}"
    assert not mgr.has_room(key), "room must be evicted once all ops are done and no clients remain"


async def test_release_during_op_does_not_evict():
    """A client leaving while a server op is in flight must NOT evict the room.

    Setup: one operate() context (ops == 1) + one acquired client.
    When release() is called for that client (clients becomes empty), the room
    must SURVIVE because ops is still 1.  Only after the operate() context exits
    (ops drops to 0) should the room be evicted.

    A client here is a bare object() — RoomManager.acquire/release only ever
    checks `room.clients`, which is a plain set; send_bytes is never called.
    """
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")

    # Enter an operate context so ops == 1
    cm = mgr.operate(key, load=_load)
    room = await cm.__aenter__()
    assert room.ops == 1

    # Register a client
    c = object()
    await mgr.acquire(key, load=_load, client=c)
    assert c in room.clients

    # Release the client — clients is now empty, but ops == 1 → room must survive
    await mgr.release(key, client=c)
    assert c not in room.clients
    assert mgr.has_room(key), (
        "room must NOT be evicted while a server op is in flight, "
        "even if there are no connected clients"
    )

    # Exit the operate context — ops drops to 0, no clients → now evict
    await cm.__aexit__(None, None, None)
    assert not mgr.has_room(key), (
        "room must be evicted once the last server op exits and no clients remain"
    )
