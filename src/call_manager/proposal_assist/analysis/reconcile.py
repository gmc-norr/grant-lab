"""Self-healing coverage tick (spec §3): every HEAD section of every
drafting workspace gets a job row eventually, even if the save-time enqueue
was lost to the post-commit DB-failure window (spec §6)."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select

from call_manager.db.models import Workspace
from call_manager.proposal_assist.analysis.queue import enqueue_uncovered_sections
from call_manager.proposal_assist.citations.gitio import GitCommitError, current_head
from call_manager.proposal_assist.collab.canonical import current_artifact_text_and_sha
from call_manager.proposal_assist.paths import workspace_repo_dir

logger = logging.getLogger(__name__)


def reconcile_analysis_coverage(*, session_factory, workspaces_dir) -> int:
    """Enqueue an analysis job for every live HEAD section that has no
    queued/running/done job. Returns the number of jobs inserted.

    "Live HEAD section" means a section of the document as it is RIGHT NOW: the
    room-canonical projection of the durable Yjs log when collab is on for the draft,
    raw git HEAD when it is off (`current_artifact_text_and_sha`). Reading git
    unconditionally enqueued work for prose the researcher had already rewritten and
    never enqueued the sections they could see — and `analyze_job` resolves the
    job's section against the SAME basis, so the two must move together or every job
    this tick creates is superseded on arrival.

    `commit_sha` stays git HEAD: nothing reads it back, it is provenance for "the git
    state when this job was created", and it is pinned as a real 40-hex sha.

    One session, one commit at the end. Workspaces that are soft-deleted, have no
    draft yet, or have an unreadable HEAD are skipped without raising — the next
    tick retries. The covered/uncovered predicate lives in
    queue.enqueue_uncovered_sections (shared with the collab checkpoint hook,
    3b-5d.D3).
    """
    inserted = 0
    with session_factory() as session:
        ws_ids = (
            session.execute(select(Workspace.id).where(Workspace.soft_deleted_at.is_(None)))
            .scalars()
            .all()
        )
        for ws_id in ws_ids:
            repo = workspace_repo_dir(Path(workspaces_dir), ws_id)
            try:
                doc, _ = current_artifact_text_and_sha(
                    session, workspace_id=ws_id, artifact="draft", repo_dir=repo
                )
            except Exception:
                # One workspace must not halt coverage for every other workspace.
                # The pre-canonical read could only raise OSError; projecting the
                # durable log adds markdown/ProseMirror parse failures, and this
                # tick's whole contract is "skip it, the next tick retries".
                logger.exception(
                    "analysis reconcile: cannot read the draft for workspace %s — skipped",
                    ws_id,
                )
                continue
            if not doc:
                # No repo / no proposal yet (workspace still in setup), or an empty
                # document — nothing to cover either way.
                continue
            try:
                head = current_head(repo)
            except GitCommitError:
                logger.debug(
                    "analysis reconcile: cannot read HEAD for workspace %s — skipped",
                    ws_id,
                    exc_info=True,
                )
                continue
            inserted += enqueue_uncovered_sections(
                session, workspace_id=ws_id, doc=doc, commit_sha=head
            )
        session.commit()
    return inserted
