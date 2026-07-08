"""TDD: Task 3 — DraftOut/SpecOut gain collab_enabled + can_write read-model fields.

Reuses _app / _seed / _client from tests/test_api_workspaces.py and _add_spec
from tests/test_api_spec.py (same production repo layout).
"""

from __future__ import annotations

from call_manager.proposal_assist.collab import state as collab_state
from tests.test_api_spec import _add_spec
from tests.test_api_workspaces import _app, _client, _seed


def test_draft_exposes_collab_enabled_and_can_write(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """The first read auto-enables collab (enable-on-touch, 3b-5d.D4); owner
    can_write True; member can_write True; the flag is sticky afterwards."""
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "Preamble.\n\n## A\n\nBody.\n"
    )

    # --- first read auto-enables (enable-on-touch, 3b-5d.D4), owner ---
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/draft")
    assert r.status_code == 200
    body = r.json()
    assert body["collab_enabled"] is True
    assert body["can_write"] is True  # owner always can_write

    # --- non-owner active member: can_edit must stay owner-only, can_write must be True ---
    rm = _client(app, member_id).get(f"/api/workspaces/{ws_id}/draft")
    assert rm.status_code == 200
    rm_body = rm.json()
    assert rm_body["can_edit"] is False  # unchanged owner-oriented semantics
    assert rm_body["can_write"] is True  # resolve_access: active phase → member can write

    # --- already enabled by the first read; enable_collab is a sticky no-op
    # and the flag stays True ---
    with pg_session_factory() as s:
        collab_state.enable_collab(s, workspace_id=ws_id, artifact="draft")
        s.commit()

    r2 = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/draft")
    assert r2.json()["collab_enabled"] is True


def test_spec_exposes_collab_enabled_and_can_write(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """SpecOut gets the same two fields; the first read auto-enables collab
    (enable-on-touch, 3b-5d.D4); unlocked active phase → member can_write True."""
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "draft preamble\n"
    )
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")

    # --- owner: first read auto-enables (enable-on-touch, 3b-5d.D4) ---
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/spec")
    assert r.status_code == 200
    body = r.json()
    assert body["collab_enabled"] is True
    assert body["can_write"] is True  # owner, unlocked

    # --- member: can_edit False (owner-only), can_write True (unlocked active → any member) ---
    rm = _client(app, member_id).get(f"/api/workspaces/{ws_id}/spec")
    assert rm.status_code == 200
    rm_body = rm.json()
    assert rm_body["can_edit"] is False
    assert rm_body["can_write"] is True

    # --- already enabled by the first read; enable_collab is a sticky no-op
    # and the flag stays True ---
    with pg_session_factory() as s:
        collab_state.enable_collab(s, workspace_id=ws_id, artifact="spec")
        s.commit()

    r2 = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/spec")
    assert r2.json()["collab_enabled"] is True


def test_spec_locked_makes_member_can_write_false(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """A LOCKED spec → a member gets can_write False (owner-only), while the owner
    keeps can_write True. Proves can_write is wired through resolve_access's
    lock branch, not hard-coded True."""
    from datetime import UTC, datetime

    from call_manager.db.models import Workspace

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "draft preamble\n"
    )
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        ws.spec_locked_at = datetime.now(UTC)
        ws.spec_locked_by = owner_id
        s.commit()

    rm = _client(app, member_id).get(f"/api/workspaces/{ws_id}/spec")
    assert rm.status_code == 200
    assert rm.json()["can_write"] is False  # locked spec → member cannot write

    ro = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/spec")
    assert ro.json()["can_write"] is True  # owner still can_write on a locked spec
