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
from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    current_head,
    write_and_commit_files,
)
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


def _head_or_empty(repo) -> str:
    """Current HEAD SHA, or ``""`` when the workspace has no committable git repo
    yet (missing repo dir / no commits). Lets a checkpoint of a repo-less but
    collab-enabled workspace resolve to a clean no-op instead of raising."""
    try:
        return current_head(repo)
    except GitCommitError:
        return ""


def checkpoint_artifact(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    artifact: str,
    workspaces_dir,
    extra_files: dict[str, str] | None = None,
) -> CheckpointResult:
    repo = workspace_repo_dir(workspaces_dir, workspace_id)

    # Acquire the per-workspace commit mutex FIRST, so the whole
    # read -> decide -> commit -> advance sequence below is serialized against
    # concurrent checkpoints AND the still-live git-first paths (save_section,
    # human_cite, agent tools). Reading the watermark/log BEFORE the mutex would
    # let a racing checkpoint commit and advance the watermark between our read
    # and our write — we could then rewrite git HEAD backwards and regress the
    # watermark. Released at the caller's COMMIT/ROLLBACK; we never commit here.
    acquire_commit_mutex(session, workspace_id)

    # ONE atomic snapshot of the log: doc, max_id, and authors all derive from
    # these rows (fixes the load-doc-then-read-max TOCTOU).
    rows = store.load_rows(session, workspace_id=workspace_id, artifact=artifact)
    watermark = state.get_checkpoint_watermark(
        session, workspace_id=workspace_id, artifact=artifact
    )
    max_id = rows[-1][0] if rows else 0
    head = _head_or_empty(repo)

    # Nothing new since the last checkpoint, or no git repo to commit into yet
    # (collab enabled before the workspace repo exists): clean no-op. Returning
    # here — rather than falling through to write_and_commit_files — keeps a
    # repo-less explicit checkpoint a 200/no-op instead of an unhandled 500.
    if max_id <= watermark or head == "":
        return CheckpointResult(committed=False, commit_sha=head, authors=[])

    authors = sorted({a for (rid, a, _) in rows if rid > watermark and a is not None})
    if not authors:
        # The only updates since the watermark are unauthored — i.e. the git-HEAD
        # hydration seed (actor_id=None). git already holds that content, so
        # committing it would merely re-serialize HEAD (dropping any non-schema
        # markup) and record a spurious system-authored version for a session
        # nobody edited. Advance past the seed WITHOUT committing, so a no-edit
        # open/close is a true no-op; the first real edit checkpoints the whole
        # doc, attributed to its author.
        state.advance_checkpoint(
            session,
            workspace_id=workspace_id,
            artifact=artifact,
            up_to_update_id=max_id,
            commit_sha=head,
        )
        return CheckpointResult(committed=False, commit_sha=head, authors=[])

    doc = build_doc([b for (_, _, b) in rows])
    md = serialize_doc_to_markdown(doc)
    fname = filename_for(_ARTIFACT[artifact])
    author_tag = ",".join(str(a) for a in authors)
    files = {fname: md}
    if extra_files:
        files.update(extra_files)
    commit_sha = write_and_commit_files(
        repo, files, f"collab checkpoint: {artifact} (authors:{author_tag})"
    )
    changed = commit_sha != head
    if changed:
        session.add(
            ArtifactVersion(
                workspace_id=workspace_id,
                commit_sha=commit_sha,
                created_by="user",  # authors is non-empty on this path
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
    # Compact the just-checkpointed prefix into the merged full-state (Slice 3b-1),
    # in THIS transaction/mutex. Reuse the lowest snapshot id so replay stays
    # merged-first; delete the other snapshotted ids explicitly. Guard >= 2 rows
    # (a single-row log has nothing to collapse and must not be pointlessly
    # rewritten). `doc` is the merged doc already built above.
    if len(rows) >= 2:
        # Preserve the most-recent author on the merged row so load_rows still
        # reflects the last author after compaction (transparency); the merged
        # row is <= the watermark, so this attribution never re-triggers authorship.
        merged_actor_id = next((a for (_, a, _) in reversed(rows) if a is not None), None)
        store.compact_snapshot(
            session,
            workspace_id=workspace_id,
            artifact=artifact,
            min_id=rows[0][0],
            merged_update=doc.get_update(),
            merged_actor_id=merged_actor_id,
            delete_ids=[rid for (rid, _, _) in rows[1:]],
        )
    return CheckpointResult(committed=changed, commit_sha=commit_sha, authors=authors)
