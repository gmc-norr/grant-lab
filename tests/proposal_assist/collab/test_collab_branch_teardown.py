"""prepare_branch_switch: the teardown a branch switch needs.

Two properties, and they pull in opposite directions — which is why both are
tested. It must REPORT that it could not linearize (so the switch aborts), and
it must FINISH tearing down anyway (so no writable orphan survives the abort)."""

import asyncio
import uuid

import pytest

from call_manager.proposal_assist.collab.room import MANAGER, WS_DOCUMENT_REPLACED, ClientState
from tests.proposal_assist.collab.test_collab_room import FakeClient


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _open(key):
    async def _load():
        return []

    client = FakeClient()
    room = await MANAGER.acquire(
        key, load=_load, client=client, state=ClientState(user_id=1, can_write=True)
    )
    return room, client


@pytest.mark.anyio
async def test_tears_down_both_artifact_rooms():
    wid = uuid.uuid4()
    draft, dc = await _open((wid, "draft"))
    spec, sc = await _open((wid, "spec"))

    res = await MANAGER.prepare_branch_switch(wid)

    assert res.locked is True
    assert res.closed == 2
    assert draft.evicted is True and spec.evicted is True
    assert MANAGER._rooms.get((wid, "draft")) is None
    assert MANAGER._rooms.get((wid, "spec")) is None


@pytest.mark.anyio
async def test_closes_with_document_replaced_so_the_client_discards_its_old_doc():
    # Reconnecting the retained Y.Doc would merge the source branch into the
    # target. The application code tells the client to rebuild before reconnecting.
    wid = uuid.uuid4()
    _, client = await _open((wid, "draft"))
    await MANAGER.prepare_branch_switch(wid)
    assert client.closed == WS_DOCUMENT_REPLACED


@pytest.mark.anyio
async def test_reports_locked_False_when_room_lock_is_held():
    wid = uuid.uuid4()
    room, _ = await _open((wid, "draft"))

    async with room.lock:  # stand in for a persist in flight
        res = await asyncio.wait_for(MANAGER.prepare_branch_switch(wid), timeout=10)

    assert res.locked is False  # the switch must abort on this


@pytest.mark.anyio
async def test_still_FINISHES_the_teardown_when_the_lock_times_out():
    # THE ORPHAN TEST. The teardown pops the room before it reaches for the lock,
    # so a teardown that gives up on timeout leaves a room that is out of the
    # registry, un-tombstoned and still writable — held directly by its clients
    # (collab_ws.py:144). A retry tears down a DIFFERENT room, truncates, and the
    # orphan's next keystroke lands afterwards.
    wid = uuid.uuid4()
    room, client = await _open((wid, "draft"))

    async with room.lock:
        res = await asyncio.wait_for(MANAGER.prepare_branch_switch(wid), timeout=10)

    assert res.locked is False
    assert MANAGER._rooms.get((wid, "draft")) is None  # popped
    assert room.evicted is True  # AND tombstoned
    assert client.closed == WS_DOCUMENT_REPLACED  # AND socket closed


@pytest.mark.anyio
async def test_is_idempotent_on_a_workspace_with_no_rooms():
    res = await MANAGER.prepare_branch_switch(uuid.uuid4())
    assert res.locked is True and res.closed == 0
