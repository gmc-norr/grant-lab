# tests/proposal_assist/collab/test_collab_permissions.py
"""Task 2: refresh_permissions reconciliation tests (Slice 2c)."""
import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from call_manager.db.models import User, Workspace, WorkspaceViewer
from call_manager.proposal_assist.collab.permissions import (
    refresh_permissions,
    signal_permission_change,
)
from call_manager.proposal_assist.collab.room import MANAGER, ClientState
from tests.proposal_assist.collab.test_collab_room import FakeClient  # reuse the fake


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _seed_ws_owner_member(pg_session_factory):
    """Create owner + member Users, a Workspace (active phase), and a
    WorkspaceViewer for the member. Returns (ws_id, owner_id, member_id).
    No git repo on disk — Task-2 tests never checkpoint for real.
    """
    with pg_session_factory() as s:
        owner = User(status="active", email=f"perm-owner-{uuid.uuid4().hex[:8]}@umu.se")
        member = User(status="active", email=f"perm-member-{uuid.uuid4().hex[:8]}@umu.se")
        s.add(owner)
        s.add(member)
        s.flush()
        ws = Workspace(
            title="Perm-test workspace",
            owner_id=owner.id,
            current_phase="active",
        )
        s.add(ws)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=member.id))
        s.commit()
        return ws.id, owner.id, member.id


def _lock_spec(pg_session_factory, ws_id, _owner_id):
    """Set ws.spec_locked_at so resolve_access returns can_write=False for members."""
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        ws.spec_locked_at = datetime.now(UTC)
        s.commit()


def _remove_member(pg_session_factory, ws_id, member_id):
    """Delete the WorkspaceViewer row so the member loses membership."""
    with pg_session_factory() as s:
        s.query(WorkspaceViewer).filter_by(
            workspace_id=ws_id, user_id=member_id
        ).delete()
        s.commit()


def _soft_delete_ws(pg_session_factory, ws_id):
    """Set ws.soft_deleted_at so resolve_access returns (False, False) for everyone."""
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        ws.soft_deleted_at = datetime.now(UTC)
        s.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_refresh_downgrades_member_write_on_locked_spec(pg_session_factory, tmp_path):
    ws_id, owner_id, member_id = _seed_ws_owner_member(pg_session_factory)
    key = (ws_id, "spec")
    owner_c, member_c = FakeClient(), FakeClient()

    async def _load():
        return []

    await MANAGER.acquire(key, load=_load, client=owner_c,
                          state=ClientState(user_id=owner_id, can_write=True))
    await MANAGER.acquire(key, load=_load, client=member_c,
                          state=ClientState(user_id=member_id, can_write=True))
    try:
        # Lock the spec in the DB (member should lose spec write; owner keeps it).
        _lock_spec(pg_session_factory, ws_id, owner_id)
        await refresh_permissions(
            pg_session_factory, str(tmp_path), workspace_id=ws_id, artifact="spec"
        )
        room = MANAGER.get(key)
        assert room.clients[member_c].can_write is False   # soft downgrade
        assert room.clients[owner_c].can_write is True     # owner unaffected
        assert member_c.closed is None                     # NOT disconnected
    finally:
        await MANAGER.evict(key)


@pytest.mark.anyio
async def test_refresh_revokes_removed_member_keeps_others(pg_session_factory, tmp_path):
    # Live workspace, one member removed, owner stays connected → the removed member
    # is closed+deregistered; the room stays alive (owner) so no eviction/checkpoint.
    ws_id, owner_id, member_id = _seed_ws_owner_member(pg_session_factory)
    key = (ws_id, "draft")
    owner_c, member_c = FakeClient(), FakeClient()

    async def _load():
        return []

    await MANAGER.acquire(key, load=_load, client=owner_c,
                          state=ClientState(user_id=owner_id, can_write=True))
    await MANAGER.acquire(key, load=_load, client=member_c,
                          state=ClientState(user_id=member_id, can_write=True))
    try:
        _remove_member(pg_session_factory, ws_id, member_id)   # delete WorkspaceViewer
        await refresh_permissions(
            pg_session_factory, str(tmp_path), workspace_id=ws_id, artifact="draft"
        )
        room = MANAGER.get(key)
        assert member_c.closed == 1008                 # hard revoke, policy-violation
        assert member_c not in room.clients            # deregistered
        assert owner_c in room.clients                 # owner untouched
    finally:
        await MANAGER.evict(key)


