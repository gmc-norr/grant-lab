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
        if u is None:
            return (False, False, ws_deleted)
        is_member, can_write = resolve_access(
            s, user=u, workspace_id=workspace_id, artifact=artifact
        )
        return (is_member, can_write, ws_deleted)


async def refresh_permissions(session_factory, workspaces_dir, *,
                              workspace_id: uuid.UUID, artifact: str) -> None:
    """Reconcile every client in room (workspace_id, artifact). No-op if the room
    isn't live (flag off in prod, or nobody connected)."""
    key = (workspace_id, artifact)
    room = MANAGER.get(key)
    if room is None:
        return
    # Snapshot before any close/deregister so mutation can't corrupt iteration.
    # All room.clients mutations run on THIS event loop, so the snapshot is atomic
    # (no await between get and list).
    for client, state in list(room.clients.items()):
        try:
            is_member, can_write, ws_deleted = await run_in_threadpool(
                _resolve, session_factory, state.user_id, workspace_id, artifact
            )
        except Exception:
            logger.warning(
                "collab.refresh_permissions_resolve_failed",
                extra={"event": "collab_refresh_resolve_failed", "room": str(key)},
                exc_info=True,
            )
            continue  # isolate per-client failure; leave this client as-is
        if not is_member:
            # Gate any in-flight frame from this client BEFORE closing: a write
            # already waiting on room._lock will see can_write=False under the lock
            # and drop (interlocks with the Task-1 under-lock recheck, Codex High #1).
            state.can_write = False
            with contextlib.suppress(Exception):
                await client.close(code=_WS_POLICY_VIOLATION)
            if ws_deleted:
                # Workspace gone → no checkpoint (a checkpoint could error / is
                # pointless). Codex High #2.
                await MANAGER.drop_client(key, client)
            else:
                # Live workspace, member lost access → preserve the last-leave
                # checkpoint semantics of a normal disconnect. Codex High #2.
                await MANAGER.release(
                    key,
                    client=client,
                    on_evict=make_checkpoint_on_evict(
                        session_factory, workspaces_dir, workspace_id, artifact
                    ),
                )
        else:
            state.can_write = can_write   # soft up/downgrade, in place


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
