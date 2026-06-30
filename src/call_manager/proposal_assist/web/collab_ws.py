"""Collaborative-editing WebSocket sync endpoint (Slice 1).

One room per (workspace, artifact). Connection-time cookie auth + cached
write-permission; per-message write gating; Postgres-persisted, actor-tagged
update log; restart recovery by replay. No git, no awareness/presence yet.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, WebSocket
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from call_manager.proposal_assist.collab import store
from call_manager.proposal_assist.collab.room import MANAGER, PersistError
from call_manager.proposal_assist.collab.ws_auth import (
    WSAuthError,
    check_origin,
    resolve_access,
    resolve_ws_user,
)

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
            return resolve_access(s, user=user, workspace_id=workspace_id, artifact=artifact)

    is_member, can_write = await run_in_threadpool(_access)
    if not is_member:
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return

    await websocket.accept()

    key = (workspace_id, artifact)

    async def _load() -> list[bytes]:
        def _read():
            with session_factory() as s:
                return store.load_updates(s, workspace_id=workspace_id, artifact=artifact)

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

    room = await MANAGER.acquire(key, load=_load, client=websocket)
    try:
        await websocket.send_bytes(room.initial_message())
        async for message in websocket.iter_bytes():
            if len(message) > _MAX_FRAME_BYTES:
                await websocket.close(code=_WS_MESSAGE_TOO_BIG)
                break
            await room.handle(
                sender=websocket,
                user_id=user.id,
                can_write=can_write,
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
        await websocket.close(code=_WS_INTERNAL_ERROR)
    finally:
        await MANAGER.release(key, client=websocket)
