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


@pytest.mark.anyio
async def test_kernel_accept_aborts_on_tombstoned_room(pg_session_factory, tmp_path):
    # Site 2 (kernel path): a kernel accept whose room is tombstoned mid-flight must
    # abort BEFORE commit (RoomEvicted) — the pc stays pending, the durable log is
    # untouched, and a fresh connect rebuilds from the log. The Room.handle path is
    # covered above; this pins the kernel _body recheck + RoomEvicted, otherwise
    # untested.
    from pathlib import Path

    from call_manager.db.models import ProposedChange, User
    from call_manager.proposal_assist.collab import state
    from call_manager.proposal_assist.collab.hydrate import load_or_hydrate
    from call_manager.proposal_assist.collab.markdown_bridge import pm_to_markdown
    from call_manager.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json
    from call_manager.proposal_assist.editing.sectionizer import split_sections
    from call_manager.proposal_assist.proposals.kernel_accept import (
        RoomEvicted,
        accept_via_kernel,
    )
    from tests.proposal_assist.collab.test_collab_kernel_accept import (
        _git_head,
        _init_repo,
        _mk_pc,
    )
    from tests.proposal_assist.conftest import make_user, make_workspace, stamp_for

    with pg_session_factory() as s:
        o = make_user(s, email="k-tombstone@umu.se")
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, "Preamble.\n\n## A\n\nBody a.\n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    key = (wid, "draft")

    async def _load():
        with pg_session_factory() as s:
            u = load_or_hydrate(
                s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft",
                workspaces_dir=str(tmp_path)
            )
            s.commit()
            return u

    client = FakeClient()
    room = await MANAGER.acquire(
        key, load=_load, client=client, state=ClientState(user_id=oid, can_write=True)
    )
    base_sha = split_sections(pm_to_markdown(ydoc_to_pm_json(room.doc)))[1].sha
    with pg_session_factory() as s:
        pc_id = _mk_pc(
            s, wid, section_index=1, new_text="## A\n\nEdited.\n",
            base_commit_sha=_git_head(repo), base_section_sha=base_sha,
        )
    room.evicted = True   # tombstone the live room (still registered → operate reuses it)
    with pg_session_factory() as s:
        approver = s.get(User, oid)
        stamp = stamp_for(s, wid)  # the accept route's belief, formed while live
    try:
        with pytest.raises(RoomEvicted):
            await accept_via_kernel(
                session_factory=pg_session_factory, workspaces_dir=str(tmp_path),
                stamp=stamp, workspace_id=wid, pc_id=pc_id, approver=approver, artifact="draft",
            )
        with pg_session_factory() as s:
            assert s.get(ProposedChange, pc_id).status == "pending"   # not committed
    finally:
        await MANAGER.evict(key)
