from pycrdt import Doc, Text
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import WorkspaceViewer, YjsUpdate
from call_manager.proposal_assist.collab import protocol as P
from call_manager.proposal_assist.collab import state as collab_state
from tests.proposal_assist.conftest import make_user, make_workspace


def _cookie(app, user_id: int) -> str:
    signer: SessionSigner = app.state.session_signer
    return signer.make(user_id=user_id)


def _update_frame(text: str) -> bytes:
    d = Doc()
    d["body"] = Text()
    cap: list[bytes] = []
    sub = d.observe(lambda e: cap.append(e.update))
    d["body"] += text
    d.unobserve(sub)
    return P.create_update_message(cap[-1])


def test_unauthenticated_ws_is_rejected(_app, pg_session_factory):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="route-noauth@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wid = ws.id
    client = TestClient(_app)
    client.__enter__()
    try:
        import pytest

        url = f"/api/ws/workspaces/{wid}/draft"
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(url) as wsconn:
            wsconn.receive_bytes()
    finally:
        client.__exit__(None, None, None)


def test_owner_can_connect_and_persist_an_update(_app, pg_session_factory):
    from fastapi.testclient import TestClient

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="route-owner@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id
    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        with client.websocket_connect(f"/api/ws/workspaces/{wid}/draft") as wsconn:
            first = wsconn.receive_bytes()          # server SYNC_STEP1
            assert P.is_sync_step1(first)
            wsconn.send_bytes(_update_frame("owner edit"))
            # allow the server to apply+persist
        # after the context closes, the update is durable + actor-tagged
    finally:
        client.__exit__(None, None, None)
    with pg_session_factory() as s:
        rows = s.execute(select(YjsUpdate).where(YjsUpdate.workspace_id == wid)).scalars().all()
        assert len(rows) == 1
        assert rows[0].actor_id == oid
        # and the bytes reconstruct the edit
        d = Doc()
        d["body"] = Text()
        d.apply_update(rows[0].update_bytes)
        assert "owner edit" in str(d["body"])


def test_persist_failure_closes_socket_without_double_close_crash(
    _app, pg_session_factory, monkeypatch
):
    # Regression (Slice-2c): on a durable-write failure the route hits PersistError,
    # calls MANAGER.evict(key) — which since Slice-2c closes THIS socket — then closes
    # the websocket AGAIN. A 2nd close on a real Starlette socket raises RuntimeError;
    # the route must suppress it and end cleanly (close 1011), not crash the handler.
    import pytest
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from call_manager.proposal_assist.collab import store

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="route-persistfail@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id

    def _boom(*_a, **_k):
        raise RuntimeError("durable write failed")

    monkeypatch.setattr(store, "append_update", _boom)

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(
            f"/api/ws/workspaces/{wid}/draft"
        ) as wsconn:
            assert P.is_sync_step1(wsconn.receive_bytes())  # initial SYNC_STEP1
            wsconn.send_bytes(_update_frame("boom"))         # → persist raises → PersistError
            wsconn.receive_bytes()                            # server evicts + closes (1011)
        assert exc.value.code == 1011  # clean server-error close, no unhandled RuntimeError
    finally:
        client.__exit__(None, None, None)


def test_non_member_connection_is_rejected(_app, pg_session_factory):
    import pytest
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="route-owner2@ki.se")
        stranger = make_user(s, email="route-stranger@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wid, sid = ws.id, stranger.id
    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, sid))
    try:
        url = f"/api/ws/workspaces/{wid}/draft"
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(url) as wsconn:
            wsconn.receive_bytes()
    finally:
        client.__exit__(None, None, None)


