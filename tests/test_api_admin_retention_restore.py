"""POST /api/admin/workspaces/deleted/{workspace_id}/restore — admin restore route.

Coverage:
  1. admin restores deleted workspace → 204, soft_deleted_at NULL, one audit row
  2. missing → 404; non-admin → 403; live → 204 with NO audit row
  3. second restore of a now-live workspace → 204, still exactly one audit row
  4. CommitMutexTimeout → 429 with Retry-After
  5. generation forwarding — route hands the generation the service returned, never
     a re-read one
  6. fault injection — signal_workspace_restored returns False → 503 + Retry-After;
     retry with real bridge → 204, workspace usable, exactly one audit row total
  7. route works with RETENTION_INSPECTION_ENABLED unset (restore is NOT gated)
"""

import uuid

from sqlalchemy import select

from grant_lab.db.models import Workspace, WorkspaceEvent
from grant_lab.web.api import admin_retention as ws_api
from tests.admin_retention_helpers import client_for, setup_app, soft_delete
from tests.proposal_assist.conftest import make_user, make_workspace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_workspace(sf):
    """Create a plain workspace (not deleted). Returns workspace_id."""
    with sf() as s:
        owner = make_user(s, email=f"owner-{uuid.uuid4().hex[:8]}@umu.se")
        ws = make_workspace(s, owner=owner, title="Test WS")
        s.commit()
        return ws.id


def _audit_rows(sf, workspace_id):
    with sf() as s:
        rows = (
            s.execute(
                select(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == workspace_id)
                .where(WorkspaceEvent.kind == "admin_deleted_workspace_restored")
            )
            .scalars()
            .all()
        )
        return rows


def _path(ws_id):
    return f"/api/admin/workspaces/deleted/{ws_id}/restore"


# ---------------------------------------------------------------------------
# Test 1: happy path
# ---------------------------------------------------------------------------


