"""Collab retirement (B3): both rooms evicted, clients closed 1008, no checkpoint,
room.ops drained; acquire AND operate reject a retired workspace, including the
straddle where retirement lands during operate's out-of-lock await load().

Also covers the write path room.ops does NOT reach: Room.handle persists inside
room.lock, so retirement flips `evicted` under that same lock.
"""

import asyncio
import uuid

import pytest
from pycrdt import Doc, Text

from grant_lab.proposal_assist.collab import protocol as P
from grant_lab.proposal_assist.collab import room as room_module
from grant_lab.proposal_assist.collab.room import ClientState, RoomManager
from grant_lab.proposal_assist.drafting.contract import WorkspaceRetired
from grant_lab.proposal_assist.lifecycle import LIFECYCLE


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


async def _empty_load():
    return []


def _write_frame(text: str = "hello") -> bytes:
    """A real sync-write frame carrying a non-empty update. Built the way
    test_collab_ws_route.py does — Room.handle drops anything P.extract_update
    cannot parse, so a hand-rolled byte string would make tests pass vacuously."""
    d = Doc()
    d["body"] = Text()
    cap: list[bytes] = []
    sub = d.observe(lambda e: cap.append(e.update))
    d["body"] += text
    d.unobserve(sub)
    return P.create_update_message(cap[-1])


@pytest.mark.anyio
async def test_retire_evicts_both_rooms_and_closes_clients_1008():
    mgr = RoomManager()
    ws = uuid.uuid4()
    draft, spec = (ws, "draft"), (ws, "spec")
    cd, cs = FakeClient(), FakeClient()
    await mgr.acquire(draft, load=_empty_load, client=cd,
                      state=ClientState(user_id=1, can_write=True))
    await mgr.acquire(spec, load=_empty_load, client=cs,
                      state=ClientState(user_id=1, can_write=True))

    n = await mgr.retire_workspace(ws, 1)

    assert n == 2
    assert cd.closed == 1008 and cs.closed == 1008   # policy violation, NOT 1011
    assert mgr.get(draft) is None and mgr.get(spec) is None


@pytest.mark.anyio
async def test_both_artifact_rooms_retire_concurrently(monkeypatch):
    """Budget-critical, not cosmetic: serialising the two rooms doubled the collab
    hook's worst case to 2 x (lock wait + ops drain) = 6.0s, past the retirement
    barrier the sync delete route blocks on (see test_retirement_coordinator's
    budget invariant).

    The discriminator is `completed`, not `seen`: under SERIAL teardown the first
    room's _set_evicted_under_lock is cancelled by its own _RETIRE_LOCK_WAIT_SECONDS
    bound before it can finish, so only the second room ever completes."""
    mgr = RoomManager()
    ws = uuid.uuid4()
    for artifact in ("draft", "spec"):
        await mgr.acquire((ws, artifact), load=_empty_load, client=FakeClient(),
                          state=ClientState(user_id=1, can_write=True))

    original = room_module._set_evicted_under_lock
    seen: list = []
    completed: list = []
    both_in = asyncio.Event()

    async def _instrumented(room):
        seen.append(room.key)
        if len(seen) == 2:
            both_in.set()
        await both_in.wait()          # only releases if the other room is also here
        await original(room)
        completed.append(room.key)

    monkeypatch.setattr(room_module, "_set_evicted_under_lock", _instrumented)

    n = await asyncio.wait_for(mgr.retire_workspace(ws, 1), timeout=10)

    assert n == 2                      # both sockets still closed
    assert len(completed) == 2         # serial teardown would leave 1
    assert mgr.get((ws, "draft")) is None and mgr.get((ws, "spec")) is None


@pytest.mark.anyio
async def test_one_rooms_failure_does_not_abandon_the_other(monkeypatch):
    """Concurrency must not weaken the guarantee: if the draft room's teardown
    blows up, the spec room is still evicted and its editor still closed, and
    retire_workspace still returns (the coordinator's own isolation is the last
    resort, not the first)."""
    mgr = RoomManager()
    ws = uuid.uuid4()
    clients = {}
    for artifact in ("draft", "spec"):
        clients[artifact] = FakeClient()
        await mgr.acquire((ws, artifact), load=_empty_load, client=clients[artifact],
                          state=ClientState(user_id=1, can_write=True))

    original = room_module._set_evicted_under_lock

    async def _boom_on_draft(room):
        if room.key[1] == "draft":
            raise RuntimeError("teardown exploded")
        await original(room)

    monkeypatch.setattr(room_module, "_set_evicted_under_lock", _boom_on_draft)

    # Coordinator applies transition before invoking hooks; mirror that here.
    await LIFECYCLE.apply(ws, generation=1, retired=True)
    closed = await asyncio.wait_for(mgr.retire_workspace(ws, 1), timeout=10)

    assert closed == 1                                  # only spec's socket counted
    assert clients["spec"].closed == 1008               # ...but it WAS closed
    # Both rooms are gone either way: the pop happens before the flip.
    assert mgr.get((ws, "draft")) is None and mgr.get((ws, "spec")) is None
    with pytest.raises(WorkspaceRetired):               # authority still refuses
        await mgr.acquire((ws, "draft"), load=_empty_load, client=FakeClient(),
                          state=ClientState(user_id=1, can_write=True))


