import uuid
from datetime import UTC, datetime, timedelta

from call_manager.db.models import User, WorkspaceEvent
from tests.proposal_assist.conftest import make_user, make_workspace


def _ws(_app, current_user, pg_session_factory, tmp_path):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, current_phase="active")
        s.commit()
        return ws.id


def test_activity_empty_shape(client_authed_owner, _app, current_user, pg_session_factory, tmp_path):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/activity")
    assert r.status_code == 200
    assert r.json() == {"items": []}


def test_activity_happy_path_orders_and_summarizes(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    # Explicit, distinct created_at values: events added in one transaction share
    # func.now(), so we cannot rely on insertion order — set the timestamps so
    # newest-first is deterministic regardless of the random UUID tie-breaker.
    t0 = datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 6, 23, 12, 5, 0, tzinfo=UTC)
    with pg_session_factory() as s:
        s.add(WorkspaceEvent(workspace_id=ws_id, kind="created", actor_id=current_user.id, created_at=t0))
        s.add(
            WorkspaceEvent(
                workspace_id=ws_id,
                kind="reference.added",
                actor_id=current_user.id,
                payload={"reference_id": "x"},
                created_at=t1,
            )
        )
        s.commit()
    body = client_authed_owner.get(f"/api/workspaces/{ws_id}/activity").json()
    summaries = [it["summary"] for it in body["items"]]
    # newest-first: reference.added (t1) precedes created (t0)
    assert summaries == ["added a reference", "created the workspace"]
    assert all(it["actor"] == current_user.email for it in body["items"])


def test_activity_resolves_rubric_edited_via_payload(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        editor = User(status="active", email="editor@ki.se")
        s.add(editor)
        s.flush()
        s.add(
            WorkspaceEvent(
                workspace_id=ws_id,
                kind="rubric_edited",
                actor_id=None,
                payload={"edited_by_user_id": editor.id},
            )
        )
        s.commit()
    body = client_authed_owner.get(f"/api/workspaces/{ws_id}/activity").json()
    assert body["items"][0]["summary"] == "edited the funder criteria"
    assert body["items"][0]["actor"] == "editor@ki.se"


def test_activity_tiebreak_is_deterministic(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    same = datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC)
    lo = uuid.UUID(int=1)
    hi = uuid.UUID(int=2)
    with pg_session_factory() as s:
        s.add(WorkspaceEvent(id=lo, workspace_id=ws_id, kind="created", actor_id=current_user.id, created_at=same))
        s.add(WorkspaceEvent(id=hi, workspace_id=ws_id, kind="reference.added", actor_id=current_user.id, created_at=same))
        s.commit()
    body = client_authed_owner.get(f"/api/workspaces/{ws_id}/activity").json()
    # equal created_at -> id DESC, so the larger uuid (hi) comes first
    assert body["items"][0]["id"] == str(hi)


def test_activity_non_member_403(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    # A workspace owned by a stranger: current_user is authed but is neither
    # owner nor a viewer, so require_member returns 403 (the workspace EXISTS,
    # so this is distinct from the 404 path).
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger@ki.se")
        other = make_workspace(s, owner=stranger, current_phase="active")
        s.commit()
        other_id = other.id
    r = client_authed_owner.get(f"/api/workspaces/{other_id}/activity")
    assert r.status_code == 403


def test_activity_limit_window_caps_at_100(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    base = datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC)
    with pg_session_factory() as s:
        # 105 non-collapsing events (only user_edit collapses) at distinct times.
        for i in range(105):
            s.add(
                WorkspaceEvent(
                    workspace_id=ws_id,
                    kind="created",
                    actor_id=current_user.id,
                    created_at=base + timedelta(seconds=i),
                )
            )
        s.commit()
    body = client_authed_owner.get(f"/api/workspaces/{ws_id}/activity").json()
    # Only the newest 100 raw events are considered; each renders one row.
    assert len(body["items"]) == 100


def test_activity_unknown_workspace_404(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    r = client_authed_owner.get(f"/api/workspaces/{uuid.uuid4()}/activity")
    assert r.status_code == 404


def test_activity_unauth_rejected(client_unauth, _app, current_user, pg_session_factory, tmp_path):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    r = client_unauth.get(f"/api/workspaces/{ws_id}/activity")
    assert r.status_code in (401, 403)
