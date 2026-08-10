"""git -> room hydration. On the first open of a collab-enabled artifact whose
Yjs store is empty, seed the room from git HEAD markdown and persist that seed
as update #1 so the store is canonical thereafter."""

from __future__ import annotations

import uuid

from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.canonical import (  # shared, single source
    _EMPTY_DOC,  # noqa: F401  re-exported: external importers use hydrate._EMPTY_DOC
    build_doc,  # noqa: F401  re-exported: external importers use hydrate.build_doc
    markdown_to_pm_or_empty,
    read_head_markdown,
)
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex_live
from call_manager.proposal_assist.paths import workspace_repo_dir

# The hydration seed runs once per room, ever, so a modest bound is safe: on
# contention the caller's operation fails with the house busy semantics and the
# next open retries.
_HYDRATE_MUTEX_TIMEOUT_SECONDS = 5


def load_or_hydrate(
    session, *, workspace_id: uuid.UUID, artifact: str, workspaces_dir
) -> list[bytes]:
    updates = store.load_updates(session, workspace_id=workspace_id, artifact=artifact)
    if updates:
        return updates
    if not state.is_collab_enabled(session, workspace_id=workspace_id, artifact=artifact):
        return []
    md = read_head_markdown(workspace_repo_dir(workspaces_dir, workspace_id), artifact)
    if md is None:
        # Collab enabled but the artifact file has never been committed. Leave the
        # room empty (Slice-1 behavior) rather than persisting a synthetic seed.
        return []
    seed = pm_json_to_ydoc(markdown_to_pm_or_empty(md)).get_update()
    # B-retire: the seed is a DB write on a cold path, so take the SAME lock the
    # delete transaction takes (B4) before committing it. This is race-free, not
    # merely narrowing: acquire_commit_mutex_live re-reads soft_deleted_at under
    # the mutex, and deletion holds that mutex across its own commit. Cost is one
    # mutex acquisition on the FIRST open of a room ever (afterwards load_updates
    # returns rows and we never reach here) — the same shape maybe_auto_enable
    # already pays on the artifact read path.
    #
    # Do NOT catch WorkspaceDeleted / CommitMutexTimeout here. Returning [] would
    # be indistinguishable from "this artifact has no content yet" (the branch
    # above), and accept_via_kernel would then build an EMPTY room and apply a
    # section write to it. Both exceptions are already house contracts: kernel_accept
    # handles WorkspaceDeleted, and CommitMutexTimeout is the standard busy -> 429.
    acquire_commit_mutex_live(
        session, workspace_id, timeout_seconds=_HYDRATE_MUTEX_TIMEOUT_SECONDS
    )
    store.append_update(
        session, workspace_id=workspace_id, artifact=artifact, actor_id=None, update=seed
    )
    return [seed]
