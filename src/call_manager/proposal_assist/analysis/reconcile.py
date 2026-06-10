"""Self-healing coverage tick (spec §3): every HEAD section of every
drafting workspace gets a job row eventually, even if the save-time enqueue
was lost to the post-commit DB-failure window (spec §6)."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select

from call_manager.db.models import ClaimAnalysisJob, Workspace
from call_manager.proposal_assist.analysis.queue import enqueue_section_job
from call_manager.proposal_assist.citations.gitio import GitCommitError, current_head
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir

logger = logging.getLogger(__name__)

# A section is "covered" when a job for its exact content is queued, running,
# or done. FAILED is deliberately absent: a dead-lettered job's content gets
# another chance via this tick (fresh row, fresh attempt budget). SUPERSEDED is
# likewise absent, but a superseded sha only reappears at HEAD via a content
# revert, which deserves a re-run anyway.
_COVERED_STATUSES = ("queued", "running", "done")


def reconcile_analysis_coverage(*, session_factory, workspaces_dir) -> int:
    """Enqueue an analysis job for every live HEAD section that has no
    queued/running/done job. Returns the number of jobs inserted.

    One session, one commit at the end. Workspaces that are soft-deleted,
    have no proposal.md, or have an unreadable HEAD are skipped without
    raising — the next tick retries.
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
                doc = (repo / "proposal.md").read_text(encoding="utf-8")
            except OSError:
                # No repo / no proposal yet (workspace still in setup) — skip.
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

            covered = set(
                session.execute(
                    select(ClaimAnalysisJob.section_sha).where(
                        ClaimAnalysisJob.workspace_id == ws_id,
                        ClaimAnalysisJob.status.in_(_COVERED_STATUSES),
                    )
                ).scalars()
            )
            for sec in split_sections(doc):
                if not sec.text.strip():
                    continue  # empty preamble/section — nothing to analyze
                if sec.sha in covered:
                    continue
                if enqueue_section_job(
                    session,
                    workspace_id=ws_id,
                    section_index=sec.index,
                    section_heading=sec.heading,
                    section_sha=sec.sha,
                    commit_sha=head,
                ):
                    inserted += 1
                # Duplicate sections share content (and a sha) — enqueue once.
                covered.add(sec.sha)
        session.commit()
    return inserted
