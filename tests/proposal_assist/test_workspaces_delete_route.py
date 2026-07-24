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
    # stall the suite.
    monkeypatch.setattr(
        ws_api,
        "soft_delete_workspace",
        functools.partial(ws_api.soft_delete_workspace, timeout_seconds=1),
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
