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
from sqlalchemy.exc import OperationalError
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from grant_lab.db.models import Workspace
from grant_lab.errors import is_lock_timeout
from grant_lab.proposal_assist.collab import protocol as P
from grant_lab.proposal_assist.collab import state as collab_state
from grant_lab.proposal_assist.collab import store
from grant_lab.proposal_assist.collab.hydrate import load_or_hydrate
from grant_lab.proposal_assist.collab.permissions import (
    make_checkpoint_on_evict,
    reconcile_client,
)
from grant_lab.proposal_assist.collab.room import (
    MANAGER,
    WS_DOCUMENT_REPLACED,
    ClientState,
    PersistError,
)
from grant_lab.proposal_assist.collab.ws_auth import (
    WSAuthError,
    check_origin,
    resolve_access,
    resolve_ws_user,
)
from grant_lab.proposal_assist.drafting.contract import WorkspaceRetired
from grant_lab.proposal_assist.drafting.locking import (
    CommitMutexTimeout,
    WorkspaceDeleted,
)
from grant_lab.proposal_assist.fencing import WorkspaceGenerationStale
from grant_lab.proposal_assist.ports.stamp import FenceStamp, stamp_for_workspace

logger = logging.getLogger(__name__)

router = APIRouter()

_VALID_ARTIFACTS = {"draft", "spec"}
_WS_POLICY_VIOLATION = 1008  # RFC 6455: policy violation
_WS_MESSAGE_TOO_BIG = 1009  # RFC 6455: message too big
_WS_INTERNAL_ERROR = 1011  # RFC 6455: server error
# IANA-registered 1013 "Try Again Later": transient server-side contention, not a
# refusal. The client provider treats every code EXCEPT 1008/1009 as reconnect-
# with-backoff, which is exactly the retry this needs.
_WS_TRY_AGAIN_LATER = 1013
# Cap a single inbound frame. A proposal doc is small; legitimate CRDT updates
# are far under this. HTTP body-size middleware does NOT protect WS frames, so
# bound them here to deny memory-exhaustion / abusive payloads.
_MAX_FRAME_BYTES = 1_000_000


def _parse_client_epoch(raw: str | None) -> int | None:
    """`?epoch=` as the client presents it (spec 2026-09-02 doc-epoch §3.3).

    Absent → None (a fresh doc, which can merge nothing). A non-negative
    decimal integer → that value. Anything else → ValueError; the route closes
    1008 like any other malformed request."""
    if raw is None:
        return None
    if not (raw.isascii() and raw.isdigit()):
        raise ValueError(raw)
    return int(raw)


