"""DELETE /api/workspaces/{id} under commit-mutex contention."""

import functools
import threading

from call_manager.db.models import Workspace
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex
from tests.proposal_assist.conftest import make_workspace


def test_delete_returns_429_when_the_commit_mutex_is_held(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """Deletion now takes the commit mutex, so a wedged writer makes it busy —
    the house 429, having committed NOTHING (not a 500, not a partial delete)."""
    import call_manager.web.api.workspaces as ws_api

    # Pin a short mutex timeout for this test only: we are exercising the route's
    # error mapping, not the production timeout value, and the 10s default would
    # stall the suite. The patch target is the DB half since B-retire — the route
    # no longer calls the soft_delete_workspace wrapper.
    monkeypatch.setattr(
        ws_api,
        "soft_delete_workspace_db",
        functools.partial(ws_api.soft_delete_workspace_db, timeout_seconds=1),
    )

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    holding = threading.Event()
    release = threading.Event()

    def _holder():
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    t = threading.Thread(target=_holder)
    t.start()
    try:
        holding.wait(timeout=5)
        resp = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
    finally:
        release.set()
        t.join(timeout=30)

    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") == "2"
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is None
    # ...and NO retirement ran: the route bails before the retirement bridge, so
    # a contended delete must not tombstone the workspace in-process either.
    from call_manager.proposal_assist.collab.room import MANAGER

    assert not MANAGER._lifecycle.is_retired(ws_id)


def test_delete_still_returns_204_uncontended(
    client_authed_owner, current_user, pg_session_factory
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is not None


def test_second_delete_is_404_not_a_second_204(
    client_authed_owner, current_user, pg_session_factory
):
    """The route's documented non-idempotent contract. Sequentially this is
    require_owner's 404; the interesting concurrent case (both requests pass
    require_owner, the loser then wins the mutex) is covered deterministically by
    test_soft_delete_refuses_a_second_deletion in tests/test_workspaces_service.py
    — the under-mutex read is what makes the loser 404 rather than re-stamp."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204
    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 404


def test_delete_returns_204_and_retires(
    client_authed_owner, current_user, pg_session_factory
):
    """End state of a successful delete: 204, the row is soft-deleted, and the
    retirement bridge actually ran (the workspace is tombstoned in the
    process-global room manager). ORDERING is asserted separately, by
    test_delete_runs_db_then_retire_then_204_in_that_order below."""
    from call_manager.proposal_assist.collab.room import MANAGER

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204

    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is not None  # DB committed
    # signal_workspace_retirement ran the coordinator, which tombstones the ws
    # via the lifecycle authority (Slice C0). No cleanup needed — the authority
    # uses unique UUIDs per test, so this ws_id cannot pollute other tests.
    assert MANAGER._lifecycle.is_retired(ws_id)


def test_delete_runs_db_then_retire_then_204_in_that_order(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """Route ORDER is the contract, so assert it directly instead of inferring
    it from end state. Wrap both route-module calls; each probe asserts the
    invariant that must ALREADY hold by the time it runs.

    NOTE: both names are patched ON THE ROUTE MODULE (ws_api.<name>), not on
    their defining modules — the route imported them by value at module load, so
    patching call_manager.workspaces.soft_delete_workspace_db would not affect it.
    """
    import call_manager.web.api.workspaces as ws_api

    calls: list[str] = []
    real_db = ws_api.soft_delete_workspace_db
    real_signal = ws_api.signal_workspace_retirement

    def _db(session, **kw):
        calls.append("db")
        return real_db(session, **kw)

    def _signal(request, *, workspace_id, generation):
        calls.append("retire")
        # Strongest available assertion: the DB half is not merely "called first",
        # it is COMMITTED and visible to an INDEPENDENT session by now. A route that
        # retired inside the still-open delete transaction would fail here.
        with pg_session_factory() as s2:
            assert s2.get(Workspace, workspace_id).soft_deleted_at is not None
        return real_signal(request, workspace_id=workspace_id, generation=generation)

    monkeypatch.setattr(ws_api, "soft_delete_workspace_db", _db)
    monkeypatch.setattr(ws_api, "signal_workspace_retirement", _signal)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204
    # then 204. That no FS step follows is pinned by
    # test_delete_keeps_every_byte_on_disk, not here — the scrub is no longer
    # patched onto ws_api, so this probe could not see it either way.
    assert calls == ["db", "retire"]


def test_no_new_turn_dispatches_after_delete_and_row_is_terminal(
    client_authed_owner, current_user, pg_session_factory
):
    """Spec test 14: after deletion no NEW turn dispatches and the turn row is
    terminal. We do NOT assert 'no send after the response' — that contradicts the
    conceded in-flight query race the design documents."""
    from call_manager.db.models import TurnJob
    from tests.proposal_assist.conftest import make_chat_turn  # conftest.py:247

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        job = TurnJob(
            workspace_id=ws.id, status="running",
            user_turn_id=make_chat_turn(
                s, ws, role="user", content={"type": "text", "text": "q"}).id,
            submitted_by_user_id=ws.owner_id,
        )
        s.add(job)
        s.commit()
        ws_id, job_id = ws.id, job.id

    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status != "running"     # terminalized in the delete txn
        assert s.get(Workspace, ws_id).soft_deleted_at is not None

    # No NEW turn dispatches: assert the exact SYNC gate controller.launch consults
    # before it would dispatch (Slice A's soft_deleted_at floor). This is the
    # observable "no new dispatch" without driving async launch from a sync test,
    # and it deliberately says NOTHING about an in-flight client.query (conceded race).
    #
    # NOTE: this is Slice A's DB read, NOT B-retire's in-process turns tombstone.
    # test_delete_reaches_the_turns_registry_tombstone below covers that half —
    # without it the whole turns hook could be unwired and this test would not care.
    controller = client_authed_owner.app.state.workspace_controller
    assert controller._workspace_is_deleted(ws_id) is True


def test_delete_reaches_the_turns_registry_tombstone(
    client_authed_owner, current_user, pg_session_factory
):
    """End-to-end for the TURNS half of the retirement fan-out: route ->
    signal_workspace_retirement -> coordinator -> SubprocessRegistry.retire_workspace.

    Every layer was already covered in isolation (the registry in
    test_drafting_retire_tombstone, the coordinator in test_retirement_coordinator,
    the route's db/retire ORDER with the bridge stubbed out) — but nothing
    joined them, so replacing main.py's `"turns": ...registry.retire_workspace`
    hook with a no-op left the entire proposal_assist suite green (1827 passed).
    A silently unwired hook means deleting a workspace never stops its turns, which
    is the guarantee B-retire exists to deliver.

    Asserts the FUNCTIONAL refusal, not just set membership: a post-delete launch
    claim must raise. Driven on the app loop because the registry is async and this
    route test is sync.
    """
    import asyncio

    from call_manager.proposal_assist.drafting.contract import WorkspaceRetired

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    app = client_authed_owner.app
    registry = app.state.workspace_controller.registry
    assert not registry._lifecycle.is_retired(ws_id)  # precondition: nothing pre-tombstoned

    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204

    assert registry._lifecycle.is_retired(ws_id)  # the turns hook actually ran
    # ...and the tombstone REFUSES, rather than merely being recorded.
    fut = asyncio.run_coroutine_threadsafe(
        registry.claim_launch_or_wait(ws_id), app.state.app_loop
    )
    try:
        fut.result(timeout=5)
    except WorkspaceRetired:
        pass                                   # expected
    else:
        raise AssertionError("claim_launch_or_wait did not refuse a retired workspace")


def test_the_route_signals_retirement_with_the_generation_it_committed(
    monkeypatch, client_authed_owner, pg_session_factory, current_user
):
    """Spec 4.4. If the route sampled the generation after the commit, a restore
    landing in between would make this delete's teardown look current. Pinned by
    asserting the value passed equals the value the commit produced, not the
    value the row happens to hold when the signal runs."""
    import call_manager.web.api.workspaces as ws_api

    captured: dict = {}

    def _capture(request, *, workspace_id, generation):
        captured["workspace_id"] = workspace_id
        captured["generation"] = generation
        # Simulate a transition landing between the commit and the signal.
        with pg_session_factory() as s:
            ws = s.get(Workspace, workspace_id)
            ws.lifecycle_generation = 99
            s.commit()

    monkeypatch.setattr(ws_api, "signal_workspace_retirement", _capture)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204
    assert captured["workspace_id"] == ws_id
    assert captured["generation"] == 1, (
        "the route passed a generation it re-read instead of the one its own "
        "transaction allocated"
    )
