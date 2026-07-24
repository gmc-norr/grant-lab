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

from call_manager.db.models import Workspace
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.checkpoint import checkpoint_artifact
from call_manager.proposal_assist.drafting.locking import WorkspaceDeleted

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
            try:
                res = checkpoint_artifact(
                    s,
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
            except WorkspaceDeleted:
                # Deleted between the pre-screen above and the mutex. Expected,
                # not a failure: there is nothing to hand over.
                s.rollback()
                continue
            except Exception:  # noqa: BLE001 — isolate per artifact; scan retries
                s.rollback()
                logger.error(
                    "collab.kill_switch_handover_failed",
                    extra={
                        "event": "collab_kill_switch_handover_failed",
                        "workspace_id": str(workspace_id),
                        "artifact": artifact,
                    },
                    exc_info=True,
                )
    return done
