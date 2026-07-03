import uuid

import pytest

from call_manager.proposal_assist.collab import protocol as P
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
from call_manager.proposal_assist.collab.room import MANAGER, ClientState
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from tests.proposal_assist.collab.test_collab_room import FakeClient


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_evict_sets_tombstone():
    key = (uuid.uuid4(), "draft")

    async def _load():
        return []

    client = FakeClient()
    room = await MANAGER.acquire(key, load=_load, client=client,
                                 state=ClientState(user_id=1, can_write=True))
    assert room.evicted is False
    await MANAGER.evict(key)
    assert room.evicted is True          # tombstoned
    assert client.closed is not None      # socket force-closed


@pytest.mark.anyio
async def test_handle_drops_write_on_tombstoned_room():
    # A write frame arriving after eviction must NOT apply or persist.
    key = (uuid.uuid4(), "draft")

    async def _load():
        return []

    client = FakeClient()
    room = await MANAGER.acquire(key, load=_load, client=client,
                                 state=ClientState(user_id=1, can_write=True))
    room.evicted = True
    persisted = []

    async def _persist(uid, update):
        persisted.append(update)

    upd = pm_json_to_ydoc(markdown_to_pm("## A\n\nx.\n")).get_update()
    await room.handle(sender=client, state=room.clients[client],
                      message=P.create_update_message(upd), persist=_persist)
    assert persisted == []   # tombstoned room did not persist the write
    await MANAGER.evict(key)
