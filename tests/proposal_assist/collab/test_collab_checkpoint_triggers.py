"""Task 5: checkpoint triggers — last-client-leaving + explicit HTTP endpoint.

Three groups:
  1. last-leave: a real bridge-built update sent over the WS, then disconnect,
     checkpoints (commit + ArtifactVersion + collab_checkpoint event) and evicts.
  2. explicit endpoint: POST .../collab/draft/checkpoint — owner 200 committed;
     read-only member 403; collab-disabled 409; non-member 404.
  3. flag OFF -> connection rejected (1008) before accept: no room, no yjs_updates
     row, no commit/ArtifactVersion (Codex High #1 "fully dormant" regression).
"""

import subprocess
from pathlib import Path

import pycrdt
import pytest
from fastapi.testclient import TestClient
from pycrdt import Doc
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import ArtifactVersion, WorkspaceEvent, WorkspaceViewer, YjsUpdate
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.collab import protocol as P
from call_manager.proposal_assist.collab import state as collab_state
from call_manager.proposal_assist.collab import store
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
from call_manager.proposal_assist.collab.room import MANAGER
from call_manager.proposal_assist.collab.yjs_bridge import (
    _FRAGMENT_KEY,
    _append_node,
    pm_json_to_ydoc,
)
from tests.proposal_assist.conftest import make_user, make_workspace


def _cookie(app, user_id: int) -> str:
    signer: SessionSigner = app.state.session_signer
    return signer.make(user_id=user_id)


def _init_repo(repo: Path, content: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _recv_sync_write(wsconn, tries: int = 10) -> bytes:
    """Read frames until a SYNC write (STEP2/UPDATE) arrives (the server may send
    a STEP1 first)."""
    for _ in range(tries):
        frame = wsconn.receive_bytes()
        if P.is_sync_write(frame):
            return frame
    raise AssertionError("no SYNC write frame received")


def _sync_and_append(wsconn, blocks: list[dict]) -> None:
    """Drive a faithful editing client over the socket: sync the server's seed
    into a local doc, then send ONE incremental update that appends `blocks` to
    the prosemirror fragment (an incremental edit on top of the synced state, not
    a conflicting from-scratch full-state update)."""
    client = Doc()
    wsconn.receive_bytes()  # drain the server's initial STEP1
    wsconn.send_bytes(P.create_sync_message(client))  # our STEP1 -> server replies STEP2
    step2 = _recv_sync_write(wsconn)
    P.handle_sync_message(step2[1:], client)  # apply the server's state (the seed)

    cap: list[bytes] = []
    sub = client.observe(lambda e: cap.append(e.update))
    frag = client.get(_FRAGMENT_KEY, type=pycrdt.XmlFragment)
    with client.transaction():
        for block in blocks:
            _append_node(frag, block)
    client.unobserve(sub)
    wsconn.send_bytes(P.create_update_message(cap[-1]))

    # Confirm the server has applied+persisted the update BEFORE we disconnect:
    # frames on one socket are processed in order, so receiving the STEP2 reply to
    # a STEP1 sent AFTER the update guarantees the update was already handled.
    wsconn.send_bytes(P.create_sync_message(client))
    _recv_sync_write(wsconn)


def _seed_bridge_update(pg_session_factory, wid, actor_id, md) -> Doc:
    """Persist a real bridge-built full-state update tagged with the author (the
    store-seeding path the explicit endpoint reads without a live room)."""
    doc = pm_json_to_ydoc(markdown_to_pm(md))
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=actor_id, update=doc.get_update()
        )
        s.commit()
    return doc


