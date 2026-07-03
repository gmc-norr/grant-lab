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
from call_manager.proposal_assist.paths import workspace_repo_dir


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
    store.append_update(
        session, workspace_id=workspace_id, artifact=artifact, actor_id=None, update=seed
    )
    return [seed]
