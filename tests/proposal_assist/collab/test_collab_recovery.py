# tests/proposal_assist/collab/test_collab_recovery.py
from fastapi.testclient import TestClient
from pycrdt import Doc, Text

from call_manager.auth.session_cookie import SessionSigner
from call_manager.proposal_assist.collab import protocol as P
from call_manager.proposal_assist.collab.room import MANAGER
from tests.proposal_assist.conftest import make_user, make_workspace


def _cookie(app, uid):
    signer: SessionSigner = app.state.session_signer
    return signer.make(user_id=uid)


def _update_frame(text):
    d = Doc()
    d["body"] = Text()
    cap = []
    sub = d.observe(lambda e: cap.append(e.update))
    d["body"] += text
    d.unobserve(sub)
    return P.create_update_message(cap[-1])


def _recv_until_write(wsconn, tries: int = 10) -> bytes:
    for _ in range(tries):
        frame = wsconn.receive_bytes()
        if P.is_sync_write(frame):
            return frame
    raise AssertionError("no SYNC write frame received")


def test_state_survives_room_eviction_and_reconnect(_app, pg_session_factory):
    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="recover-owner@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        s.commit()
        wid, oid = ws.id, owner.id

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        with client.websocket_connect(f"/api/ws/workspaces/{wid}/draft") as wsconn:
            wsconn.receive_bytes()  # STEP1
            wsconn.send_bytes(_update_frame("durable edit"))
        # context exit -> last client leaves -> room evicted from MANAGER
        assert not MANAGER.has_room((wid, "draft"))

        # fresh connection: server must rebuild the doc from Postgres and
        # answer our STEP1 with a STEP2 carrying the recovered content.
        with client.websocket_connect(f"/api/ws/workspaces/{wid}/draft") as wsconn:
            wsconn.receive_bytes()  # server STEP1 (drain)
            mirror = Doc()
            mirror["body"] = Text()
            wsconn.send_bytes(P.create_sync_message(mirror))  # our STEP1
            reply = _recv_until_write(wsconn)                 # server STEP2
            mirror.apply_update(P.extract_update(reply))
            assert "durable edit" in str(mirror["body"])
    finally:
        client.__exit__(None, None, None)


def _canonical(node):
    """Normalise empty-attrs representation difference between markdown_to_pm and
    ydoc_to_pm_json: ydoc_to_pm_json emits ``attrs: {}`` on marks (matching
    y-prosemirror), while markdown_to_pm omits ``attrs`` for marks without
    attributes. Both are logically identical; _canonical drops empty attrs so the
    comparison is of logical content only."""
    if isinstance(node, list):
        return [_canonical(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key == "attrs":
            cleaned = {k: v for k, v in value.items() if v is not None}
            if cleaned:
                out["attrs"] = cleaned
            continue
        if key in ("marks", "content"):
            out[key] = [_canonical(c) for c in value]
            continue
        out[key] = _canonical(value)
    return out


def test_xmlfragment_doc_survives_recovery_via_the_bridge(_app, pg_session_factory):
    # Exercise the REAL Slice-0 bridge surface (the 'prosemirror' XmlFragment),
    # not a toy Text: seed a proposal doc, persist it through the route, evict
    # the room, reconnect, and confirm the recovered state reconstructs the
    # exact ProseMirror JSON.
    from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
    from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc, ydoc_to_pm_json

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="xml-recover@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        s.commit()
        wid, oid = ws.id, owner.id

    pm_json = markdown_to_pm("## Background\n\nHello **world**.\n")
    seed = pm_json_to_ydoc(pm_json)
    seed_frame = P.create_update_message(seed.get_update())

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    url = f"/api/ws/workspaces/{wid}/draft"
    try:
        with client.websocket_connect(url) as wsconn:
            wsconn.receive_bytes()       # STEP1
            wsconn.send_bytes(seed_frame)
        assert not MANAGER.has_room((wid, "draft"))  # evicted

        with client.websocket_connect(url) as wsconn:
            wsconn.receive_bytes()       # server STEP1 (drain)
            mirror = Doc()
            wsconn.send_bytes(P.create_sync_message(mirror))  # our STEP1
            reply = _recv_until_write(wsconn)                 # server STEP2 (recovered)
            mirror.apply_update(P.extract_update(reply))
            # _canonical normalises the attrs:{} representation difference:
            # ydoc_to_pm_json emits attrs:{} on attr-less marks (matching
            # y-prosemirror), while markdown_to_pm omits attrs for such marks.
            # This is a known documentation-level difference (see test_yjs_bridge.py).
            assert _canonical(ydoc_to_pm_json(mirror)) == _canonical(pm_json)
    finally:
        client.__exit__(None, None, None)