# --------------------------------------------------------------------------- #
# Group 1: last-client-leaving checkpoints, then evicts the room.
# --------------------------------------------------------------------------- #
def test_last_leave_checkpoints_and_evicts(_app, pg_session_factory, tmp_path):
    _app.state.session_factory = pg_session_factory
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        owner = make_user(s, email="trig-last-leave@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## Background\noriginal.\n")

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        with client.websocket_connect(f"/api/ws/workspaces/{wid}/draft") as wsconn:
            # A faithful editing client: sync the server's seed, then append a
            # real block incrementally (so it merges cleanly with the seed).
            _sync_and_append(
                wsconn,
                [
                    {
                        "type": "heading",
                        "attrs": {"level": 2},
                        "content": [{"type": "text", "text": "Extra"}],
                    },
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "rewritten by owner."}],
                    },
                ],
            )
            # closing the context disconnects -> last leave -> checkpoint-on-evict
    finally:
        client.__exit__(None, None, None)

    # the room is gone (evicted on last leave)
    assert MANAGER.get((wid, "draft")) is None

    # a commit landed on disk with the real edited content
    disk = (Path(tmp_path) / str(wid) / "repo" / PROPOSAL_FILENAME).read_text()
    assert "rewritten by owner." in disk

    with pg_session_factory() as s:
        av = (
            s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid))
            .scalars()
            .all()
        )
        assert len(av) == 1 and av[0].created_by == "user"
        ev = (
            s.execute(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wid,
                    WorkspaceEvent.kind == "collab_checkpoint",
                )
            )
            .scalars()
            .all()
        )
        assert len(ev) == 1 and ev[0].payload["authors"] == [oid]


# --------------------------------------------------------------------------- #
# Group 2: explicit "Save a version" HTTP endpoint.
# --------------------------------------------------------------------------- #
def test_explicit_endpoint_owner_commits(_app, pg_session_factory, tmp_path):
    _app.state.session_factory = pg_session_factory
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        owner = make_user(s, email="trig-explicit-owner@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## X\norig.\n")
    _seed_bridge_update(pg_session_factory, wid, oid, "## X\n\nedited by owner.\n")

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        r = client.post(f"/api/workspaces/{wid}/collab/draft/checkpoint")
    finally:
        client.__exit__(None, None, None)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["committed"] is True
    assert body["authors"] == [oid]

    with pg_session_factory() as s:
        av = (
            s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid))
            .scalars()
            .all()
        )
        assert len(av) == 1


def test_explicit_endpoint_read_only_member_is_forbidden(_app, pg_session_factory, tmp_path):
    _app.state.session_factory = pg_session_factory
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        owner = make_user(s, email="trig-ro-owner@ki.se")
        member = make_user(s, email="trig-ro-member@ki.se")
        # setup phase -> members are read-only for the draft
        ws = make_workspace(s, owner=owner, current_phase="setup")
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=member.id))
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, mid = ws.id, member.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## X\norig.\n")

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, mid))
    try:
        r = client.post(f"/api/workspaces/{wid}/collab/draft/checkpoint")
    finally:
        client.__exit__(None, None, None)
    assert r.status_code == 403, r.text


def test_explicit_endpoint_collab_disabled_is_conflict(_app, pg_session_factory, tmp_path):
    _app.state.session_factory = pg_session_factory
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        owner = make_user(s, email="trig-disabled-owner@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        # NOT enabling collab for the draft
        s.commit()
        wid, oid = ws.id, owner.id

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        r = client.post(f"/api/workspaces/{wid}/collab/draft/checkpoint")
    finally:
        client.__exit__(None, None, None)
    assert r.status_code == 409, r.text


def test_explicit_endpoint_non_member_is_not_found(_app, pg_session_factory, tmp_path):
    _app.state.session_factory = pg_session_factory
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        owner = make_user(s, email="trig-nm-owner@ki.se")
        stranger = make_user(s, email="trig-nm-stranger@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, sid = ws.id, stranger.id

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, sid))
    try:
        r = client.post(f"/api/workspaces/{wid}/collab/draft/checkpoint")
    finally:
        client.__exit__(None, None, None)
    assert r.status_code == 404, r.text


# --------------------------------------------------------------------------- #
# Group 3: flag OFF -> the WS is rejected before accept; fully dormant.
# --------------------------------------------------------------------------- #
def test_flag_off_connection_rejected_and_fully_dormant(_app, pg_session_factory, tmp_path):
    _app.state.session_factory = pg_session_factory
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        owner = make_user(s, email="trig-flagoff@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        # deliberately NOT calling enable_collab
        s.commit()
        wid, oid = ws.id, owner.id

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    try:
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(f"/api/ws/workspaces/{wid}/draft") as wsconn,
        ):
            wsconn.receive_bytes()  # closed with 1008 before accept
    finally:
        client.__exit__(None, None, None)

    # fully dormant: no room, no update rows, no commit/version
    assert MANAGER.get((wid, "draft")) is None
    with pg_session_factory() as s:
        rows = s.execute(select(YjsUpdate).where(YjsUpdate.workspace_id == wid)).scalars().all()
        assert rows == []
        av = (
            s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid))
            .scalars()
            .all()
        )
        assert av == []
