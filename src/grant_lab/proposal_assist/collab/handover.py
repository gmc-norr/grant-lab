"""Kill-switch handover (3b-5d.D4 §3.2): make git the SOLE canonical store.

Runs at app startup (lifespan) when COLLAB_KILL_SWITCH is engaged, and again
from the periodic scan tick while it stays engaged (retry for artifacts whose
earlier pass failed). Per artifact with ANY log rows: checkpoint the full log
to git (record_version=False), then truncate the log and reset the watermark —
drain and truncate in ONE transaction, so a truncate can never be visible
without its checkpoint. After the pass, nothing can overwrite git-first writes
(no log left to checkpoint from), and lifting the switch re-hydrates empty
stores from CURRENT git HEAD. Pure sync DB+git — never touches the asyncio
room MANAGER (no rooms exist while the switch refuses connects)."""

from __future__ import annotations

import logging

from grant_lab.db.models import Workspace
from grant_lab.proposal_assist.collab import state, store
from grant_lab.proposal_assist.collab.checkpoint import checkpoint_artifact
from grant_lab.proposal_assist.drafting.locking import (
    CommitMutexTimeout,
    WorkspaceDeleted,
    is_lock_timeout,
)
from grant_lab.proposal_assist.fencing import WorkspaceGenerationStale
from grant_lab.proposal_assist.ports.stamp import stamp_for_workspace

logger = logging.getLogger(__name__)


def run_kill_switch_handover(session_factory, *, workspaces_dir) -> int:
    """Returns the number of artifacts fully handed over (drained + truncated)."""
    with session_factory() as s:
        candidates = store.find_artifacts_with_updates(s)
    done = 0
    for workspace_id, artifact in candidates:
        with session_factory() as s:
            ws = s.get(Workspace, workspace_id)
            if ws is None or ws.soft_deleted_at is not None:
                continue
            # Mint the fence stamp from the liveness pre-screen above (this pass's
            # access check): checkpoint_artifact re-validates it under the mutex, so
            # a workspace deleted or bumped between here and the mutex raises
            # WorkspaceDeleted / WorkspaceGenerationStale, both benign below.
            stamp = stamp_for_workspace(ws)
            try:
                res = checkpoint_artifact(
                    s,
                    stamp=stamp,
                    workspace_id=workspace_id,
                    artifact=artifact,
                    workspaces_dir=workspaces_dir,
                    record_version=False,
                )
                if res.commit_sha == "":
                    # No git repo — nowhere to hand the content over to. Keep
                    # the log (data preservation beats pass completion); the
                    # scan retries once a repo exists.
                    raise RuntimeError("no git repo to hand the collab log over to")
                store.truncate_log(s, workspace_id=workspace_id, artifact=artifact)
                state.reset_checkpoint(s, workspace_id=workspace_id, artifact=artifact)
                s.commit()  # checkpoint rows + truncate + reset: ONE transaction
                done += 1
            except Exception as exc:  # noqa: BLE001 — isolate per artifact; scan retries
                # This pass reaches the commit mutex INDIRECTLY, through
                # checkpoint_artifact. Since Slice C3 bounded the mutex-held
                # tail, routine row contention RAISES OperationalError(55P03)
                # where it used to hang — the same benign "someone else is
                # writing; the log survives untruncated and the next scan tick
                # retries" outcome as a mutex ACQUISITION timeout, or as a
                # workspace deleted (or deleted-and-restored past the stamped
                # generation) between the pre-screen above and the mutex.
                # None of those is a fault, so none earns an ERROR + traceback
                # on a path the scheduler re-runs while the switch stays engaged
                # — or, with COLLAB_CHECKPOINT_SCAN_SECONDS=0, on the one-shot
                # startup pass that is then the only attempt (scheduler.py adds
                # no periodic job at 0; see config.py's description of that
                # setting).
                #
                # ONE clause rather than sibling `except WorkspaceDeleted` /
                # `except OperationalError` arms: a re-raise from a sibling
                # clause escapes the try entirely instead of falling through to
                # `except Exception`, which would change what a REAL database
                # failure does here. Mirrors permissions.make_checkpoint_on_evict.
                #
                # GitCommitError is deliberately NOT benign on this path: the
                # whole point of the handover is getting the log INTO git, so a
                # commit that fails is a real fault — as is the repo-less
                # RuntimeError raised above.
                benign = isinstance(
                    exc, (CommitMutexTimeout, WorkspaceDeleted, WorkspaceGenerationStale)
                ) or is_lock_timeout(exc)
                if benign:
                    logger.warning(
                        "collab.kill_switch_handover_skipped",
                        extra={
                            "event": "collab_kill_switch_handover_skipped",
                            "workspace_id": str(workspace_id),
                            "artifact": artifact,
                            "reason": type(exc).__name__,
                        },
                    )
                else:
                    logger.error(
                        "collab.kill_switch_handover_failed",
                        extra={
                            "event": "collab_kill_switch_handover_failed",
                            "workspace_id": str(workspace_id),
                            "artifact": artifact,
                        },
                        exc_info=True,
                    )
                # Outside the branch: a rollback is required either way to end
                # this aborted transaction before this session (opened fresh per
                # candidate, inside the `for` loop above) closes and returns its
                # connection to the pool.
                s.rollback()
    return done
