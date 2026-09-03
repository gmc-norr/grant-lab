# tests/proposal_assist/collab/test_collab_permission_routes.py
"""Task 4 (Slice 2c): spy tests + e2e test that mutation routes call
signal_from_request after commit with the correct workspace_id + artifacts.

Spy tests (monkeypatch): fast, no real collab room needed — just assert the
route module's signal_from_request is called with the right arguments.

E2e test: enable collab, connect a member over the real WS test client, lock
the spec via the route, then assert the member's write flag was reconciled.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from call_manager.db.models import WorkspaceSetupJob
from call_manager.proposal_assist.collab import state as collab_state
from call_manager.proposal_assist.collab.room import MANAGER, ClientState
from tests.proposal_assist.collab.test_collab_room import FakeClient
from tests.test_api_spec import _add_spec
from tests.test_api_workspaces import _app, _client, _seed

# ---------------------------------------------------------------------------
# Shared seed helpers
# ---------------------------------------------------------------------------

def _seed_with_spec(app, pg_session_factory, tmp_path):
    """Seed owner + member + workspace + a committed non-empty spec. Returns
    (owner_id, member_id, ws_id)."""
    owner_id, member_id, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "draft\n"
    )
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")
    return owner_id, member_id, ws_id


def _seed_for_activate(pg_session_factory, app):
    """Seed a workspace in setup phase with a waiting_for_rubric_review job.
    Returns (owner_id, ws_id)."""
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        owner = make_user(s, email=f"rubric-owner-{uuid.uuid4().hex[:6]}@umu.se")
        ws = make_workspace(s, owner=owner, current_phase="setup")
        ws.rubric_criteria = [
            {"name": "Impact", "description": "d", "weight": 0.6, "scale_max": 5},
            {"name": "Feasibility", "description": "d", "weight": 0.4, "scale_max": 5},
        ]
        ws.rubric_funder_notes = "notes"
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="waiting_for_rubric_review"))
        s.commit()
        return owner.id, ws.id


# ---------------------------------------------------------------------------
# Spy test helpers
# ---------------------------------------------------------------------------

def _noop_signal(request, *, workspace_id, artifacts):
    pass


# ---------------------------------------------------------------------------
# post_lock_spec → signal(workspace_id, artifacts=["spec"])
# ---------------------------------------------------------------------------

def test_lock_route_signals_spec(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """POST /spec/lock calls signal_from_request with artifacts=["spec"]."""
    import call_manager.web.api.workspaces as wsmod

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, ws_id = _seed_with_spec(app, pg_session_factory, tmp_path)

    calls: list = []
    monkeypatch.setattr(
        wsmod,
        "signal_from_request",
        lambda request, *, workspace_id, artifacts: calls.append((workspace_id, artifacts)),
    )

    c = _client(app, owner_id)
    resp = c.post(f"/api/workspaces/{ws_id}/spec/lock")
    assert resp.status_code == 200, resp.text
    assert len(calls) == 1
    assert calls[0][0] == ws_id
    assert calls[0][1] == ["spec"]


def test_lock_route_no_signal_on_error(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """POST /spec/lock with empty spec (409) must NOT call signal_from_request."""
    import call_manager.web.api.workspaces as wsmod

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "   \n")  # whitespace-only → 409

    calls: list = []
    monkeypatch.setattr(
        wsmod,
        "signal_from_request",
        lambda request, *, workspace_id, artifacts: calls.append(artifacts),
    )

    c = _client(app, owner_id)
    resp = c.post(f"/api/workspaces/{ws_id}/spec/lock")
    assert resp.status_code == 409
    assert calls == []  # signal not fired on error path


# ---------------------------------------------------------------------------
# post_unlock_spec → signal(workspace_id, artifacts=["spec"])
# ---------------------------------------------------------------------------

def test_unlock_route_signals_spec(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """POST /spec/unlock calls signal_from_request with artifacts=["spec"]."""
    import call_manager.web.api.workspaces as wsmod

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, ws_id = _seed_with_spec(app, pg_session_factory, tmp_path)

    # Lock first so unlock is a real transition (else idempotent, but signal
    # still fires — unlock always succeeds).
    # Temporarily suppress the spy during lock so we capture only unlock.
    monkeypatch.setattr(wsmod, "signal_from_request", _noop_signal)
    c = _client(app, owner_id)
    c.post(f"/api/workspaces/{ws_id}/spec/lock")

    calls: list = []
    monkeypatch.setattr(
        wsmod,
        "signal_from_request",
        lambda request, *, workspace_id, artifacts: calls.append((workspace_id, artifacts)),
    )
    resp = c.post(f"/api/workspaces/{ws_id}/spec/unlock")
    assert resp.status_code == 200, resp.text
    assert len(calls) == 1
    assert calls[0][0] == ws_id
    assert calls[0][1] == ["spec"]


# ---------------------------------------------------------------------------
# soft_delete_workspace_api → signal(workspace_id, artifacts=["draft", "spec"])
# ---------------------------------------------------------------------------

def test_soft_delete_route_signals_both_artifacts(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """DELETE /workspaces/{id} RETIRES the workspace instead of signalling a
    permission refresh (Slice B-retire).

    Until B-retire the route called signal_from_request(artifacts=["draft","spec"])
    to make live editors re-resolve their permissions. Retirement subsumes that: it
    tears both rooms down and closes every socket 1008, so there is no client left
    to refresh. The coverage this test carries is unchanged — "deleting a workspace
    tells the collab layer, for BOTH artifacts" — only the mechanism moved, so the
    assertion moves with it rather than being dropped."""
    import call_manager.web.api.workspaces as wsmod

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")

    signal_calls: list = []
    retire_calls: list = []
    monkeypatch.setattr(
        wsmod,
        "signal_from_request",
        lambda request, *, workspace_id, artifacts: signal_calls.append((workspace_id, artifacts)),
    )
    monkeypatch.setattr(
        wsmod,
        "signal_workspace_retirement",
        lambda request, *, workspace_id, generation: retire_calls.append(workspace_id),
    )

    c = _client(app, owner_id)
    resp = c.delete(f"/api/workspaces/{ws_id}")
    assert resp.status_code == 204, resp.text
    assert retire_calls == [ws_id]      # retirement ran...
    assert signal_calls == []           # ...and subsumed the permission refresh


# ---------------------------------------------------------------------------
# activate (rubric.py) → signal(workspace_id, artifacts=["draft", "spec"])
# ---------------------------------------------------------------------------

def test_activate_route_signals_draft(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """POST /workspaces/{id}/activate signals BOTH artifacts: setup gates draft AND
    spec to owner-only, so activation lifts the write-gate for both."""
    import call_manager.web.api.rubric as rubmod

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, ws_id = _seed_for_activate(pg_session_factory, app)

    calls: list = []
    monkeypatch.setattr(
        rubmod,
        "signal_from_request",
        lambda request, *, workspace_id, artifacts: calls.append((workspace_id, artifacts)),
    )

    c = _client(app, owner_id)
    body = {
        "criteria": [
            {"name": "Impact", "description": "d", "weight": 0.6, "scale_max": 5},
            {"name": "Feasibility", "description": "d", "weight": 0.4, "scale_max": 5},
        ],
        "funder_notes": "",
    }
    resp = c.post(f"/api/workspaces/{ws_id}/activate", json=body)
    assert resp.status_code == 200, resp.text
    assert len(calls) == 1
    assert calls[0][0] == ws_id
    assert calls[0][1] == ["draft", "spec"]


def test_activate_no_signal_on_wrong_phase(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """POST /activate on an already-active workspace (409) must NOT call signal_from_request."""
    import call_manager.web.api.rubric as rubmod

    # Seed workspace with current_phase=active and job status != waiting_for_rubric_review
    app = _app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        from tests.proposal_assist.conftest import make_user, make_workspace
        owner = make_user(s, email=f"rubric2-owner-{uuid.uuid4().hex[:6]}@umu.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        ws.rubric_criteria = [
            {"name": "Impact", "description": "d", "weight": 0.6, "scale_max": 5},
        ]
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="done"))
        s.commit()
        o_id, w_id = owner.id, ws.id

    calls: list = []
    monkeypatch.setattr(
        rubmod,
        "signal_from_request",
        lambda request, *, workspace_id, artifacts: calls.append(artifacts),
    )

    c = _client(app, o_id)
    body = {"criteria": [{"name": "Impact", "description": "d", "weight": 1.0, "scale_max": 5}]}
    resp = c.post(f"/api/workspaces/{w_id}/activate", json=body)
    assert resp.status_code == 409
    assert calls == []


# ---------------------------------------------------------------------------
# remove_member (settings.py) → signal(workspace_id, artifacts=["draft", "spec"])
# ---------------------------------------------------------------------------

def test_remove_member_route_signals_both_artifacts(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """DELETE /members/{user_id} calls signal_from_request with artifacts=["draft","spec"]."""
    import call_manager.web.api.settings as setmod

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")

    calls: list = []
    monkeypatch.setattr(
        setmod,
        "signal_from_request",
        lambda request, *, workspace_id, artifacts: calls.append((workspace_id, artifacts)),
    )

    c = _client(app, owner_id)
    resp = c.delete(f"/api/workspaces/{ws_id}/members/{member_id}")
    assert resp.status_code == 204, resp.text
    assert len(calls) == 1
    assert calls[0][0] == ws_id
    assert calls[0][1] == ["draft", "spec"]


def test_add_member_route_does_not_signal(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """POST /members never calls signal_from_request (new member has no live connection)."""
    import call_manager.web.api.settings as setmod

    # We can only monkeypatch if the name exists in the module. If the import is
    # present, this succeeds; if signal_from_request is not imported into settings.py
    # the monkeypatch still works (it injects the name). The test asserts no CALL.
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, stranger_id, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")

    # We need a user NOT already in the workspace — use a fresh user.
    with pg_session_factory() as s:
        from call_manager.db.models import User
        new_user = User(status="active", email="newmember@umu.se")
        s.add(new_user)
        s.commit()
        new_email = new_user.email

    calls: list = []
    # Inject the spy into the settings module namespace (even if not imported yet,
    # monkeypatch.setattr on the module works for names that exist post-import).
    monkeypatch.setattr(setmod, "signal_from_request", lambda *a, **k: calls.append(True))

    c = _client(app, owner_id)
    resp = c.post(f"/api/workspaces/{ws_id}/members", json={"email": new_email})
    assert resp.status_code == 201, resp.text
    assert calls == []  # add_member must NOT call signal_from_request


# ---------------------------------------------------------------------------
# E2e: lock spec → member's can_write reconciled to False in the live room
# ---------------------------------------------------------------------------

@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_lock_spec_reconciles_member_write_in_live_room(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """End-to-end: POST /spec/lock signals the live room; connected member's
    can_write drops from True to False without disconnect."""
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, ws_id = _seed_with_spec(app, pg_session_factory, tmp_path)

    # Enable collab for the spec artifact so signal_from_request finds a live room.
    with pg_session_factory() as s:
        collab_state.enable_collab(s, workspace_id=ws_id, artifact="spec")
        s.commit()

    # Manually register a fake member client in the room (mirrors test_collab_permissions.py).
    key = (ws_id, "spec")
    member_fc = FakeClient()

    async def _load():
        return []

    await MANAGER.acquire(
        key, load=_load, client=member_fc,
        state=ClientState(user_id=member_id, can_write=True)
    )

    try:
        # Point the app at a real event loop so signal_from_request can schedule.
        app.state.app_loop = asyncio.get_running_loop()

        # POST /spec/lock — this fires signal_from_request which schedules
        # refresh_permissions on the running loop as a fire-and-forget future.
        c = _client(app, owner_id)
        resp = c.post(f"/api/workspaces/{ws_id}/spec/lock")
        assert resp.status_code == 200, resp.text

        # Poll until the scheduled refresh_permissions coroutine and its
        # run_in_threadpool(_resolve, ...) callback complete (bounded ~0.5 s).
        room = MANAGER.get(key)
        assert room is not None
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not room.clients[member_fc].can_write:
                break
        assert room.clients[member_fc].can_write is False  # downgraded by reconciliation
        assert member_fc.closed is None                    # NOT evicted (still a member)
    finally:
        await MANAGER.evict(key)
