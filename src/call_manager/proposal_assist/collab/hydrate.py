"""git -> room hydration. On the first open of a collab-enabled artifact whose
Yjs store is empty, seed the room from git HEAD markdown and persist that seed
as update #1 so the store is canonical thereafter."""

from __future__ import annotations

import uuid
from pathlib import Path

from pycrdt import Doc

from call_manager.proposal_assist.artifacts import Artifact, filename_for
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from call_manager.proposal_assist.paths import workspace_repo_dir

_ARTIFACT = {"draft": Artifact.DRAFT, "spec": Artifact.SPEC}

# ProseMirror's ``doc`` node requires ``block+`` (at least one block child), so an
# empty / whitespace-only HEAD markdown has no valid PM representation. Seed such
# rooms from a single empty paragraph — the canonical empty document — which
# round-trips to a lone-newline markdown on checkpoint.
_EMPTY_DOC = {"type": "doc", "content": [{"type": "paragraph"}]}


def build_doc(updates: list[bytes]) -> Doc:
    doc = Doc()
    for u in updates:
        doc.apply_update(u)
    return doc


def _markdown_to_pm(md: str) -> dict:
    if not md.strip():
        return _EMPTY_DOC
    return markdown_to_pm(md)


def _read_head_markdown(workspaces_dir, workspace_id: uuid.UUID, artifact: str) -> str | None:
    """The artifact's HEAD markdown, or ``None`` when the file does not exist
    (no repo yet — nothing to hydrate from, so the room stays Slice-1 empty)."""
    repo = workspace_repo_dir(workspaces_dir, workspace_id)
    path = Path(repo) / filename_for(_ARTIFACT[artifact])
    return path.read_text() if path.exists() else None


def load_or_hydrate(
    session, *, workspace_id: uuid.UUID, artifact: str, workspaces_dir
) -> list[bytes]:
    updates = store.load_updates(session, workspace_id=workspace_id, artifact=artifact)
    if updates:
        return updates
    if not state.is_collab_enabled(session, workspace_id=workspace_id, artifact=artifact):
        return []
    md = _read_head_markdown(workspaces_dir, workspace_id, artifact)
    if md is None:
        # Collab enabled but the artifact file has never been committed. There is
        # no git HEAD to seed from, so leave the room empty (Slice-1 behavior)
        # rather than persisting a synthetic seed.
        return []
    seed = pm_json_to_ydoc(_markdown_to_pm(md)).get_update()
    store.append_update(
        session, workspace_id=workspace_id, artifact=artifact, actor_id=None, update=seed
    )
    return [seed]
