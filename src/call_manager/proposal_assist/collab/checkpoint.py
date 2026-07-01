"""room -> git checkpoint: serialize the live Doc to markdown, commit it, and
record provenance (ArtifactVersion + WorkspaceEvent), co-authored from the
per-update actor log since the last checkpoint watermark."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from pycrdt import Doc
from sqlalchemy.orm import Session

from call_manager.db.models import ArtifactVersion, WorkspaceEvent
from call_manager.proposal_assist.artifacts import Artifact, filename_for
from call_manager.proposal_assist.citations.gitio import current_head, write_and_commit_files
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.hydrate import build_doc
from call_manager.proposal_assist.collab.markdown_bridge import pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex
from call_manager.proposal_assist.paths import workspace_repo_dir

_ARTIFACT = {"draft": Artifact.DRAFT, "spec": Artifact.SPEC}


@dataclass
class CheckpointResult:
    committed: bool
    commit_sha: str
    authors: list[int]


def serialize_doc_to_markdown(doc: Doc) -> str:
    return pm_to_markdown(ydoc_to_pm_json(doc))


def checkpoint_artifact(
    session: Session, *, workspace_id: uuid.UUID, artifact: str, workspaces_dir
) -> CheckpointResult:
    repo = workspace_repo_dir(workspaces_dir, workspace_id)

    # ONE atomic snapshot of the log: doc, max_id, and authors all derive from
    # these rows. A concurrent append landing after this SELECT is excluded and
    # caught by the next checkpoint — the watermark never passes an unserialized
    # update (fixes the load-doc-then-read-max TOCTOU).
    rows = store.load_rows(session, workspace_id=workspace_id, artifact=artifact)
    watermark = state.get_checkpoint_watermark(
        session, workspace_id=workspace_id, artifact=artifact
    )
    max_id = rows[-1][0] if rows else 0

    if max_id <= watermark:
        return CheckpointResult(committed=False, commit_sha=current_head(repo), authors=[])

    authors = sorted({a for (rid, a, _) in rows if rid > watermark and a is not None})
    doc = build_doc([b for (_, _, b) in rows])
    md = serialize_doc_to_markdown(doc)
    fname = filename_for(_ARTIFACT[artifact])

    # Serialize against every other git mutation of this workspace (save_section,
    # human_cite, agent tools) — the git-first paths stay live until 2b. Released
    # at the caller's COMMIT/ROLLBACK; checkpoint_artifact never commits itself.
    acquire_commit_mutex(session, workspace_id)

    head_before = current_head(repo)
    author_tag = ",".join(str(a) for a in authors) or "system"
    commit_sha = write_and_commit_files(
        repo, {fname: md}, f"collab checkpoint: {artifact} (authors:{author_tag})"
    )
    changed = commit_sha != head_before
    if changed:
        session.add(
            ArtifactVersion(
                workspace_id=workspace_id,
                commit_sha=commit_sha,
                created_by="user" if authors else "system",
                message=f"collab checkpoint: {artifact}",
            )
        )
        session.add(
            WorkspaceEvent(
                workspace_id=workspace_id,
                kind="collab_checkpoint",
                actor_id=authors[0] if len(authors) == 1 else None,
                payload={"artifact": artifact, "commit_sha": commit_sha, "authors": authors},
            )
        )
    state.advance_checkpoint(
        session,
        workspace_id=workspace_id,
        artifact=artifact,
        up_to_update_id=max_id,
        commit_sha=commit_sha,
    )
    return CheckpointResult(committed=changed, commit_sha=commit_sha, authors=authors)
