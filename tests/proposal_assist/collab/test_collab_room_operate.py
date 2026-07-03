# tests/proposal_assist/collab/test_collab_room_operate.py
"""Pure-asyncio unit tests for RoomManager.operate / release ops-refcount (Codex H1).

No DB, no Postgres — deterministic and fast.

asyncio_mode = "auto" is configured in pyproject.toml, so bare `async def test_*`
functions run under pytest-asyncio automatically (no decorator needed).
"""

import asyncio
import uuid

from call_manager.proposal_assist.collab.room import ClientState, RoomManager
from tests.proposal_assist.collab.test_collab_room import FakeClient


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
    checks `room.clients`, which is a dict keyed by client; send_bytes is never called.
    """
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")

    # Enter an operate context so ops == 1
    cm = mgr.operate(key, load=_load)
    room = await cm.__aenter__()
    assert room.ops == 1

    # Register a client
    c = object()
    await mgr.acquire(key, load=_load, client=c, state=ClientState(user_id=1, can_write=True))
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


async def test_operate_else_branch_bumps_ops_atomically():
    """Force operate()'s cold-race ELSE branch — a second op that saw room=None at
    the first check, then found the room already created after waiting on the keylock
    — and assert ops is bumped ATOMICALLY with the re-fetch (Codex H1 slow-path fix).

    Interleaving (deterministic via a gated load):
      - op1 enters, cold-creates, and BLOCKS inside load() while holding the keylock
        (the room is NOT yet registered).
      - op2 enters: first check sees room=None (op1 hasn't registered), so it goes to
        the keylock and BLOCKS (op1 holds it).
      - op1's load is released → op1 registers the room (ops=1) and releases the keylock.
      - op2 acquires the keylock, re-fetches → FINDS the room → takes the else branch
        and bumps ops to 2 in the SAME lock section as the re-fetch.

    Before the fix, op2 re-fetched and bumped ops in two separate lock sections, so a
    concurrent op1 exit could pop the room in the gap and hand op2 a zombie. With the
    fix, both ops share one live room and it is evicted exactly once after both exit.
    """
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")

    entered = asyncio.Event()
    gate = asyncio.Event()

    async def _blocking_load() -> list[bytes]:
        entered.set()          # op1 is inside cold-create load(), keylock held, unregistered
        await gate.wait()      # block until released
        return []

    cm1 = mgr.operate(key, load=_blocking_load)
    t1 = asyncio.create_task(cm1.__aenter__())
    await entered.wait()       # op1 now blocked in load() — room not yet registered

    cm2 = mgr.operate(key, load=_load)
    t2 = asyncio.create_task(cm2.__aenter__())
    await asyncio.sleep(0.05)  # let op2 reach the keylock await (op1 still holds it)

    gate.set()                 # release op1: it registers the room and frees the keylock
    room1 = await t1
    room2 = await t2

    assert room1 is room2, "the else-branch op must share op1's room, not a zombie"
    assert room1.ops == 2, f"else-branch must bump ops atomically to 2, got {room1.ops}"
    assert mgr.has_room(key) and mgr.get(key) is room1

    await cm1.__aexit__(None, None, None)
    assert room1.ops == 1 and mgr.has_room(key)
    await cm2.__aexit__(None, None, None)
    assert room1.ops == 0 and not mgr.has_room(key)


async def test_last_client_release_during_op_checkpoints_on_op_exit():
    # A room with ONE client and an in-flight server op (ops=1). The last client
    # is released with an on_evict checkpoint; because ops>0 the room is NOT
    # evicted yet and on_evict must NOT run. When the op exits (ops->0, no
    # clients), the room is evicted AND the stashed on_evict fires exactly once.
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    fired = {"n": 0}

    async def _on_evict(_room):
        fired["n"] += 1

    async def _load():
        return []

    client = FakeClient()
    async with mgr.operate(key, load=_load) as room:   # ops=1
        room.clients[client] = ClientState(user_id=1, can_write=True)
        await mgr.release(key, client=client, on_evict=_on_evict)
        assert mgr.get(key) is room     # not evicted (ops>0)
        assert fired["n"] == 0          # on_evict deferred
    # operate() exited: ops->0, no clients → evict + fire stashed on_evict
    assert mgr.get(key) is None
    assert fired["n"] == 1
