"""Mid-session permission reconciliation for collab rooms (Slice 2c).

After a permission mutation commits, re-derive every connected client's access
from resolve_access (the same function connect uses) and apply the delta: flip
the per-client can_write in place, or close+deregister a client that lost
membership. DB-touching (unlike room.py) — it reaches MANAGER + resolve_access.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import uuid

from starlette.concurrency import run_in_threadpool

from call_manager.db.models import User, Workspace
from call_manager.proposal_assist.citations.gitio import GitCommitError
from call_manager.proposal_assist.collab.checkpoint import checkpoint_artifact
from call_manager.proposal_assist.collab.room import MANAGER
from call_manager.proposal_assist.collab.ws_auth import resolve_access
from call_manager.proposal_assist.drafting.locking import CommitMutexTimeout

logger = logging.getLogger(__name__)

_WS_POLICY_VIOLATION = 1008  # RFC 6455 — mirrors collab_ws.py; a revoked client is closed with it.
# RFC 6455 service-restart — a RECONNECTABLE close. Used for a self
# password-change: the acting browser re-mints its cookie and should reconnect
# (unlike logout/disable, which are terminal 1008). Public: imported by me.py.
WS_RECONNECTABLE_CLOSE = 1012
# Upper bound on how long an invalidation route blocks on the teardown barrier.
# Also a liveness bound: the barrier awaits release()->checkpoint run_in_threadpool,
# which competes with the route worker for the pool. See signal_user_revocation.
_REVOCATION_BARRIER_SECONDS = 5.0


def make_checkpoint_on_evict(session_factory, workspaces_dir, workspace_id, artifact):
    """Build the last-leave checkpoint callback (same behavior as collab_ws.py's
    inline _checkpoint_on_evict — extracted here so both the WS route and a
    live-workspace revoke share ONE implementation). Swallows the benign teardown
    errors; the durable log survives, so the next trigger retries."""
    async def _on_evict(_room) -> None:
        def _cp():
            with session_factory() as s:
                # Skip checkpointing a soft-deleted (gone) workspace. This keeps the
                # ws_deleted "no checkpoint" split (Codex High #2) robust EVEN WHEN the
                # WS route's own finally-release — which always passes this callback —
                # wins the last-leave race over refresh_permissions' drop_client.
                ws = s.get(Workspace, workspace_id)
                if ws is None or ws.soft_deleted_at is not None:
                    return
                try:
                    checkpoint_artifact(
                        s,
                        workspace_id=workspace_id,
                        artifact=artifact,
                        workspaces_dir=workspaces_dir,
                    )
                    s.commit()
                except (GitCommitError, CommitMutexTimeout):
                    logger.warning(
                        "collab.checkpoint_on_evict_skipped",
                        extra={"event": "collab_checkpoint_on_evict_skipped",
                               "room": str((workspace_id, artifact))},
                    )
                    s.rollback()
                except Exception:
                    logger.error(
                        "collab.checkpoint_on_evict_error",
                        extra={"event": "collab_checkpoint_on_evict_error",
                               "room": str((workspace_id, artifact))},
                        exc_info=True,
                    )
                    s.rollback()
        await run_in_threadpool(_cp)
    return _on_evict


def _resolve(session_factory, user_id: int, workspace_id: uuid.UUID,
             artifact: str) -> tuple[bool, bool, bool]:
    """Re-run resolve_access for one user (its own session; runs in a threadpool).
    Returns (is_member, can_write, ws_deleted). A removed viewer still has a User
    row → is_member=False with ws_deleted=False (live-workspace revoke → checkpoint);
    a soft-deleted workspace → ws_deleted=True (revoke without checkpoint). Codex High #2."""
    with session_factory() as s:
        ws = s.get(Workspace, workspace_id)
        ws_deleted = ws is None or ws.soft_deleted_at is not None
        u = s.get(User, user_id)
        if u is None or u.status != "active":
            # Fail closed on a logged-out/disabled account: a member whose
            # account is no longer active re-resolves as not-member, so a
            # permission reconcile closes+deregisters their live socket
            # (defense-in-depth beneath the prompt revocation sweep).
            return (False, False, ws_deleted)
        is_member, can_write = resolve_access(
            s, user=u, workspace_id=workspace_id, artifact=artifact
        )
        return (is_member, can_write, ws_deleted)


