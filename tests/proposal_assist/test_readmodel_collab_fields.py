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
    """collab_enabled is False by default; owner can_write True; member can_write True;
    after enable_collab the flag flips."""
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "Preamble.\n\n## A\n\nBody.\n"
    )

    # --- collab OFF by default, owner ---
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/draft")
    assert r.status_code == 200
    body = r.json()
    assert body["collab_enabled"] is False
    assert body["can_write"] is True  # owner always can_write

    # --- non-owner active member: can_edit must stay owner-only, can_write must be True ---
    rm = _client(app, member_id).get(f"/api/workspaces/{ws_id}/draft")
    assert rm.status_code == 200
    rm_body = rm.json()
    assert rm_body["can_edit"] is False  # unchanged owner-oriented semantics
    assert rm_body["can_write"] is True  # resolve_access: active phase → member can write

    # --- enable_collab flips collab_enabled True ---
    with pg_session_factory() as s:
        collab_state.enable_collab(s, workspace_id=ws_id, artifact="draft")
        s.commit()

    r2 = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/draft")
    assert r2.json()["collab_enabled"] is True


def test_spec_exposes_collab_enabled_and_can_write(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """SpecOut gets the same two fields; unlocked active phase → member can_write True."""
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _, ws_id = _seed(
        app, pg_session_factory, tmp_path, "draft preamble\n"
    )
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")

    # --- owner: defaults ---
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/spec")
    assert r.status_code == 200
    body = r.json()
    assert body["collab_enabled"] is False
    assert body["can_write"] is True  # owner, unlocked

    # --- member: can_edit False (owner-only), can_write True (unlocked active → any member) ---
    rm = _client(app, member_id).get(f"/api/workspaces/{ws_id}/spec")
    assert rm.status_code == 200
    rm_body = rm.json()
    assert rm_body["can_edit"] is False
    assert rm_body["can_write"] is True

    # --- enable_collab for spec flips its own flag ---
    with pg_session_factory() as s:
        collab_state.enable_collab(s, workspace_id=ws_id, artifact="spec")
        s.commit()

    r2 = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/spec")
    assert r2.json()["collab_enabled"] is True