@pytest.mark.anyio
async def test_retire_does_not_run_the_deferred_checkpoint_callback():
    """drop-style teardown: retirement must NEVER checkpoint (the workspace is
    soft-deleted; a checkpoint would write to a dead workspace).

    Asserts on the ROOM's own on-evict callback rather than by patching
    checkpoint.checkpoint_artifact. The earlier version of this test did the
    latter, which could not fail: every real caller (permissions.py, handover.py,
    kernel.py) does `from ...checkpoint import checkpoint_artifact`, binding the
    name at import, so setattr on the checkpoint MODULE never intercepts them —
    the counter could not increment no matter what retirement did.

    `_pending_on_evict` is the actual mechanism a checkpoint would reach
    retirement through: release()/operate()-exit stash the callback there and run
    it on eviction. Retirement pops the room without ever running it, and this
    test fails the moment that changes.
    """
    mgr = RoomManager()
    ws = uuid.uuid4()
    key = (ws, "draft")
    room = await mgr.acquire(key, load=_empty_load, client=FakeClient(),
                             state=ClientState(user_id=1, can_write=True))

    ran: list[str] = []

    async def _on_evict(_room):
        ran.append("checkpointed")

    room._pending_on_evict = _on_evict       # what a last-leave checkpoint installs

    await mgr.retire_workspace(ws, 1)

    assert ran == []                          # never invoked
    assert room._pending_on_evict is _on_evict   # nor consumed and silently dropped


@pytest.mark.anyio
async def test_acquire_rejects_when_retirement_lands_during_another_creators_load():
    """Covers BOTH of acquire()'s retirement re-checks, which had zero coverage:
    deleting them left all 371 collab tests green.

    One interleaving reaches both. Creator A enters the per-key lock and parks in
    its out-of-lock `await load()`. Joiner B blocks on that same key lock.
    Retirement lands. A's load returns -> A hits the AFTER-LOAD re-check and must
    refuse rather than publish a room for a deleted workspace; releasing the key
    lock lets B in, and B hits the UNDER-KEYLOCK re-check on the way to creating
    its own room.
    """
    mgr = RoomManager()
    ws = uuid.uuid4()
    key = (ws, "draft")
    load_entered = asyncio.Event()
    allow_load_finish = asyncio.Event()

    async def _slow_load():
        load_entered.set()
        await allow_load_finish.wait()
        return []

    async def _creator():
        return await mgr.acquire(key, load=_slow_load, client=FakeClient(),
                                 state=ClientState(user_id=1, can_write=True))

    async def _joiner():
        return await mgr.acquire(key, load=_empty_load, client=FakeClient(),
                                 state=ClientState(user_id=2, can_write=True))

    a = asyncio.ensure_future(_creator())
    await asyncio.wait_for(load_entered.wait(), timeout=5)   # A holds the key lock
    b = asyncio.ensure_future(_joiner())                     # B queues behind it
    await asyncio.sleep(0.05)                                # let B reach the key lock
    assert not b.done()

    # Coordinator applies transition before invoking hooks; mirror that here.
    await LIFECYCLE.apply(ws, generation=1, retired=True)
    await mgr.retire_workspace(ws, 1)                           # retirement lands mid-load
    allow_load_finish.set()

    with pytest.raises(WorkspaceRetired):
        await asyncio.wait_for(a, timeout=5)                 # after-load re-check
    with pytest.raises(WorkspaceRetired):
        await asyncio.wait_for(b, timeout=5)                 # under-keylock re-check
    assert mgr.get(key) is None                              # never published


@pytest.mark.anyio
async def test_retire_flips_can_write_false_before_closing():
    mgr = RoomManager()
    ws = uuid.uuid4()
    st = ClientState(user_id=1, can_write=True)
    await mgr.acquire((ws, "draft"), load=_empty_load, client=FakeClient(), state=st)
    await mgr.retire_workspace(ws, 1)
    assert st.can_write is False


@pytest.mark.anyio
async def test_acquire_rejects_a_retired_workspace():
    mgr = RoomManager()
    ws = uuid.uuid4()
    # Coordinator applies transition before invoking hooks; mirror that here.
    await LIFECYCLE.apply(ws, generation=1, retired=True)
    await mgr.retire_workspace(ws, 1)
    with pytest.raises(WorkspaceRetired):
        await mgr.acquire((ws, "draft"), load=_empty_load, client=FakeClient(),
                          state=ClientState(user_id=1, can_write=True))