async def reconcile_client(session_factory, workspaces_dir, room, client, state, *,
                          workspace_id: uuid.UUID, artifact: str) -> None:
    """Re-resolve ONE client's access and apply the delta. Caller holds
    room.recon_lock. On a resolve error, fail CLOSED (downgrade to read-only) —
    never leave stale write access (Slice 3a d).

    ``room`` is currently unused in the function body but is part of the contract:
    the caller must hold ``room.recon_lock`` before invoking this function, and
    passing ``room`` explicitly makes that obligation visible at call sites — a
    caller cannot reconcile a client without naming the room whose lock it holds.
    """
    try:
        is_member, can_write, ws_deleted = await run_in_threadpool(
            _resolve, session_factory, state.user_id, workspace_id, artifact
        )
    except Exception:
        logger.warning(
            "collab.refresh_permissions_resolve_failed",
            extra={"event": "collab_refresh_resolve_failed", "room": str((workspace_id, artifact))},
            exc_info=True,
        )
        state.can_write = False   # fail-closed downgrade (Slice 3a d)
        return
    if not is_member:
        # Gate any in-flight frame from this client BEFORE closing: a write
        # already waiting on room._lock will see can_write=False under the lock
        # and drop (interlocks with the Task-1 under-lock recheck, Codex High #1).
        state.can_write = False
        with contextlib.suppress(Exception):
            await client.close(code=_WS_POLICY_VIOLATION)
        key = (workspace_id, artifact)
        if ws_deleted:
            # Workspace gone → no checkpoint (a checkpoint could error / is
            # pointless). Codex High #2.
            await MANAGER.drop_client(key, client)
        else:
            # Live workspace, member lost access → preserve the last-leave
            # checkpoint semantics of a normal disconnect. Codex High #2.
            on_evict = make_checkpoint_on_evict(
                session_factory, workspaces_dir, workspace_id, artifact
            )
            await MANAGER.release(key, client=client, on_evict=on_evict)
    else:
        state.can_write = can_write


async def revoke_user_sockets(session_factory, workspaces_dir, user_id: int, *,
                              close_code: int = _WS_POLICY_VIOLATION) -> int:
    """Tear down every live collab socket belonging to ``user_id`` (all rooms).

    For each match, mirror reconcile_client's not-member branch: flip
    can_write=False (the synchronous write barrier), close the socket, and
    PROACTIVELY deregister via MANAGER.release with a last-leave checkpoint.
    Deregistration — not the flag — is what makes revocation durable: a flipped
    can_write is reversible by a later reconcile (permissions.py else-branch),
    but a socket removed from room.clients can never be re-granted. Returns the
    count torn down. Runs on the event loop; caller (signal_user_revocation)
    bridges from the sync route. Snapshot rooms first so we hold no manager/room
    lock across the per-room awaits (keeps the recon_lock -> manager._lock ->
    room._lock order — release()/drop_client() take manager._lock)."""
    rooms = await MANAGER.snapshot_rooms()
    n = 0
    for room in rooms:
        workspace_id, artifact = room.key
        async with room.recon_lock:
            for client, state in list(room.clients.items()):
                if state.user_id != user_id:
                    continue
                state.can_write = False
                with contextlib.suppress(Exception):
                    await client.close(code=close_code)
                on_evict = make_checkpoint_on_evict(
                    session_factory, workspaces_dir, workspace_id, artifact
                )
                await MANAGER.release(room.key, client=client, on_evict=on_evict)
                n += 1
    return n


async def refresh_permissions(session_factory, workspaces_dir, *,
                              workspace_id: uuid.UUID, artifact: str) -> None:
    """Reconcile every client in room (workspace_id, artifact). No-op if the room
    isn't live. Serialized per-room via room.recon_lock so overlapping signals
    (e.g. unlock then lock) cannot interleave into a stale can_write (Slice 3a a)."""
    key = (workspace_id, artifact)
    room = MANAGER.get(key)
    if room is None:
        return
    async with room.recon_lock:
        for client, state in list(room.clients.items()):
            await reconcile_client(session_factory, workspaces_dir, room, client, state,
                                   workspace_id=workspace_id, artifact=artifact)