def test_admin_restores_deleted_workspace(
    make_app, monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Admin POSTing restore on a deleted workspace → 204, row is live, audit row written."""
    app = setup_app(make_app, monkeypatch, pg_url, tmp_path)
    c, _ = client_for(app, pg_session_factory, email="admin@umu.se", is_admin=True)
    ws_id = _seed_workspace(pg_session_factory)
    with pg_session_factory() as s:
        soft_delete(s, ws_id)

    with c:
        r = c.post(_path(ws_id))
    assert r.status_code == 204, r.text

    # Row is live (soft_deleted_at NULL)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        assert ws.soft_deleted_at is None

    # Exactly one audit row
    rows = _audit_rows(pg_session_factory, ws_id)
    assert len(rows) == 1
    assert rows[0].payload["workspace_id"] == str(ws_id)


# ---------------------------------------------------------------------------
# Test 2: missing, non-admin, live
# ---------------------------------------------------------------------------


def test_missing_workspace_returns_404(make_app, monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = setup_app(make_app, monkeypatch, pg_url, tmp_path)
    c, _ = client_for(app, pg_session_factory, email="admin@umu.se", is_admin=True)
    missing_id = uuid.uuid4()
    r = c.post(_path(missing_id))
    assert r.status_code == 404, r.text
    assert "error" in r.json()


def test_non_admin_returns_403(make_app, monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = setup_app(make_app, monkeypatch, pg_url, tmp_path)
    ws_id = _seed_workspace(pg_session_factory)
    with pg_session_factory() as s:
        soft_delete(s, ws_id)
    c, _ = client_for(app, pg_session_factory, email="user@umu.se", is_admin=False)
    r = c.post(_path(ws_id))
    assert r.status_code == 403, r.text


def test_live_workspace_returns_204_without_audit(
    make_app, monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """A live workspace returns 204 immediately with no audit row written."""
    app = setup_app(make_app, monkeypatch, pg_url, tmp_path)
    c, _ = client_for(app, pg_session_factory, email="admin@umu.se", is_admin=True)
    ws_id = _seed_workspace(pg_session_factory)

    # Workspace is LIVE (not deleted)
    with c:
        r = c.post(_path(ws_id))
    assert r.status_code == 204, r.text

    # No audit rows
    rows = _audit_rows(pg_session_factory, ws_id)
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# Test 3: idempotent second restore (workspace is now live)
# ---------------------------------------------------------------------------


def test_second_restore_is_204_with_exactly_one_audit_row(
    make_app, monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """A second call after successful restore finds the workspace live → 204,
    total audit rows stays at 1."""
    app = setup_app(make_app, monkeypatch, pg_url, tmp_path)
    c, _ = client_for(app, pg_session_factory, email="admin@umu.se", is_admin=True)
    ws_id = _seed_workspace(pg_session_factory)
    with pg_session_factory() as s:
        soft_delete(s, ws_id)

    with c:
        r1 = c.post(_path(ws_id))
        assert r1.status_code == 204, r1.text
        r2 = c.post(_path(ws_id))
        assert r2.status_code == 204, r2.text

    # Still exactly one audit row (the second path found it live → no write)
    rows = _audit_rows(pg_session_factory, ws_id)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Test 4: CommitMutexTimeout → 429
# ---------------------------------------------------------------------------


def test_commit_mutex_timeout_is_429(make_app, monkeypatch, pg_url, pg_session_factory, tmp_path):
    """CommitMutexTimeout must surface as 429 + Retry-After: 2 with BUSY_ERROR_MESSAGE."""
    from grant_lab.proposal_assist.drafting.locking import (
        BUSY_ERROR_MESSAGE,
        CommitMutexTimeout,
    )

    app = setup_app(make_app, monkeypatch, pg_url, tmp_path)
    c, _ = client_for(app, pg_session_factory, email="admin@umu.se", is_admin=True)
    ws_id = _seed_workspace(pg_session_factory)
    with pg_session_factory() as s:
        soft_delete(s, ws_id)

    def _timeout(*a, **kw):
        raise CommitMutexTimeout("held by another writer")

    monkeypatch.setattr(ws_api, "restore_workspace_db", _timeout)

    r = c.post(_path(ws_id))
    assert r.status_code == 429, r.text
    assert r.headers["Retry-After"] == "2"
    assert r.json()["error"]["message"] == BUSY_ERROR_MESSAGE


# ---------------------------------------------------------------------------
# Test 5: generation forwarding
# ---------------------------------------------------------------------------


def test_route_forwards_the_generation_it_received_not_a_re_read(
    make_app, monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """The route must forward RestoreOutcome.generation to signal_workspace_restored,
    never re-read one from the DB after the commit.

    Technique: wrap restore_workspace_db so that after it commits, we bump the row's
    lifecycle_generation to 99 on a separate session. If the route re-reads the
    generation it would send 99; if it forwards the outcome it sends the real one.
    """
    app = setup_app(make_app, monkeypatch, pg_url, tmp_path)
    c, _ = client_for(app, pg_session_factory, email="admin@umu.se", is_admin=True)
    ws_id = _seed_workspace(pg_session_factory)
    with pg_session_factory() as s:
        soft_delete(s, ws_id)

    captured = {}

    real_restore = ws_api.restore_workspace_db

    def _restore_then_move_the_row(session, **kw):
        outcome = real_restore(session, **kw)
        # Stash the generation the service returned BEFORE bumping the row.
        captured["expected_generation"] = outcome.generation
        # Bump the row to 99 AFTER the real transaction committed but BEFORE the
        # route reads the outcome — any re-read from here on yields 99.
        with pg_session_factory() as other:
            ws_row = other.get(Workspace, kw["workspace_id"])
            ws_row.lifecycle_generation = 99
            other.commit()
        return outcome

    def _capture_signal(request, *, workspace_id, generation):
        captured["generation"] = generation
        return True  # pretend the bridge worked

    monkeypatch.setattr(ws_api, "restore_workspace_db", _restore_then_move_the_row)
    monkeypatch.setattr(ws_api, "signal_workspace_restored", _capture_signal)

    r = c.post(_path(ws_id))
    assert r.status_code == 204, r.text
    assert "generation" in captured
    # Must NOT be 99 (the re-read value); must be what the service returned
    assert captured["generation"] != 99
    # Must be the EXACT generation the service returned (positive check)
    assert captured["generation"] == captured["expected_generation"]


# ---------------------------------------------------------------------------
# Test 6: fault injection — 503 then retry succeeds, exactly one audit row
# ---------------------------------------------------------------------------


def test_fault_injection_503_then_retry_with_real_bridge(
    make_app, monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Force signal_workspace_restored to return False → 503 + Retry-After.
    Then retry with the real bridge → 204, workspace is usable, exactly one
    audit row across both calls."""
    from grant_lab.proposal_assist.lifecycle import signal_workspace_restored as _real_signal

    app = setup_app(make_app, monkeypatch, pg_url, tmp_path)
    c, _ = client_for(app, pg_session_factory, email="admin@umu.se", is_admin=True)
    ws_id = _seed_workspace(pg_session_factory)
    with pg_session_factory() as s:
        soft_delete(s, ws_id)

    # First call: signal returns False → 503
    monkeypatch.setattr(ws_api, "signal_workspace_restored", lambda *a, **kw: False)
    r1 = c.post(_path(ws_id))
    assert r1.status_code == 503, r1.text
    assert r1.headers["Retry-After"] == "2"
    assert "error" in r1.json()

    # After the 503, the DB is committed (workspace is live) but in-memory authority
    # may be behind. There must be exactly one audit row.
    rows_after_503 = _audit_rows(pg_session_factory, ws_id)
    assert len(rows_after_503) == 1

    # Workspace is already live in DB (the 503 committed but couldn't apply)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        assert ws.soft_deleted_at is None

    # Restore signal; retry with the real bridge (lifespan needed)
    monkeypatch.setattr(ws_api, "signal_workspace_restored", _real_signal)
    with c:
        r2 = c.post(_path(ws_id))
    # workspace is live → the pre-lock read sees LIVE → 204 immediately, no new audit
    assert r2.status_code == 204, r2.text

    # Still exactly one audit row total across both calls
    rows_total = _audit_rows(pg_session_factory, ws_id)
    assert len(rows_total) == 1


# ---------------------------------------------------------------------------
# Test 7: route is NOT gated by RETENTION_INSPECTION_ENABLED
# ---------------------------------------------------------------------------


def test_restore_works_without_retention_inspection_enabled(
    make_app, monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Restore must be reachable even when RETENTION_INSPECTION_ENABLED is unset
    (the default). The neighbouring GET IS gated; restore reads nothing back."""
    # RETENTION_INSPECTION_ENABLED is intentionally NOT set here
    app = setup_app(make_app, monkeypatch, pg_url, tmp_path)
    # Confirm the flag is off
    assert not app.state.settings.retention_inspection_enabled

    c, _ = client_for(app, pg_session_factory, email="admin@umu.se", is_admin=True)
    ws_id = _seed_workspace(pg_session_factory)
    with pg_session_factory() as s:
        soft_delete(s, ws_id)

    with c:
        r = c.post(_path(ws_id))
    assert r.status_code == 204, r.text


# ---------------------------------------------------------------------------
# Test 8: OpenAPI schema advertises 204 (not 200)
# ---------------------------------------------------------------------------


def test_openapi_schema_advertises_204_not_200(
    make_app, monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """The route decorator must carry status_code=204 so FastAPI builds the correct
    OpenAPI response. Returning Response(status_code=204) without the decorator kwarg
    advertises 200 in the schema while returning 204 at runtime — the TS codegen would
    see the wrong code."""
    app = setup_app(make_app, monkeypatch, pg_url, tmp_path)
    schema = app.openapi()
    path_key = "/api/admin/workspaces/deleted/{workspace_id}/restore"
    assert path_key in schema["paths"], f"route not in OpenAPI schema: {path_key}"
    responses = schema["paths"][path_key]["post"]["responses"]
    assert "204" in responses, f"204 not in responses: {list(responses.keys())}"
    assert "200" not in responses, f"200 unexpectedly in responses: {list(responses.keys())}"