def test_read_only_member_update_is_not_persisted(_app, pg_session_factory):
    # member on a SETUP-phase workspace is read-only for the draft
    from fastapi.testclient import TestClient

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="ro-owner@ki.se")
        member = make_user(s, email="ro-member@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="setup")
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=member.id))
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, mid = ws.id, member.id
    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, mid))
    try:
        with client.websocket_connect(f"/api/ws/workspaces/{wid}/draft") as wsconn:
            assert P.is_sync_step1(wsconn.receive_bytes())  # can still read
            wsconn.send_bytes(_update_frame("should be dropped"))
    finally:
        client.__exit__(None, None, None)
    with pg_session_factory() as s:
        rows = s.execute(select(YjsUpdate).where(YjsUpdate.workspace_id == wid)).scalars().all()
        assert rows == []  # read-only write was dropped


def test_invalid_artifact_is_rejected(_app, pg_session_factory):
    import pytest
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="bad-art@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wid, oid = ws.id, owner.id
    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        url = f"/api/ws/workspaces/{wid}/notes"
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(url) as wsconn:
            wsconn.receive_bytes()
    finally:
        client.__exit__(None, None, None)


def test_member_connecting_to_locked_spec_is_read_only(_app, pg_session_factory):
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from call_manager.db.models import WorkspaceViewer, YjsUpdate

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="ls-owner@ki.se")
        member = make_user(s, email="ls-member@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        ws.spec_locked_at = datetime.now(UTC)          # locked → members read-only on spec
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=member.id))
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="spec")
        s.commit()
        wid, mid = ws.id, member.id
    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, mid))
    try:
        with client.websocket_connect(f"/api/ws/workspaces/{wid}/spec") as wsconn:
            assert P.is_sync_step1(wsconn.receive_bytes())   # connect + read OK
            wsconn.send_bytes(_update_frame("should be dropped"))
    finally:
        client.__exit__(None, None, None)
    with pg_session_factory() as s:
        rows = s.execute(select(YjsUpdate).where(YjsUpdate.workspace_id == wid)).scalars().all()
        assert rows == []   # read-only member's write was dropped


def _recv_until_write(wsconn, tries: int = 10) -> bytes:
    """Read frames until a SYNC write (STEP2/UPDATE) arrives; the server may
    send a STEP1 first."""
    for _ in range(tries):
        frame = wsconn.receive_bytes()
        if P.is_sync_write(frame):
            return frame
    raise AssertionError("no SYNC write frame received")


def test_foreign_origin_is_rejected(_app, pg_session_factory):
    import pytest
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="origin-owner@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wid, oid = ws.id, owner.id
    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/api/ws/workspaces/{wid}/draft", headers={"origin": "http://evil.example"}
            ) as wsconn,
        ):
            wsconn.receive_bytes()
    finally:
        client.__exit__(None, None, None)


def test_oversized_frame_closes_the_connection(_app, pg_session_factory):
    import pytest
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="big-owner@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id
    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        with client.websocket_connect(f"/api/ws/workspaces/{wid}/draft") as wsconn:
            wsconn.receive_bytes()  # STEP1
            wsconn.send_bytes(b"\x00" * 1_000_001)  # over _MAX_FRAME_BYTES
            with pytest.raises(WebSocketDisconnect):
                wsconn.receive_bytes()
    finally:
        client.__exit__(None, None, None)


def test_two_connections_converge_through_the_route(_app, pg_session_factory):
    # Both connections use the SAME authenticated owner so they share one
    # TestClient event loop (two TestClients = two loops sharing an asyncio.Lock
    # = undefined). This proves the route's apply+broadcast path A->B; multi-user
    # authorization is covered by the ws_auth tests, and two-distinct-client
    # convergence by the socket-free room test.
    from fastapi.testclient import TestClient

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="conv-owner@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id
    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    url = f"/api/ws/workspaces/{wid}/draft"
    try:
        with client.websocket_connect(url) as a, client.websocket_connect(url) as b:
            a.receive_bytes()  # A's STEP1 (drain)
            b.receive_bytes()  # B's STEP1 (drain)
            a.send_bytes(_update_frame("shared edit"))
            frame = _recv_until_write(b)
            mirror = Doc()
            mirror["body"] = Text()
            mirror.apply_update(P.extract_update(frame))
            assert "shared edit" in str(mirror.get("body", type=Text))
    finally:
        client.__exit__(None, None, None)