def signal_from_request(request, *, workspace_id: uuid.UUID, artifacts: list[str]) -> None:
    """Route-side convenience: fire signal_permission_change using the app's
    captured loop + session_factory + workspaces_dir. NEVER raises — a mutation
    route must not fail because reconciliation couldn't be scheduled (Codex Medium #1)."""
    try:
        loop = getattr(request.app.state, "app_loop", None)
        signal_permission_change(
            loop,
            request.app.state.session_factory,
            request.app.state.settings.workspaces_dir,
            workspace_id=workspace_id,
            artifacts=artifacts,
        )
    except Exception:
        logger.warning(
            "collab.signal_from_request_failed",
            extra={"event": "collab_signal_from_request_failed", "workspace_id": str(workspace_id)},
            exc_info=True,
        )


def _log_signal_result(fut) -> None:
    try:
        exc = fut.exception()
    except concurrent.futures.CancelledError:
        return  # a cancelled reconciliation is not an error to report
    if exc is not None:
        logger.warning("collab.signal_permission_change_failed", exc_info=exc)


def signal_user_revocation(request, user_id: int, *,
                           close_code: int = _WS_POLICY_VIOLATION) -> None:
    """Route-side (SYNC): schedule revoke_user_sockets on the app loop and BLOCK
    (bounded) until every matching socket is flipped+deregistered — the strong
    'no writes after the invalidation response' contract. NEVER raises into the
    route: a missing loop, a schedule failure, a timeout, or a coroutine error
    degrades to a logged warning (the DB epoch bump already invalidated future
    auth; the barrier is the live-socket guarantee, best-effort within its bound).

    The _REVOCATION_BARRIER_SECONDS timeout is a LIVENESS bound as well as a
    security-window bound: the teardown awaits release()->checkpoint via
    run_in_threadpool, so a barrier-blocked route worker competes with the
    checkpoint for the pool. On timeout the route returns and the teardown
    continues on the loop (degrade to eventual)."""
    try:
        loop = getattr(request.app.state, "app_loop", None)
        if loop is None:
            return
        session_factory = request.app.state.session_factory
        workspaces_dir = request.app.state.settings.workspaces_dir
        coro = revoke_user_sockets(
            session_factory, workspaces_dir, user_id, close_code=close_code
        )
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            coro.close()   # never-awaited coroutine → close it before re-raising
            raise
        try:
            fut.result(timeout=_REVOCATION_BARRIER_SECONDS)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "collab.revocation_barrier_timeout",
                extra={"event": "collab_revocation_barrier_timeout", "user_id": user_id},
            )   # teardown continues on the loop; degrade to eventual
    except Exception:
        logger.warning(
            "collab.signal_user_revocation_failed",
            extra={"event": "collab_signal_user_revocation_failed", "user_id": user_id},
            exc_info=True,
        )


def signal_permission_change(loop, session_factory, workspaces_dir, *,
                             workspace_id: uuid.UUID, artifacts: list[str]) -> list:
    """Fire-and-forget: schedule refresh_permissions on the app event loop for
    each artifact, from a SYNC route (threadpool thread). Returns the futures
    (prod ignores them; tests may await via asyncio.wrap_future). A missing/closed
    loop degrades to a logged no-op — reconciliation is best-effort and MUST NOT
    raise into the mutation route (Codex Medium #1)."""
    if loop is None:
        return []
    futures = []
    for artifact in artifacts:
        # Build the coroutine first so we can close() it if scheduling fails —
        # otherwise a closed/failed loop leaves it un-awaited ("coroutine was never
        # awaited" RuntimeWarning + leaked frame).
        coro = refresh_permissions(session_factory, workspaces_dir,
                                   workspace_id=workspace_id, artifact=artifact)
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            coro.close()
            logger.warning(
                "collab.signal_schedule_failed",
                extra={"event": "collab_signal_schedule_failed",
                       "room": str((workspace_id, artifact))},
                exc_info=True,
            )
            continue
        fut.add_done_callback(_log_signal_result)
        futures.append(fut)
    return futures
