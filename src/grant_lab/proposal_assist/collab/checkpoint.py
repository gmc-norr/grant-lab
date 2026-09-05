"""room -> git checkpoint: serialize the live Doc to markdown, commit it, and
record provenance (ArtifactVersion + WorkspaceEvent), co-authored from the
per-update actor log since the last checkpoint watermark."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from pycrdt import Doc
from sqlalchemy.orm import Session

from grant_lab.db.models import ArtifactVersion, Workspace, WorkspaceEvent
from grant_lab.proposal_assist.analysis.queue import enqueue_uncovered_sections
from grant_lab.proposal_assist.artifacts import Artifact, filename_for
from grant_lab.proposal_assist.citations.gitio import (
    GitCommitError,
    current_head,
    write_and_commit_files,
)
from grant_lab.proposal_assist.collab import state, store
from grant_lab.proposal_assist.collab.canonical import markdown_to_pm_or_empty
from grant_lab.proposal_assist.collab.hydrate import build_doc
from grant_lab.proposal_assist.collab.markdown_bridge import pm_to_markdown
from grant_lab.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json
from grant_lab.proposal_assist.drafting.locking import (
    WorkspaceDeleted,
    is_lock_timeout,
)
from grant_lab.proposal_assist.fencing import (
    WorkspaceGenerationStale,
    acquire_commit_mutex_at_generation,
)
from grant_lab.proposal_assist.paths import workspace_repo_dir
from grant_lab.proposal_assist.ports.stamp import FenceStamp, stamp_for_workspace

logger = logging.getLogger(__name__)

_ARTIFACT = {"draft": Artifact.DRAFT, "spec": Artifact.SPEC}


@dataclass
class CheckpointResult:
    committed: bool
    commit_sha: str
    authors: list[int]


def serialize_doc_to_markdown(doc: Doc) -> str:
    return pm_to_markdown(ydoc_to_pm_json(doc))


class CheckpointProjectionUnreadable(RuntimeError):
    """The serializer emitted markdown that the reader (``markdown_to_pm``)
    refuses. Committing it would brick the artifact: the next hydration raises
    on the committed bytes, every GET of the draft answers 500 and the git-first
    save path answers 409 (review 2026-09-02 §2.3 — an empty blockquote did
    exactly this). Refusing keeps the durable log and the live room intact; the
    next trigger retries once the document has changed. A serializer defect, so
    it is logged at ERROR with the reader's message."""


def _refuse_unreadable_projection(md: str, *, workspace_id: uuid.UUID, artifact: str) -> None:
    """Dry-run the reader on what is about to be committed (review §2.3). The
    parser is 60 lines from the serializer and would have refused every shape
    that bricked an artifact; this makes it refuse at COMMIT time, before the
    bytes exist anywhere but this call."""
    try:
        markdown_to_pm_or_empty(md)
    except ValueError as exc:
        logger.error(
            "collab.checkpoint_projection_unreadable",
            extra={
                "event": "collab_checkpoint_projection_unreadable",
                "workspace_id": str(workspace_id),
                "artifact": artifact,
                "error": str(exc),
            },
        )
        raise CheckpointProjectionUnreadable(f"{artifact}: {exc}") from exc


