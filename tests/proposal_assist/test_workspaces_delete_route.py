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

    assert ws_id not in MANAGER._retired_ws


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
    test_delete_runs_db_then_retire_then_scrub_in_that_order below."""
    from call_manager.proposal_assist.collab.room import MANAGER

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204

    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is not None      # DB committed
    # signal_workspace_retirement ran the collab hook, which tombstones the ws.
    assert ws_id in MANAGER._retired_ws
    MANAGER._retired_ws.discard(ws_id)                                  # don't leak the global


def test_delete_runs_db_then_retire_then_scrub_in_that_order(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """Route ORDER is the whole B5 contract, so assert it directly instead of
    inferring it from end state. Wrap all three route-module calls; each probe
    asserts the invariant that must ALREADY hold by the time it runs.

    NOTE: all three names are patched ON THE ROUTE MODULE (ws_api.<name>), not on
    their defining modules — the route imported them by value at module load, so
    patching call_manager.workspaces.scrub_workspace_bytes would not affect it.
    """
    import call_manager.web.api.workspaces as ws_api
    from call_manager.proposal_assist.collab.room import MANAGER

    calls: list[str] = []
    real_db = ws_api.soft_delete_workspace_db
    real_signal = ws_api.signal_workspace_retirement
    real_scrub = ws_api.scrub_workspace_bytes

    def _db(session, **kw):
        calls.append("db")
        return real_db(session, **kw)

    def _signal(request, *, workspace_id):
        calls.append("retire")
        # Strongest available assertion: the DB half is not merely "called first",
        # it is COMMITTED and visible to an INDEPENDENT session by now. A route that
        # retired inside the still-open delete transaction would fail here.
        with pg_session_factory() as s2:
            assert s2.get(Workspace, workspace_id).soft_deleted_at is not None
        return real_signal(request, workspace_id=workspace_id)

    def _scrub(workspace_id, **kw):
        calls.append("scrub")
        assert "retire" in calls          # bytes are never scrubbed before retirement
        return real_scrub(workspace_id, **kw)

    monkeypatch.setattr(ws_api, "soft_delete_workspace_db", _db)
    monkeypatch.setattr(ws_api, "signal_workspace_retirement", _signal)
    monkeypatch.setattr(ws_api, "scrub_workspace_bytes", _scrub)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204
    assert calls == ["db", "retire", "scrub"]
    MANAGER._retired_ws.discard(ws_id)


def test_no_new_turn_dispatches_after_delete_and_row_is_terminal(
    client_authed_owner, current_user, pg_session_factory
):
    """Spec test 14: after deletion no NEW turn dispatches and the turn row is
    terminal. We do NOT assert 'no send after the response' — that contradicts the
    conceded in-flight query race the design documents."""
    from call_manager.db.models import TurnJob
    from call_manager.proposal_assist.collab.room import MANAGER
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
    controller = client_authed_owner.app.state.workspace_controller
    assert controller._workspace_is_deleted(ws_id) is True
    MANAGER._retired_ws.discard(ws_id)