@pytest.mark.anyio
async def test_operate_rejects_a_retired_workspace():
    mgr = RoomManager()
    ws = uuid.uuid4()
    # Coordinator applies transition before invoking hooks; mirror that here.
    await LIFECYCLE.apply(ws, generation=1, retired=True)
    await mgr.retire_workspace(ws, 1)
    with pytest.raises(WorkspaceRetired):
        async with mgr.operate((ws, "draft"), load=_empty_load):
            pass


@pytest.mark.anyio
async def test_operate_rejects_when_retirement_lands_during_load():
    """The straddle kernel_accept.py:71 rides: retirement commits while operate is
    inside its OUT-OF-LOCK await load(); the room must not be published."""
    mgr = RoomManager()
    ws = uuid.uuid4()
    key = (ws, "draft")
    load_entered = asyncio.Event()
    allow_load_finish = asyncio.Event()

    async def _slow_load():
        load_entered.set()
        await allow_load_finish.wait()       # park inside operate's cold-start load()
        return []

    async def _op():
        async with mgr.operate(key, load=_slow_load) as room:
            return room

    op_task = asyncio.ensure_future(_op())
    await asyncio.wait_for(load_entered.wait(), timeout=5)   # operate is inside load()
    # Coordinator applies transition before invoking hooks; mirror that here.
    await LIFECYCLE.apply(ws, generation=1, retired=True)
    await mgr.retire_workspace(ws, 1)                           # retirement lands mid-load
    allow_load_finish.set()                                  # let load() return

    with pytest.raises(WorkspaceRetired):
        await op_task
    assert mgr.get(key) is None                              # never published


@pytest.mark.anyio
async def test_retire_drains_in_flight_ops():
    """retire_workspace waits for an in-flight operate() to leave before returning
    (room.ops back to 0). The op itself is refused durably by B4's mutex; the drain
    only bounds 'retirement is done'."""
    mgr = RoomManager()
    ws = uuid.uuid4()
    key = (ws, "draft")
    inside = asyncio.Event()
    release = asyncio.Event()

    async def _op():
        async with mgr.operate(key, load=_empty_load):
            inside.set()
            await release.wait()             # hold room.ops == 1

    op_task = asyncio.ensure_future(_op())
    await asyncio.wait_for(inside.wait(), timeout=5)

    async def _retire_then_flag():
        await mgr.retire_workspace(ws, 1)
        return "retired"

    retire_task = asyncio.ensure_future(_retire_then_flag())
    await asyncio.sleep(0.05)
    assert not retire_task.done()            # still draining (ops == 1)
    release.set()                            # op leaves -> ops == 0
    assert await asyncio.wait_for(retire_task, timeout=5) == "retired"
    await op_task


@pytest.mark.anyio
async def test_retire_waits_for_an_in_flight_client_persist_and_stops_later_ones():
    """The `ops` drain does NOT cover client writes: Room.handle persists inside
    room.lock, and room.ops is only bumped by operate(). Retirement flips `evicted`
    under room.lock, so (a) it cannot return while a persist is in flight, and
    (b) every LATER frame drops before apply/persist."""
    mgr = RoomManager()
    ws = uuid.uuid4()
    key = (ws, "draft")

    persisted: list[bytes] = []
    in_persist = asyncio.Event()
    release_persist = asyncio.Event()

    async def _persist(actor_id, update):
        in_persist.set()
        await release_persist.wait()
        persisted.append(update)

    room = await mgr.acquire(
        key, load=_empty_load, client=FakeClient(), state=ClientState(user_id=1, can_write=True)
    )
    # Park a write inside room.lock, mid-persist.
    write_task = asyncio.ensure_future(
        room.handle(sender=FakeClient(), state=ClientState(user_id=1, can_write=True),
                    message=_write_frame("a"), persist=_persist)
    )
    await asyncio.wait_for(in_persist.wait(), timeout=5)

    retire_task = asyncio.ensure_future(mgr.retire_workspace(ws, 1))
    await asyncio.sleep(0.05)
    assert not retire_task.done()            # blocked on room.lock — the whole point

    release_persist.set()
    await asyncio.wait_for(write_task, timeout=5)
    await asyncio.wait_for(retire_task, timeout=5)
    assert len(persisted) == 1               # the in-flight one completed

    # A LATER frame on the same room object persists nothing: evicted is set.
    await room.handle(
        sender=FakeClient(), state=ClientState(user_id=1, can_write=True),
        message=_write_frame("b"), persist=_persist,
    )
    assert len(persisted) == 1               # unchanged — dropped before persist
