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

from grant_lab.db.models import CoherenceJob, ProposedChange
from grant_lab.proposal_assist.citations.audit import validate_citations_deterministic
from grant_lab.proposal_assist.citations.ledger import load_ledger
from grant_lab.proposal_assist.collab.canonical import (
    current_artifact_sha,
    current_artifact_text_and_sha,
)
from grant_lab.proposal_assist.editing.save import CITATIONS_FILENAME
from grant_lab.proposal_assist.editing.sectionizer import split_sections
from grant_lab.proposal_assist.evaluator.jobs import latest_completed_evaluator_job
from grant_lab.proposal_assist.problems.builders import (
    broken_citation_problems,
    coherence_drift_problem,
    count_by_severity,
    criterion_gap_problems,
    ledger_unreadable_problem,
    order_problems,
    over_length_problems,
    uncited_claim_problems,
)
from grant_lab.proposal_assist.uncited_claims.jobs import (
    dismissed_claim_hashes,
    latest_completed_uncited_claims_job,
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

    # ONE read of the draft, and every signal below is judged against it — the text
    # AND the sha the staleness flags compare. Reading git here while hashing the room
    # below made this one function judge two different documents: it reported broken
    # citations the researcher had already fixed (the deleted marker survives in git)
    # and missed over-length sections they could see (git has not caught up).
    #
    # Read DEFENSIVELY: an unreadable path (permissions, a directory), invalid UTF-8
    # or an unprojectable log must not 500 the endpoint. On failure the draft is
    # treated as empty, so the citation + over-length signals contribute nothing and
    # the sha comparisons below report the scorecard as stale — "we could not confirm
    # this is current" is the honest degradation, never a silent "it is current".
    try:
        draft_text, draft_sha = current_artifact_text_and_sha(
            session, workspace_id=workspace_id, artifact="draft", repo_dir=repo_dir
        )
    except Exception:
        logger.exception("problems: draft read failed")
        draft_text, draft_sha = "", ""
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
    except (ValueError, OSError) as exc:
        # ValueError = corrupt/wrong-version JSON (load_ledger). OSError = the ledger
        # path is unreadable (a directory, permissions) — surface it as the SAME
        # "ledger unreadable" error row rather than silently dropping it (which would
        # also hide the draft's orphan markers).
        rows.append(ledger_unreadable_problem(str(exc)))
    except Exception:
        logger.exception("problems: broken_citation signal failed")

    # 2. criterion gaps + scorecard staleness (same logic as web/evaluator.py:107-112).
    scorecard_stale: list[str] = []
    try:
        job = latest_completed_evaluator_job(session, workspace_id=workspace_id)
        scorecard = job.scorecard if job is not None else None
        if job is not None:
            if job.evaluated_draft_sha != draft_sha:
                scorecard_stale.append("draft")
            if job.evaluated_spec_sha != current_artifact_sha(
                session, workspace_id=workspace_id, artifact="spec", repo_dir=repo_dir
            ):
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

    # 5. uncited claims (LLM) — latest completed pass; dismissed hashes filtered out.
    uncited_claims_stale = False
    try:
        uc_job = latest_completed_uncited_claims_job(session, workspace_id=workspace_id)
        if uc_job is not None:
            dismissed = dismissed_claim_hashes(session, workspace_id=workspace_id)
            uncited_claims_stale = uc_job.evaluated_draft_sha != draft_sha
            rows += uncited_claim_problems(uc_job.claims or [], dismissed)
    except Exception:
        logger.exception("problems: uncited_claim signal failed")

    # Final ordering is the one step outside the per-signal try-blocks; guard it too
    # so the never-500 contract holds even against an un-coerced row (counts are a
    # total tally over the same set, so they're computed regardless of sort order).
    counts = count_by_severity(rows)
    try:
        ordered = order_problems(rows)
    except Exception:
        logger.exception("problems: ordering failed; returning rows unordered")
        ordered = rows
    return {
        "problems": ordered,
        "counts": counts,
        "scorecard_stale": scorecard_stale,
        "uncited_claims_stale": uncited_claims_stale,
    }