@pytest.mark.anyio
async def test_refresh_live_last_client_revoke_checkpoints(
    pg_session_factory, tmp_path, monkeypatch
):
    # Codex High #2: a member removed while the LAST connected client on a LIVE
    # workspace must go through release(on_evict=checkpoint) — preserving the
    # normal last-leave checkpoint — NOT the no-checkpoint drop_client path.
    import call_manager.proposal_assist.collab.permissions as perm
    seen = {"factory": 0, "evict": 0}

    def _spy_factory(sf, wd, wid, art):
        seen["factory"] += 1
        async def _cb(_room):
            seen["evict"] += 1
        return _cb

    monkeypatch.setattr(perm, "make_checkpoint_on_evict", _spy_factory)
    ws_id, owner_id, member_id = _seed_ws_owner_member(pg_session_factory)
    key = (ws_id, "draft")
    member_c = FakeClient()

    async def _load():
        return []

    await MANAGER.acquire(key, load=_load, client=member_c,
                          state=ClientState(user_id=member_id, can_write=True))
    _remove_member(pg_session_factory, ws_id, member_id)
    await refresh_permissions(
        pg_session_factory, str(tmp_path), workspace_id=ws_id, artifact="draft"
    )
    assert member_c.closed == 1008
    assert MANAGER.get(key) is None                    # last client → evicted
    assert seen["factory"] == 1 and seen["evict"] == 1  # LIVE revoke → checkpoint path taken


@pytest.mark.anyio
async def test_refresh_upgrades_member_write_on_unlock(pg_session_factory, tmp_path):
    # Symmetry: the SAME mechanism flips can_write False -> True. Seed a member
    # connected with can_write=False (as if the spec were locked at connect); with
    # the spec UNLOCKED in the DB, resolve_access returns can_write=True.
    ws_id, owner_id, member_id = _seed_ws_owner_member(pg_session_factory)
    key = (ws_id, "spec")
    member_c = FakeClient()

    async def _load():
        return []

    await MANAGER.acquire(key, load=_load, client=member_c,
                          state=ClientState(user_id=member_id, can_write=False))
    try:
        # spec is unlocked (default) → member may write
        await refresh_permissions(
            pg_session_factory, str(tmp_path), workspace_id=ws_id, artifact="spec"
        )
        assert MANAGER.get(key).clients[member_c].can_write is True
        assert member_c.closed is None
    finally:
        await MANAGER.evict(key)


@pytest.mark.anyio
async def test_refresh_soft_delete_revokes_all_without_checkpoint(
    pg_session_factory, tmp_path, monkeypatch
):
    # Soft-delete makes resolve_access return is_member=False for EVERYONE (owner
    # included, ws_auth.py:106), so every client is closed+deregistered and the room
    # empties -> evicted via drop_client WITHOUT a checkpoint (workspace is gone).
    import call_manager.proposal_assist.collab.permissions as perm
    seen = {"factory": 0}
    monkeypatch.setattr(perm, "make_checkpoint_on_evict",
                        lambda *a, **k: seen.__setitem__("factory", seen["factory"] + 1))
    ws_id, owner_id, member_id = _seed_ws_owner_member(pg_session_factory)
    key = (ws_id, "draft")
    owner_c, member_c = FakeClient(), FakeClient()

    async def _load():
        return []

    await MANAGER.acquire(key, load=_load, client=owner_c,
                          state=ClientState(user_id=owner_id, can_write=True))
    await MANAGER.acquire(key, load=_load, client=member_c,
                          state=ClientState(user_id=member_id, can_write=True))
    _soft_delete_ws(pg_session_factory, ws_id)   # sets ws.soft_deleted_at + commit
    await refresh_permissions(
        pg_session_factory, str(tmp_path), workspace_id=ws_id, artifact="draft"
    )
    assert owner_c.closed == 1008 and member_c.closed == 1008
    assert MANAGER.get(key) is None              # room emptied -> evicted
    assert seen["factory"] == 0                  # NO checkpoint path (drop_client)


