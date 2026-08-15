"""Close-code mapping for the two exceptions MANAGER.acquire can now raise:
a retired acquire is 1008 (policy, do not retry), and the commit-mutex timeout
load_or_hydrate raises on a first-ever room open is 1013 (try again later) —
neither is 1011/1006 (B3 + post-merge review)."""

import pytest
from starlette.websockets import WebSocketDisconnect

from call_manager.auth.session_cookie import SessionSigner
from call_manager.proposal_assist.collab import state as collab_state
from call_manager.proposal_assist.collab.room import MANAGER
from call_manager.proposal_assist.web import collab_ws as collab_ws_module
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


def test_hydration_mutex_timeout_closes_with_1013(_app, pg_session_factory, monkeypatch):
    """B-retire made load_or_hydrate take the LIVE commit mutex to persist the seed,
    so acquire() gained a SECOND raise the route never handled: CommitMutexTimeout
    fell through to the route's own `raise` and became 1011 + a traceback for what is
    ordinary contention on a cold room. 1013 (try again later) is not in the client
    provider's no-reconnect set {1008, 1009}, so the editor retries with backoff."""
    from fastapi.testclient import TestClient

    from call_manager.proposal_assist.drafting.locking import CommitMutexTimeout

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="ws-mutex@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id

    def _boom(*a, **k):
        raise CommitMutexTimeout(str(wid))

    # The route holds a module-level reference, so patch it there. A fresh workspace
    # id means the room is cold, so acquire() really does call load().
    monkeypatch.setattr(collab_ws_module, "load_or_hydrate", _boom)

    client = TestClient(_app)
    client.__enter__()
    try:
        client.cookies.set("session", _cookie(_app, oid))
        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(
            f"/api/ws/workspaces/{wid}/draft"
        ) as wsconn:
            wsconn.receive_bytes()
        assert exc.value.code == 1013      # NOT 1011, NOT 1008 (retry is correct here)
    finally:
        client.__exit__(None, None, None)


def test_a_bounded_hydration_lock_timeout_closes_with_1013_not_1011(
    _app, pg_session_factory, monkeypatch
):
    """The 1013 mapping above covers CommitMutexTimeout, raised while ACQUIRING the
    mutex. Slice C3 bounded the tail AFTER it, so the same cold-room hydration can
    now fail with a raw OperationalError (55P03) from the seed's INSERT. That type
    is not in the route's except list, so without this arm it would be 1011 plus a
    traceback for ordinary contention — and unlike every HTTP site, main.py's
    handler cannot reach a WebSocket.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import OperationalError

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="ws-tailbound@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id

    class _Orig(Exception):
        sqlstate = "55P03"

    def _boom(*a, **k):
        raise OperationalError("INSERT INTO yjs_updates", {}, _Orig())

    monkeypatch.setattr(collab_ws_module, "load_or_hydrate", _boom)

    client = TestClient(_app)
    client.__enter__()
    try:
        client.cookies.set("session", _cookie(_app, oid))
        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(
            f"/api/ws/workspaces/{wid}/draft"
        ) as wsconn:
            wsconn.receive_bytes()
        assert exc.value.code == 1013      # NOT 1011
    finally:
        client.__exit__(None, None, None)


def test_a_non_lock_operational_error_keeps_its_existing_propagation(
    _app, pg_session_factory, monkeypatch
):
    """The arm must not swallow real database failures. 08006 is connection_failure.

    It re-raises, so the outcome must stay byte-identical to having no arm at all —
    which is NOT a 1011 close, as this test first assumed. Starlette runs registered
    exception handlers in the WEBSOCKET scope too, so main.py's OperationalError
    handler fires and emits its 500 `internal_error` envelope as a denial response.
    That is exactly why the 55P03 case above cannot lean on that handler: a denial
    response on an ALREADY-ACCEPTED socket is not a signal the browser client can
    retry on (and is an ASGI protocol violation on a real server), so contention
    needs the explicit 1013 close. Asserted as the concrete observed behaviour
    rather than "not 1013", so a future change to that propagation is visible here.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import OperationalError
    from starlette.testclient import WebSocketDenialResponse

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="ws-tailbound-real@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id

    class _Orig(Exception):
        sqlstate = "08006"

    def _boom(*a, **k):
        raise OperationalError("SELECT 1", {}, _Orig())

    monkeypatch.setattr(collab_ws_module, "load_or_hydrate", _boom)

    client = TestClient(_app)
    client.__enter__()
    try:
        client.cookies.set("session", _cookie(_app, oid))
        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(
            f"/api/ws/workspaces/{wid}/draft"
        ) as wsconn:
            wsconn.receive_bytes()
        assert isinstance(exc.value, WebSocketDenialResponse), (
            f"a real database failure must keep its existing propagation, not be "
            f"absorbed by the arm; got {type(exc.value).__name__} "
            f"code={getattr(exc.value, 'code', None)!r}"
        )
        assert exc.value.status_code == 500                 # NOT the 429 of a 55P03
        assert exc.value.json()["error"]["code"] == "internal_error"
    finally:
        client.__exit__(None, None, None)
