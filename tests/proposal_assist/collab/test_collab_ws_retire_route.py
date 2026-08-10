"""A retired acquire surfaces as WS close 1008 (policy), not 1011/1006 (B3)."""

import pytest
from starlette.websockets import WebSocketDisconnect

from call_manager.auth.session_cookie import SessionSigner
from call_manager.proposal_assist.collab import state as collab_state
from call_manager.proposal_assist.collab.room import MANAGER
from tests.proposal_assist.conftest import make_user, make_workspace


def _cookie(app, user_id: int) -> str:
    signer: SessionSigner = app.state.session_signer
    return signer.make(user_id=user_id)


def test_retired_acquire_closes_with_1008(_app, pg_session_factory):
    """accept() happens at collab_ws.py:87 and the route's try opens at :118, so a
    raise from MANAGER.acquire would surface as 1011/1006. The route maps
    WorkspaceRetired to close(1008) + return before entering the try."""
    from fastapi.testclient import TestClient

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="ws-retire@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    # Force the in-process tombstone directly (deterministic; avoids threading the
    # async retire through the sync test client). MANAGER is the same module-global
    # the route uses. Discard in finally so it cannot leak into other tests sharing
    # the process-global MANAGER.
    MANAGER._retired_ws.add(wid)
    try:
        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(
            f"/api/ws/workspaces/{wid}/draft"
        ) as wsconn:
            wsconn.receive_bytes()
        assert exc.value.code == 1008      # NOT 1011, NOT 1006
    finally:
        MANAGER._retired_ws.discard(wid)
        client.__exit__(None, None, None)
