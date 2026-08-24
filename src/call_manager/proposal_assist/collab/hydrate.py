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
from call_manager.proposal_assist.fencing import acquire_commit_mutex_at_generation
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.ports.stamp import FenceStamp

# The hydration seed runs once per room, ever, so a modest bound is safe: on
# contention the caller's operation fails with the house busy semantics and the
# next open retries.
_HYDRATE_MUTEX_TIMEOUT_SECONDS = 5


def load_or_hydrate(
    session, *, stamp: FenceStamp, workspace_id: uuid.UUID, artifact: str, workspaces_dir
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
    # B-retire + endgame Task 5 (Primitive B): the seed is a DB write on a cold
    # path, so take the SAME lock the delete transaction takes (B4) before committing
    # it, now AT the caller's stamped generation. This is race-free, not merely
    # narrowing: acquire_commit_mutex_at_generation re-reads soft_deleted_at AND the
    # lifecycle_generation under the mutex, and deletion holds that mutex across its
    # own commit. The generation is threaded from the caller's access check (a route
    # membership check, or the WS connect authorization) — never re-read here at the
    # write. Cost is one mutex acquisition on the FIRST open of a room ever
    # (afterwards load_updates returns rows and we never reach here) — the same shape
    # maybe_auto_enable already pays on the artifact read path.
    #
    # Do NOT catch WorkspaceDeleted / WorkspaceGenerationStale / CommitMutexTimeout
    # here. Returning [] would be indistinguishable from "this artifact has no
    # content yet" (the branch above), and accept_via_kernel would then build an
    # EMPTY room and apply a section write to it. All three are already house
    # contracts: on the HTTP paths WorkspaceDeleted and WorkspaceGenerationStale are
    # a 404 (app-level handlers) and CommitMutexTimeout the standard busy -> 429. On
    # the WEBSOCKET path those handlers still RUN (Starlette dispatches them in the
    # websocket scope too), but all they can emit on an already-accepted socket is a
    # denial response the client cannot see or act on — so collab_ws maps them to a
    # close frame instead; see its except around MANAGER.acquire.
    acquire_commit_mutex_at_generation(
        session,
        stamp.workspace_id,
        generation=stamp.generation,
        timeout_seconds=_HYDRATE_MUTEX_TIMEOUT_SECONDS,
    )
    store.append_update(
        session, workspace_id=workspace_id, artifact=artifact, actor_id=None, update=seed
    )
    return [seed]
