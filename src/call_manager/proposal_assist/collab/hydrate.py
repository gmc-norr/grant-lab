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

# Bounds the wait on EVERY room open, not only the first one ever: since spec
# §4.4 the mutex is taken before the log read so a mid-switch reconnect blocks
# rather than republishing the pre-switch room. On contention the caller's
# operation fails with the house busy semantics (429 on HTTP, a 1013 close frame
# on the websocket) and the client retries.
_HYDRATE_MUTEX_TIMEOUT_SECONDS = 5


def load_or_hydrate(
    session, *, stamp: FenceStamp, workspace_id: uuid.UUID, artifact: str, workspaces_dir
) -> list[bytes]:
    # SPEC §4.4 — the mutex comes BEFORE the read, not before the seed write.
    #
    # This used to guard only the cold seed path, below. That left the WARM path
    # (the log already has rows) taking no lock at all, and a branch switch has a
    # window — rooms torn down, log not yet truncated — in which a reconnect on
    # that path reads the OLD branch's rows and the manager publishes a room from
    # them that the switch has already torn down and will not tear down again.
    # The switch then truncates underneath it and the workspace serves the old
    # branch's text under the new branch's name.
    #
    # Cost, accepted knowingly: every room open now pays one per-workspace mutex
    # acquisition rather than only the first open ever, and CommitMutexTimeout
    # becomes reachable on an ordinary reconnect. Room open is a connect-time
    # path, not a per-keystroke one. The handling is unchanged and is described
    # in the comment below.
    #
    # Do NOT catch WorkspaceDeleted / WorkspaceGenerationStale / CommitMutexTimeout
    # here. Returning [] would be indistinguishable from "this artifact has no
    # content yet", and accept_via_kernel would then build an EMPTY room and apply
    # a section write to it. All three are already house contracts: on the HTTP
    # paths WorkspaceDeleted and WorkspaceGenerationStale are a 404 (app-level
    # handlers) and CommitMutexTimeout the standard busy -> 429. On the WEBSOCKET
    # path those handlers still RUN (Starlette dispatches them in the websocket
    # scope too), but all they can emit on an already-accepted socket is a denial
    # response the client cannot see or act on — so collab_ws maps them to a close
    # frame instead; see its except around MANAGER.acquire.
    acquire_commit_mutex_at_generation(
        session,
        stamp.workspace_id,
        generation=stamp.generation,
        timeout_seconds=_HYDRATE_MUTEX_TIMEOUT_SECONDS,
    )
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
    store.append_update(
        session, workspace_id=workspace_id, artifact=artifact, actor_id=None, update=seed
    )
    return [seed]
