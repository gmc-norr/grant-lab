"""Collaborative-editing WebSocket sync endpoint (Slice 1).

One room per (workspace, artifact). Connection-time cookie auth + cached
write-permission; per-message write gating; Postgres-persisted, actor-tagged
update log; restart recovery by replay. No git, no awareness/presence yet.
"""

from __future__ import annotations

import contextlib
import logging
import uuid

from fastapi import APIRouter, WebSocket
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from call_manager.proposal_assist.collab import state as collab_state
from call_manager.proposal_assist.collab import store
from call_manager.proposal_assist.collab.hydrate import load_or_hydrate
from call_manager.proposal_assist.collab.permissions import make_checkpoint_on_evict
from call_manager.proposal_assist.collab.room import MANAGER, ClientState, PersistError
from call_manager.proposal_assist.collab.ws_auth import (
    WSAuthError,
    check_origin,
    resolve_access,
    resolve_ws_user,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_VALID_ARTIFACTS = {"draft", "spec"}
_WS_POLICY_VIOLATION = 1008  # RFC 6455: policy violation
_WS_MESSAGE_TOO_BIG = 1009  # RFC 6455: message too big
_WS_INTERNAL_ERROR = 1011  # RFC 6455: server error
# Cap a single inbound frame. A proposal doc is small; legitimate CRDT updates
# are far under this. HTTP body-size middleware does NOT protect WS frames, so
# bound them here to deny memory-exhaustion / abusive payloads.
_MAX_FRAME_BYTES = 1_000_000


@router.websocket("/api/ws/workspaces/{workspace_id}/{artifact}")
async def collab_ws(websocket: WebSocket, workspace_id: uuid.UUID, artifact: str) -> None:
    if artifact not in _VALID_ARTIFACTS:
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return

    # 0. CSWSH defense: reject cross-site Origin before doing anything else.
    if not check_origin(websocket):
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return

    # 1. Authenticate (before accept).
    try:
        user = await resolve_ws_user(websocket)
    except WSAuthError:
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return

    # 2. Authorize membership + resolve write permission (once, at connect).
    session_factory = websocket.app.state.session_factory

    def _access():
        with session_factory() as s:
            is_member, can_write = resolve_access(
                s, user=user, workspace_id=workspace_id, artifact=artifact
            )
            enabled = collab_state.is_collab_enabled(
                s, workspace_id=workspace_id, artifact=artifact
            )
            return is_member, can_write, enabled

    is_member, can_write, collab_enabled = await run_in_threadpool(_access)
    if not is_member:
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return
    if not collab_enabled:
        # Feature dormant for this artifact: no room, no writes, no yjs_updates rows.
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return

    await websocket.accept()

    key = (workspace_id, artifact)
    workspaces_dir = websocket.app.state.settings.workspaces_dir

    async def _load() -> list[bytes]:
        def _read():
            with session_factory() as s:
                updates = load_or_hydrate(
                    s, workspace_id=workspace_id, artifact=artifact, workspaces_dir=workspaces_dir
                )
                s.commit()  # persist the seed if hydration happened
                return updates

        return await run_in_threadpool(_read)

    async def _persist(actor_id: int, update: bytes) -> None:
        def _write():
            with session_factory() as s:
                store.append_update(
                    s,
                    workspace_id=workspace_id,
                    artifact=artifact,
                    actor_id=actor_id,
                    update=update,
                )

        await run_in_threadpool(_write)

    state = ClientState(user_id=user.id, can_write=can_write)
    room = await MANAGER.acquire(key, load=_load, client=websocket, state=state)
    try:
        async with room.lock:
            initial = room.initial_message()
        await websocket.send_bytes(initial)
        async for message in websocket.iter_bytes():
            if len(message) > _MAX_FRAME_BYTES:
                await websocket.close(code=_WS_MESSAGE_TOO_BIG)
                break
            await room.handle(
                sender=websocket,
                state=state,
                message=message,
                persist=_persist,
            )
    except WebSocketDisconnect:
        pass
    except PersistError:
        # Durable write failed: the in-memory doc is ahead of the log. Evict the
        # room so the next connection rebuilds from the log (the CRDT client
        # re-offers its update on reconnect). Close this socket as a server error.
        await MANAGER.evict(key)
        # evict() (Slice-2c) already closed THIS socket if it was still registered;
        # a second close on an already-closed Starlette WebSocket raises RuntimeError.
        # Suppress it — the close still runs (and matters) if evict found no room.
        with contextlib.suppress(Exception):
            await websocket.close(code=_WS_INTERNAL_ERROR)
    finally:
        await MANAGER.release(
            key,
            client=websocket,
            on_evict=make_checkpoint_on_evict(
                session_factory, workspaces_dir, workspace_id, artifact
            ),
        )
