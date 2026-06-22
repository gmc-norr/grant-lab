"""Defensive orchestrator for the Problems panel.

Reads the draft + ledger + latest scorecard + pending coherence count, runs the
pure builders (builders.py) each in its own try-block so one bad source never
500s the endpoint, and returns {problems, counts, scorecard_stale}. NO LLM, no
mutation, no new table.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from call_manager.db.models import CoherenceJob, ProposedChange
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME, SPEC_FILENAME
from call_manager.proposal_assist.citations.audit import validate_citations_deterministic
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.editing.save import CITATIONS_FILENAME
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.evaluator.context import content_sha
from call_manager.proposal_assist.evaluator.jobs import latest_completed_evaluator_job
from call_manager.proposal_assist.problems.builders import (
    broken_citation_problems,
    coherence_drift_problem,
    count_by_severity,
    criterion_gap_problems,
    ledger_unreadable_problem,
    order_problems,
    over_length_problems,
)

logger = logging.getLogger(__name__)


def count_pending_coherence_proposals(session: Session, workspace_id: uuid.UUID) -> int:
    """Pending coherence_session proposals whose emitting job is completed (the same
    visibility gate web/proposals.py applies to the inbox)."""
    return (
        session.scalar(
            select(func.count())
            .select_from(ProposedChange)
            .where(
                ProposedChange.workspace_id == workspace_id,
                ProposedChange.status == "pending",
                ProposedChange.origin_kind == "coherence_session",
                exists().where(
                    CoherenceJob.id == ProposedChange.origin_job_id,
                    CoherenceJob.status == "completed",
                ),
            )
        )
        or 0
    )


def assemble_problems(
    *,
    session: Session,
    workspace_id: uuid.UUID,
    repo_dir: Path,
    ws_rubric_updated_at,
    budget: int,
) -> dict:
    rows: list[dict] = []

    # Read+split the draft DEFENSIVELY too: an unreadable path (permissions, a
    # directory) or invalid UTF-8 must not 500 the endpoint. On failure the draft is
    # treated as empty (the citation + over-length signals then contribute nothing).
    try:
        draft_path = repo_dir / PROPOSAL_FILENAME
        draft_text = draft_path.read_text() if draft_path.exists() else ""
    except Exception:
        logger.exception("problems: draft read failed")
        draft_text = ""
    sections = split_sections(draft_text)  # pure/total over a str (cannot raise)

    # 1. broken citations — absent ledger STILL runs the validator (orphan markers
    #    surface); a corrupt ledger becomes ONE error row. Neither is silenced.
    try:
        citations_path = repo_dir / CITATIONS_FILENAME
        ledger = load_ledger(citations_path.read_text() if citations_path.exists() else None)
        issues = validate_citations_deterministic(
            proposal_text=draft_text,
            ledger=ledger,
            session=session,
            workspace_id=workspace_id,
        )
        rows += broken_citation_problems(issues, draft_text)
    except ValueError as exc:
        rows.append(ledger_unreadable_problem(str(exc)))
    except Exception:
        logger.exception("problems: broken_citation signal failed")

    # 2. criterion gaps + scorecard staleness (same logic as web/evaluator.py:107-112).
    scorecard_stale: list[str] = []
    try:
        job = latest_completed_evaluator_job(session, workspace_id=workspace_id)
        scorecard = job.scorecard if job is not None else None
        if job is not None:
            if job.evaluated_draft_sha != content_sha(repo_dir / PROPOSAL_FILENAME):
                scorecard_stale.append("draft")
            if job.evaluated_spec_sha != content_sha(repo_dir / SPEC_FILENAME):
                scorecard_stale.append("spec")
            if job.evaluated_rubric_updated_at != ws_rubric_updated_at:
                scorecard_stale.append("rubric")
        rows += criterion_gap_problems(scorecard)
    except Exception:
        logger.exception("problems: criterion_gap signal failed")

    # 3. over-length draft sections.
    try:
        rows += over_length_problems(sections, budget)
    except Exception:
        logger.exception("problems: over_length signal failed")

    # 4. coherence drift — one roll-up pointing at the inbox.
    try:
        drift = coherence_drift_problem(count_pending_coherence_proposals(session, workspace_id))
        if drift is not None:
            rows.append(drift)
    except Exception:
        logger.exception("problems: coherence_drift signal failed")

    ordered = order_problems(rows)
    return {
        "problems": ordered,
        "counts": count_by_severity(ordered),
        "scorecard_stale": scorecard_stale,
    }
