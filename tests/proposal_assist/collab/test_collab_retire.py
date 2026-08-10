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

from call_manager.proposal_assist.collab import protocol as P
from call_manager.proposal_assist.collab.room import ClientState, RoomManager
from call_manager.proposal_assist.drafting.contract import WorkspaceRetired


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

    n = await mgr.retire_workspace(ws)

    assert n == 2
    assert cd.closed == 1008 and cs.closed == 1008   # policy violation, NOT 1011
    assert mgr.get(draft) is None and mgr.get(spec) is None


@pytest.mark.anyio
async def test_retire_does_not_checkpoint(monkeypatch):
    """drop-style teardown: retirement must NEVER call the checkpoint path (the
    workspace is soft-deleted; a checkpoint would write to a dead workspace)."""
    import call_manager.proposal_assist.collab.checkpoint as cp

    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("retirement must not checkpoint")

    monkeypatch.setattr(cp, "checkpoint_artifact", _boom)
    mgr = RoomManager()
    ws = uuid.uuid4()
    await mgr.acquire((ws, "draft"), load=_empty_load, client=FakeClient(),
                      state=ClientState(user_id=1, can_write=True))
    await mgr.retire_workspace(ws)
    assert called["n"] == 0


@pytest.mark.anyio
async def test_retire_flips_can_write_false_before_closing():
    mgr = RoomManager()
    ws = uuid.uuid4()
    st = ClientState(user_id=1, can_write=True)
    await mgr.acquire((ws, "draft"), load=_empty_load, client=FakeClient(), state=st)
    await mgr.retire_workspace(ws)
    assert st.can_write is False


@pytest.mark.anyio
async def test_acquire_rejects_a_retired_workspace():
    mgr = RoomManager()
    ws = uuid.uuid4()
    await mgr.retire_workspace(ws)
    with pytest.raises(WorkspaceRetired):
        await mgr.acquire((ws, "draft"), load=_empty_load, client=FakeClient(),
                          state=ClientState(user_id=1, can_write=True))


@pytest.mark.anyio
async def test_operate_rejects_a_retired_workspace():
    mgr = RoomManager()
    ws = uuid.uuid4()
    await mgr.retire_workspace(ws)
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
    await mgr.retire_workspace(ws)                           # retirement lands mid-load
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
        await mgr.retire_workspace(ws)
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

    retire_task = asyncio.ensure_future(mgr.retire_workspace(ws))
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