@router.websocket("/api/ws/workspaces/{workspace_id}/{artifact}")
async def collab_ws(websocket: WebSocket, workspace_id: uuid.UUID, artifact: str) -> None:
    if artifact not in _VALID_ARTIFACTS:
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return

    try:
        client_epoch = _parse_client_epoch(websocket.query_params.get("epoch"))
    except ValueError:
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

    def _access() -> tuple[bool, bool, bool, FenceStamp | None]:
        with session_factory() as s:
            is_member, can_write = resolve_access(
                s, user=user, workspace_id=workspace_id, artifact=artifact
            )
            enabled = collab_state.is_collab_enabled(
                s, workspace_id=workspace_id, artifact=artifact
            )
            # Mint the fence stamp HERE, at connect, from a bare-name workspace load
            # in the SAME transaction as resolve_access above. A long-lived socket
            # authorizes once at generation G and can still be sending edits after a
            # delete — so the hydrate write (load_or_hydrate) must re-validate THIS
            # captured generation under the mutex, in its own later session, never a
            # generation re-read at that write (endgame Task 5, framework §2b). Only
            # threaded to the hydrate path; the per-message _persist path takes no
            # commit mutex and is not a Group-D site. None when the workspace is gone
            # (is_member is already False on that path, so it is never used).
            ws = s.get(Workspace, workspace_id)
            stamp = stamp_for_workspace(ws) if ws is not None else None
            return is_member, can_write, enabled, stamp

    is_member, can_write, collab_enabled, stamp = await run_in_threadpool(_access)
    if not is_member or stamp is None:
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
                    s,
                    stamp=stamp,
                    workspace_id=workspace_id,
                    artifact=artifact,
                    workspaces_dir=workspaces_dir,
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
    try:
        room = await MANAGER.acquire(key, load=_load, client=websocket, state=state)
    except (WorkspaceRetired, WorkspaceDeleted):
        # The workspace was retired (soft-deleted) between the membership check and
        # room acquisition — or during cold-start hydration, where load_or_hydrate's
        # commit-mutex-at-generation guard raises WorkspaceDeleted. accept() already
        # happened, so a raise would surface as 1011/1006; close with 1008 (policy)
        # and return WITHOUT entering the try below (no client registered, nothing
        # to release). 1008 is TERMINAL on the client
        # (frontend/src/collab/collabProvider.ts:234) and that is correct here:
        # the artifact is gone, so coming back is pointless.
        with contextlib.suppress(Exception):
            await websocket.close(code=_WS_POLICY_VIOLATION)
        return
    except WorkspaceGenerationStale:
        # The workspace MOVED generation — a branch switch, or a delete-and-restore
        # — between this connect's authorization stamp and the mutex. Since spec
        # §4.4 this is reachable on every reconnect, not only a cold seed, because
        # load_or_hydrate now takes the mutex before reading the log.
        #
        # Retrying this socket's retained Y.Doc would merge the previous branch
        # into the new one. Tell the client to discard it and reconnect with a
        # fresh document. This stays distinct from transient mutex contention,
        # which uses 1013 below and safely retains the current Y.Doc.
        with contextlib.suppress(Exception):
            await websocket.close(code=WS_DOCUMENT_REPLACED)
        return
    except OperationalError as exc:
        # Slice C3: load_or_hydrate persists the seed INSIDE the mutex-held tail
        # (hydrate.py:55-60), and that INSERT takes an FK parent lock on
        # `workspaces` — so with the tail bounded a concurrent holder of that row
        # surfaces here as a raw 55P03. Same animal as CommitMutexTimeout below:
        # contention on a cold room, not a refusal. main.py's handler cannot help:
        # Starlette DOES run registered handlers in the WebSocket scope, but all it
        # can emit there is a denial response on an already-accepted socket — not a
        # status the browser client can see, let alone retry on. So map it to the
        # same 1013. A non-lock OperationalError is a real database failure and
        # keeps its existing propagation into that handler.
        if not is_lock_timeout(exc):
            raise
        with contextlib.suppress(Exception):
            await websocket.close(code=_WS_TRY_AGAIN_LATER)
        return
    except CommitMutexTimeout:
        # The OTHER exception the same hydration guard can raise, and a different
        # animal: contention, not refusal. It is reachable on every room open on the
        # cold-start path (load_or_hydrate now takes the live commit mutex before
        # reading the log) and it means "someone else holds the workspace mutex right
        # now" —
        # the artifact still exists and the client SHOULD come back. On HTTP this is
        # the house 429. On a WebSocket main.py's handler still RUNS — Starlette
        # dispatches registered handlers in the websocket scope exactly as in the HTTP
        # one — but all it can emit on an already-accepted socket is a denial response
        # the browser client cannot see or retry on. So map it to 1013 (try again
        # later), which the client provider retries with backoff. Falling through to
        # the route's own `raise` would give 1011 + a traceback for a routine,
        # expected collision.
        with contextlib.suppress(Exception):
            await websocket.close(code=_WS_TRY_AGAIN_LATER)
        return
    try:
        # Document epoch (spec 2026-09-02-collab-doc-epoch-design.md §3.3): a
        # client that presents an epoch holds a Y.Doc that synced against THAT
        # document. If the room is on a different one — a branch switch or the
        # kill-switch handover truncated the log while this client was in
        # reconnect backoff and never saw the 4409 — its STEP2 would merge the
        # old document into the new one (review 2026-09-02 §2.1, reproduced).
        # Refuse BEFORE STEP1, so no STEP2 is ever solicited. Inside this try so
        # `finally` deregisters the client. 4409 makes the provider discard the
        # doc and the hook build a fresh pair, which connects with no epoch.
        if client_epoch is not None and client_epoch != room.doc_epoch:
            logger.info(
                "collab.stale_doc_refused",
                extra={
                    "event": "collab_stale_doc_refused",
                    "workspace_id": str(workspace_id),
                    "artifact": artifact,
                    "client_epoch": client_epoch,
                    "room_epoch": room.doc_epoch,
                },
            )
            with contextlib.suppress(Exception):
                await websocket.close(code=WS_DOCUMENT_REPLACED)
            return
        # Close the connect-straddle TOCTOU (Slice 3a b): a permission mutation that
        # committed while we were mid-acquire may have fired its refresh BEFORE this
        # client was registered. Re-resolve THIS client now, under the recon lock so
        # ordering vs a concurrent refresh is well-defined (either the refresh saw
        # us, or we see the committed mutation here). Inside the try so `finally`
        # still releases the client if this raises.
        async with room.recon_lock:
            await reconcile_client(
                session_factory,
                workspaces_dir,
                room,
                websocket,
                state,
                workspace_id=workspace_id,
                artifact=artifact,
            )
            # Straddle epoch/status re-validation (session-revocation #1): a session
            # invalidation (logout / admin-disable / epoch bump) may have committed
            # AFTER handshake auth but BEFORE this client registered — so the
            # revocation sweep missed it, and reconcile_client (membership / write /
            # account-status, NOT epoch) would keep it. Re-run the full cookie
            # validation now, still under recon_lock, so a stale-epoch socket is
            # closed+dropped before the receive loop and can never send a write frame.
            if websocket in room.clients:
                try:
                    await resolve_ws_user(websocket)
                except WSAuthError:
                    with contextlib.suppress(Exception):
                        await websocket.close(code=_WS_POLICY_VIOLATION)
                    await MANAGER.drop_client(key, websocket)
        if websocket not in room.clients:
            return  # reconcile OR straddle re-validation revoked + deregistered this client
        # Teach the epoch BEFORE the STEP1 so a client that drops mid-handshake
        # still presents the right value next time; an old client ignores the
        # unknown type (spec §5).
        await websocket.send_bytes(P.create_doc_epoch_message(room.doc_epoch))
        async with room.lock:
            initial = room.initial_message()
        await websocket.send_bytes(initial)
        snapshot = await room.awareness_snapshot()
        if snapshot is not None:
            await websocket.send_bytes(snapshot)
        while True:
            # Drive `receive()` rather than `iter_bytes()`. `iter_bytes` reads
            # `message["bytes"]` unconditionally, so a single TEXT frame —
            # `send_text("hello")` — raised KeyError('bytes') straight out of
            # this handler: a traceback plus a 1011/1006 self-disconnect, for a
            # frame the sync protocol has no use for at all (review 2026-10-26,
            # appendix A6). Nothing we speak is text, so drop it and stay
            # connected; a disconnect still just ends the loop, which is exactly
            # what `iter_bytes` did (it swallows WebSocketDisconnect itself).
            frame = await websocket.receive()
            if frame["type"] == "websocket.disconnect":
                break
            message = frame.get("bytes")
            if message is None:
                continue
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