def _reconcile_head_for_checkpoint(session, *, workspace_id, repo) -> None:
    """Make git HEAD agree with `workspaces.current_branch` before a checkpoint
    reads it. Caller must already hold the commit mutex.

    Deliberately forgiving: a workspace with no repo, no commits, or a detached
    HEAD is left alone rather than raising. A checkpoint is not the place to fail
    a request over repo shape — `_head_or_empty` below already resolves those to
    a clean no-op, and the branch operations (which DO raise) are where a broken
    repo should surface.
    """
    from grant_lab.db.models import Workspace
    from grant_lab.proposal_assist.citations.gitio import checkout_branch, current_branch

    if not Path(repo).is_dir():
        # No repo on disk at all. `_head_or_empty` below resolves this to a clean
        # no-op, and two existing tests pin that (`..._repo_missing_is_clean_noop`).
        # Checked BEFORE any git call: subprocess raises FileNotFoundError on a
        # missing cwd, which is an OSError, NOT a GitCommitError.
        return
    row = session.get(Workspace, workspace_id)
    if row is None:
        return
    recorded = row.current_branch
    if not recorded:
        return
    try:
        if current_branch(repo) == recorded:
            return
    except (GitCommitError, OSError):
        return  # detached, or the repo went away underneath us
    try:
        checkout_branch(repo, recorded)
    except (GitCommitError, OSError):
        logger.warning(
            "collab.checkpoint_head_reconcile_failed",
            extra={
                "event": "collab_checkpoint_head_reconcile_failed",
                "workspace_id": str(workspace_id),
                "branch": recorded,
            },
        )
        return
    logger.warning(
        "collab.checkpoint_head_reconciled",
        extra={
            "event": "collab_checkpoint_head_reconciled",
            "workspace_id": str(workspace_id),
            "branch": recorded,
        },
    )


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
    stamp: FenceStamp,
    workspace_id: uuid.UUID,
    artifact: str,
    workspaces_dir,
    extra_files: dict[str, str] | None = None,
    record_version: bool = True,
    record_event: bool | None = None,
) -> CheckpointResult:
    repo = workspace_repo_dir(workspaces_dir, workspace_id)

    # None means "follow record_version" (the default/existing behavior for
    # every pre-existing call site, byte-for-byte). A kernel op that emits its
    # OWN same-transaction user-action WorkspaceEvent (e.g. the Claims-tab
    # insert's user_citation_insert) passes record_event=False to keep the
    # ArtifactVersion (provenance) while suppressing the redundant
    # collab_checkpoint feed row — one click, one feed row.
    effective_event = record_version if record_event is None else record_event

    # Acquire the per-workspace commit mutex FIRST, so the whole
    # read -> decide -> commit -> advance sequence below is serialized against
    # concurrent checkpoints AND the still-live git-first paths (save_section,
    # human_cite, agent tools). Reading the watermark/log BEFORE the mutex would
    # let a racing checkpoint commit and advance the watermark between our read
    # and our write — we could then rewrite git HEAD backwards and regress the
    # watermark. Released at the caller's COMMIT/ROLLBACK; we never commit here.
    #
    # Primitive B (endgame Task 5): refuse outright if the workspace was deleted OR
    # has moved past the caller's stamped generation while we blocked. A checkpoint
    # writes git + DB rows, which is precisely the mutation deletion must stop. The
    # generation is threaded from the caller's access check (the stamp), never
    # re-read here at the write. Teardown/background callers skip on
    # WorkspaceDeleted/WorkspaceGenerationStale; the user-triggered "Save a version"
    # route lets both reach the 404 handler.
    acquire_commit_mutex_at_generation(session, stamp.workspace_id, generation=stamp.generation)

    # Branch design §4.1: `workspaces.current_branch` is the source of truth and
    # git HEAD is a CACHE. Reconcile before the head read below, because this
    # function commits to whatever HEAD is.
    #
    # Without this, a branch switch that crashed between its checkout and its DB
    # commit leaves HEAD on the target while the column still names the source —
    # and the very next checkpoint (a last-leave, the periodic scan, a kernel
    # accept, the explicit "Save a version" route: ten call sites, most of them
    # NOT branch operations) would commit the SOURCE branch's log onto the TARGET
    # branch. §4.1 promises "reconciliation repairs it even after a crash"; that
    # promise is only true if every git-writing entry point keeps it, not just the
    # branch operations.
    #
    # Under the mutex, so it cannot race a concurrent switch. A no-op when the two
    # already agree, which is every ordinary call.
    _reconcile_head_for_checkpoint(session, workspace_id=workspace_id, repo=repo)

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
    _refuse_unreadable_projection(md, workspace_id=workspace_id, artifact=artifact)
    fname = filename_for(_ARTIFACT[artifact])
    author_tag = ",".join(str(a) for a in authors)
    files = {fname: md}
    if extra_files:
        files.update(extra_files)
    commit_sha = write_and_commit_files(
        repo, files, f"collab checkpoint: {artifact} (authors:{author_tag})"
    )
    changed = commit_sha != head
    if changed and record_version:
        session.add(
            ArtifactVersion(
                workspace_id=workspace_id,
                commit_sha=commit_sha,
                created_by="user",  # authors is non-empty on this path
                message=f"collab checkpoint: {artifact}",
            )
        )
    if changed and effective_event:
        session.add(
            WorkspaceEvent(
                workspace_id=workspace_id,
                kind="collab_checkpoint",
                actor_id=authors[0] if len(authors) == 1 else None,
                payload={"artifact": artifact, "commit_sha": commit_sha, "authors": authors},
            )
        )
    if changed and artifact == "draft":
        # Prompt Tier-2 coverage for the just-committed draft (3b-5d.D3): mirror
        # save_section step 10, funnelled. `md` is the committed HEAD content;
        # idempotent, so covered sections are free no-ops. Gated on `changed`
        # (not record_version) so the silent tail-scan checkpoints are covered too.
        enqueue_uncovered_sections(
            session, workspace_id=workspace_id, doc=md, commit_sha=commit_sha
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


def checkpoint_active_tails(session_factory, *, workspaces_dir, min_tail: int) -> int:
    """Periodic tail-bound (Slice 3b-5c): scan for growing collab logs and quietly
    checkpoint each. Runs on a BackgroundScheduler thread — pure DB+git, never
    touches the asyncio room MANAGER. Each candidate is checkpointed in its OWN
    transaction so one artifact's failure never aborts the others; errors are logged
    and swallowed (best-effort maintenance)."""
    with session_factory() as s:
        candidates = store.find_checkpoint_candidates(s, min_tail=min_tail)
    done = 0
    for workspace_id, artifact in candidates:
        with session_factory() as s:
            # Re-check liveness (mirrors permissions.make_checkpoint_on_evict): a
            # workspace soft-deleted between scan and now must not be checkpointed.
            ws = s.get(Workspace, workspace_id)
            if ws is None or ws.soft_deleted_at is not None:
                continue
            # Mint the fence stamp from the liveness re-screen above (the access
            # check for this background pass): checkpoint_artifact re-validates it
            # under the mutex, so a workspace deleted or bumped between here and the
            # mutex is caught as WorkspaceDeleted/WorkspaceGenerationStale below.
            stamp = stamp_for_workspace(ws)
            try:
                checkpoint_artifact(
                    s,
                    stamp=stamp,
                    workspace_id=workspace_id,
                    artifact=artifact,
                    workspaces_dir=workspaces_dir,
                    record_version=False,
                )
                s.commit()
                done += 1
            except (WorkspaceDeleted, WorkspaceGenerationStale):
                # Deleted — or deleted-and-restored into a new generation — between
                # the liveness screen above and the mutex. Expected, not a failure:
                # the tail we scanned belongs to a workspace/epoch that is gone, so
                # there is nothing to checkpoint and nothing to retry.
                s.rollback()
                continue
            except Exception as exc:  # noqa: BLE001 — best-effort; isolate per artifact
                s.rollback()
                logger.warning(
                    "collab.checkpoint_tail_failed",
                    extra={
                        "event": "collab_checkpoint_tail_failed",
                        "workspace_id": str(workspace_id),
                        "artifact": artifact,
                    },
                    # Since Slice C3 bounded the mutex-held tail, routine row
                    # contention arrives here as OperationalError(55P03). WARNING
                    # stays right either way — the attempt rolled back, the
                    # watermark did not move, and the next scan re-selects the
                    # same tail — but a stack trace for ordinary contention is
                    # noise on a job that ticks continuously. Everything else
                    # keeps its traceback: a genuine failure on a best-effort
                    # background path is only ever diagnosable from this log line.
                    exc_info=not is_lock_timeout(exc),
                )
    return done