@pytest.mark.anyio
async def test_checkpoint_on_evict_skips_soft_deleted_workspace(
    pg_session_factory, tmp_path, monkeypatch
):
    # Robustness for the ws_deleted split (Codex High #2 review follow-up): even when
    # the WS route's own finally-release wins the last-leave race and passes this
    # callback for a soft-deleted workspace, the callback itself must NOT checkpoint.
    import call_manager.proposal_assist.collab.permissions as perm

    called = {"n": 0}
    monkeypatch.setattr(perm, "checkpoint_artifact",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    ws_id, owner_id, member_id = _seed_ws_owner_member(pg_session_factory)
    _soft_delete_ws(pg_session_factory, ws_id)

    cb = perm.make_checkpoint_on_evict(pg_session_factory, str(tmp_path), ws_id, "draft")
    await cb(None)                    # invoke the on_evict callback directly
    assert called["n"] == 0           # soft-deleted workspace → checkpoint skipped


@pytest.mark.anyio
async def test_refresh_is_noop_when_no_room(pg_session_factory, tmp_path):
    # No room registered for this key → clean no-op (the prod flag-off path):
    # returns without raising and creates no room.
    wid = uuid.uuid4()
    await refresh_permissions(
        pg_session_factory, str(tmp_path), workspace_id=wid, artifact="draft"
    )
    assert MANAGER.get((wid, "draft")) is None


# ---------------------------------------------------------------------------
# Task 7: Slice-3a cluster a/b/d — serialize + connect self-reconcile + fail-closed
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_refresh_permissions_holds_recon_lock_during_pass(
    pg_session_factory, tmp_path, monkeypatch
):
    # cluster (a): the reconcile pass runs UNDER room.recon_lock, so overlapping
    # signals serialize (a second pass cannot interleave into a stale can_write).
    import call_manager.proposal_assist.collab.permissions as perm
    ws_id, owner_id, member_id = _seed_ws_owner_member(pg_session_factory)
    key = (ws_id, "spec")
    member_c = FakeClient()

    async def _load():
        return []

    room = await MANAGER.acquire(key, load=_load, client=member_c,
                                 state=ClientState(user_id=member_id, can_write=True))
    held = {"locked": None}
    real_resolve = perm._resolve

    def _spy_resolve(sf, uid, wid, art):
        held["locked"] = room.recon_lock.locked()   # observed DURING the pass
        return real_resolve(sf, uid, wid, art)

    monkeypatch.setattr(perm, "_resolve", _spy_resolve)
    try:
        await refresh_permissions(
            pg_session_factory, str(tmp_path), workspace_id=ws_id, artifact="spec"
        )
        assert held["locked"] is True   # pass ran while holding the recon lock
    finally:
        await MANAGER.evict(key)


@pytest.mark.anyio
async def test_reconcile_client_downgrades_on_locked_spec(pg_session_factory, tmp_path):
    # (b) The connect self-reconcile helper flips a just-registered client's
    # can_write when the DB says it should be read-only.
    from call_manager.proposal_assist.collab.permissions import reconcile_client
    ws_id, owner_id, member_id = _seed_ws_owner_member(pg_session_factory)
    key = (ws_id, "spec")
    member_c = FakeClient()

    async def _load():
        return []

    room = await MANAGER.acquire(key, load=_load, client=member_c,
                                 state=ClientState(user_id=member_id, can_write=True))
    try:
        _lock_spec(pg_session_factory, ws_id, owner_id)
        st = room.clients[member_c]
        async with room.recon_lock:
            await reconcile_client(pg_session_factory, str(tmp_path), room, member_c, st,
                                   workspace_id=ws_id, artifact="spec")
        assert room.clients[member_c].can_write is False
    finally:
        await MANAGER.evict(key)


@pytest.mark.anyio
async def test_reconcile_client_fails_closed_on_resolve_error(
    pg_session_factory, tmp_path, monkeypatch
):
    # (d) On a resolve error (DB blip), the client is DOWNGRADED to read-only, NOT
    # disconnected — no stale write access leaks, but a transient blip does not
    # drop an innocent editor.
    import call_manager.proposal_assist.collab.permissions as perm
    from call_manager.proposal_assist.collab.permissions import reconcile_client
    ws_id, owner_id, member_id = _seed_ws_owner_member(pg_session_factory)
    key = (ws_id, "spec")
    member_c = FakeClient()

    async def _load():
        return []

    room = await MANAGER.acquire(key, load=_load, client=member_c,
                                 state=ClientState(user_id=member_id, can_write=True))

    def _boom(*_a, **_k):
        raise RuntimeError("db blip")

    monkeypatch.setattr(perm, "_resolve", _boom)
    try:
        st = room.clients[member_c]
        async with room.recon_lock:
            await reconcile_client(pg_session_factory, str(tmp_path), room, member_c, st,
                                   workspace_id=ws_id, artifact="spec")
        assert st.can_write is False           # fail-closed downgrade
        assert member_c.closed is None          # NOT disconnected
        assert member_c in room.clients         # still registered
    finally:
        await MANAGER.evict(key)


# ---------------------------------------------------------------------------
# Task 3: signal_permission_change bridge tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_signal_no_loop_is_safe_noop(pg_session_factory, tmp_path):
    futs = signal_permission_change(None, pg_session_factory, str(tmp_path),
                                    workspace_id=uuid.uuid4(), artifacts=["draft", "spec"])
    assert futs == []


@pytest.mark.anyio
async def test_signal_closed_loop_never_raises(pg_session_factory, tmp_path):
    # A closed loop makes run_coroutine_threadsafe raise; signal must swallow it
    # (fail-closed) so a mutation route can never see an exception. Codex Medium #1.
    dead = asyncio.new_event_loop()
    dead.close()
    futs = signal_permission_change(dead, pg_session_factory, str(tmp_path),
                                    workspace_id=uuid.uuid4(), artifacts=["draft"])
    assert futs == []   # scheduling failed, but no exception escaped


@pytest.mark.anyio
async def test_signal_schedules_refresh_on_loop(pg_session_factory, tmp_path):
    ws_id, owner_id, member_id = _seed_ws_owner_member(pg_session_factory)
    key = (ws_id, "spec")
    member_c = FakeClient()

    async def _load():
        return []

    await MANAGER.acquire(key, load=_load, client=member_c,
                          state=ClientState(user_id=member_id, can_write=True))
    try:
        _lock_spec(pg_session_factory, ws_id, owner_id)
        loop = asyncio.get_running_loop()
        futs = signal_permission_change(loop, pg_session_factory, str(tmp_path),
                                        workspace_id=ws_id, artifacts=["spec"])
        assert len(futs) == 1
        # run_coroutine_threadsafe returns a concurrent.futures.Future; give the
        # loop a turn so the scheduled coroutine runs, then confirm the effect.
        await asyncio.wrap_future(futs[0])
        assert MANAGER.get(key).clients[member_c].can_write is False
    finally:
        await MANAGER.evict(key)


# ---------------------------------------------------------------------------
# Session-revocation: _resolve fails closed on a disabled account
# ---------------------------------------------------------------------------

def test_resolve_fails_closed_for_disabled_user(pg_session_factory):
    # A disabled account that is still a workspace member must resolve as
    # not-member, so any permission reconcile closes+deregisters its live socket
    # (defense-in-depth beneath the prompt revocation sweep). _resolve is sync
    # (runs in a threadpool in prod), so it is called directly here.
    from call_manager.proposal_assist.collab import permissions as perms

    ws_id, _owner_id, member_id = _seed_ws_owner_member(pg_session_factory)

    is_member, _can_write, ws_deleted = perms._resolve(
        pg_session_factory, member_id, ws_id, "draft"
    )
    assert is_member is True and ws_deleted is False   # active member baseline

    with pg_session_factory() as s:
        s.get(User, member_id).status = "disabled"
        s.commit()

    is_member, can_write, ws_deleted = perms._resolve(
        pg_session_factory, member_id, ws_id, "draft"
    )
    assert is_member is False and can_write is False and ws_deleted is False
