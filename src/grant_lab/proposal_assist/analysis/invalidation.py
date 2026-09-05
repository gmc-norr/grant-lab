"""Library-change staleness (spec §3, rev-3 finding 6). Both directions:
grew → re-check no_match; shrank/changed → re-check supported.

Call sites (references/ingest.py and references/deletion.py) wrap these hooks
in try/except-log: invalidation is best-effort and must never break an ingest
or a deletion. Likewise, repo trouble inside a hook (missing proposal.md,
unreadable HEAD) suppresses the *enqueue* only — the DB staleing always
happens, and the reconcile tick converges analysis coverage later.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from grant_lab.db.models import ClaimClassification, UserClaimAnnotation
from grant_lab.proposal_assist.analysis.queue import enqueue_section_job
from grant_lab.proposal_assist.citations.gitio import GitCommitError, current_head
from grant_lab.proposal_assist.editing.sectionizer import split_sections
from grant_lab.proposal_assist.paths import workspace_repo_dir

logger = logging.getLogger(__name__)


def _enqueue_for_shas(
    session: Session,
    workspace_id: uuid.UUID,
    target_shas: set[str],
    workspaces_dir: Path | str,
) -> int:
    """Enqueue one analysis job per target sha still live at HEAD. Returns the
    number of jobs actually inserted (content-keyed dedup may skip some).

    Missing proposal.md → 0; unreadable HEAD (GitCommitError) → 0, logged.
    Duplicate sections share content, so each live target sha enqueues once.
    The caller commits.
    """
    if not target_shas:
        return 0
    repo = workspace_repo_dir(workspaces_dir, workspace_id)
    try:
        doc = (repo / "proposal.md").read_text(encoding="utf-8")
    except OSError:
        logger.info(
            "claim-analysis invalidation: no readable proposal.md for workspace %s — skip enqueue",
            workspace_id,
        )
        return 0
    try:
        head = current_head(repo)
    except GitCommitError:
        logger.warning(
            "claim-analysis invalidation: cannot read HEAD for workspace %s — skip enqueue",
            workspace_id,
            exc_info=True,
        )
        return 0

    inserted = 0
    seen: set[str] = set()
    for sec in split_sections(doc):
        sha = sec.sha
        if sha not in target_shas or sha in seen:
            continue
        seen.add(sha)
        if enqueue_section_job(
            session,
            workspace_id=workspace_id,
            section_index=sec.index,
            section_heading=sec.heading,
            section_sha=sha,
            commit_sha=head,
        ):
            inserted += 1
    return inserted


def _open_annotation_shas(
    session: Session,
    workspace_id: uuid.UUID,
    kinds: tuple[str, ...],
    reference_id: uuid.UUID | None = None,
) -> set[str]:
    """Distinct section_sha of OPEN annotations of the given kinds, optionally
    restricted to annotations pointing at one reference."""
    q = session.query(UserClaimAnnotation.section_sha).filter(
        UserClaimAnnotation.workspace_id == workspace_id,
        UserClaimAnnotation.kind.in_(kinds),
        UserClaimAnnotation.status == "open",
    )
    if reference_id is not None:
        q = q.filter(UserClaimAnnotation.reference_id == reference_id)
    return {sha for (sha,) in q.distinct().all()}


def on_reference_text_ingested(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    workspaces_dir: Path | str,
) -> int:
    """Library grew — a reference's text ingest completed. Stale every
    no_match classification for the workspace (a previously unmatched claim
    may now be supported) and re-enqueue the HEAD sections that carry open
    uncited_no_match annotations. Returns the number of jobs enqueued.

    Annotations are NOT flipped here: the pipeline's fan-out stales open
    annotations for a re-analyzed section before writing fresh findings, so a
    now-supported claim REPLACES its old no_match row — no contradictory
    strip. The caller commits.
    """
    session.query(ClaimClassification).filter(
        ClaimClassification.workspace_id == workspace_id,
        ClaimClassification.outcome == "no_match",
        ClaimClassification.stale_at.is_(None),
    ).update({"stale_at": datetime.now(UTC)}, synchronize_session=False)

    shas = _open_annotation_shas(session, workspace_id, ("uncited_no_match",))
    return _enqueue_for_shas(session, workspace_id, shas, workspaces_dir)


def on_reference_unavailable(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    workspaces_dir: Path | str,
) -> int:
    """Library shrank/changed — a reference was tombstoned or hard-deleted.
    Stale the supported classifications pointing at it, flip its open
    uncited_supported annotations to stale (the strip must stop offering
    Insert for evidence that no longer exists), and re-enqueue their HEAD
    sections. Returns the number of jobs enqueued.

    ORDER MATTERS (spec rev-3 ordering bug): the section shas are collected
    FIRST — the collection filters status='open', so staleing the annotations
    before collecting would always find zero rows and never re-enqueue.

    Hard-delete callers must invoke this BEFORE session.delete(ref): the FKs
    on ClaimClassification.reference_id / UserClaimAnnotation.reference_id
    are SET NULL on delete, so calling after matches zero rows. The caller
    commits.
    """
    # 1. Collect while the annotations are still open.
    shas = _open_annotation_shas(
        session, workspace_id, ("uncited_supported",), reference_id=reference_id
    )
    # 2. Stale the cached retrieval outcomes pointing at this reference.
    session.query(ClaimClassification).filter(
        ClaimClassification.workspace_id == workspace_id,
        ClaimClassification.reference_id == reference_id,
        ClaimClassification.stale_at.is_(None),
    ).update({"stale_at": datetime.now(UTC)}, synchronize_session=False)
    # 3. Flip the open annotations to stale.
    session.query(UserClaimAnnotation).filter(
        UserClaimAnnotation.workspace_id == workspace_id,
        UserClaimAnnotation.kind == "uncited_supported",
        UserClaimAnnotation.status == "open",
        UserClaimAnnotation.reference_id == reference_id,
    ).update({"status": "stale"}, synchronize_session=False)
    # 4. Re-enqueue the collected sections still live at HEAD.
    return _enqueue_for_shas(session, workspace_id, shas, workspaces_dir)
