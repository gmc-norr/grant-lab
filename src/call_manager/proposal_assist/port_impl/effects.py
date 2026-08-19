"""SqlEffects — the semantic operations, each owning its own fence.

Every operation here follows mechanism 1 of spec 3.5: acquire the SHARED lease,
then perform ONE predicate-bearing write, then inspect what it returned.

Two rules govern every method and neither is negotiable:

  1. The generation predicate is INSIDE the write statement. A predicate in its
     own statement guards nothing — both statements take fresh READ COMMITTED
     snapshots. That defect shipped twice (28abbc4, 701fbb7).
  2. The lease is taken BEFORE the write and never upgraded to the exclusive
     mutex. Shared-then-exclusive is the one deadlock shape this protocol has.
"""

from __future__ import annotations

from typing import Any, NoReturn

from sqlalchemy import exists, select, update
from sqlalchemy.orm import sessionmaker

from call_manager.db.models import (
    EvaluatorJob,
    ReviewRun,
    ReviewRunItem,
    UncitedClaimsJob,
    Workspace,
)
from call_manager.proposal_assist.fencing import acquire_shared_lease, live_at_generation
from call_manager.proposal_assist.ports.effects import (
    SubmitReview,
    SubmitScorecard,
    SubmitUncitedClaims,
)
from call_manager.proposal_assist.ports.errors import (
    EffectRejected,
    WorkspaceGenerationStale,
)
from call_manager.proposal_assist.ports.stamp import FenceStamp

_SCORECARD_TWICE = "a scorecard has already been submitted for this run"
_SCORECARD_TARGET = "scorecard target is not an active evaluator run for this workspace"
_REVIEW_TWICE = "a review has already been submitted for this item"
_REVIEW_RUN = "submit_review target run is not active for this workspace"
_REVIEW_ITEM = "submit_review target is not a running item for this run"
_CLAIMS_TWICE = "claims have already been submitted for this run"
_CLAIMS_TARGET = "target is not an active uncited-claims run for this workspace"


def _explain_refusal(
    session,
    stamp: FenceStamp,
    *,
    checks: tuple[tuple[Any, str], ...],
    fallback: str,
) -> NoReturn:
    """Decide WHICH message the agent sees. Authorizes nothing.

    Runs only after the write's own single statement already refused it. Every
    statement here is diagnostic: moving any of it ahead of the write would turn
    the operation into the two-statement race the fence exists to close.

    Staleness is classified first because a deletion also bumps the generation,
    so both conditions hold at once and callers map them to different outcomes.
    """
    row = session.execute(
        select(Workspace.lifecycle_generation, Workspace.soft_deleted_at).where(
            Workspace.id == stamp.workspace_id
        )
    ).first()
    if row is None or row[1] is not None or row[0] != stamp.generation:
        raise WorkspaceGenerationStale(
            stamp.workspace_id,
            expected=stamp.generation,
            actual=None if row is None else row[0],
        )
    for clause, message in checks:
        if session.execute(select(clause)).scalar():
            raise EffectRejected(message)
    raise EffectRejected(fallback)


class SqlEffects:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._sessions = session_factory

    def submit_scorecard(self, stamp: FenceStamp, cmd: SubmitScorecard) -> None:
        with self._sessions.begin() as session:
            acquire_shared_lease(session, stamp.workspace_id)
            landed = session.execute(
                update(EvaluatorJob)
                .where(
                    EvaluatorJob.id == stamp.origin_job_id,
                    EvaluatorJob.workspace_id == stamp.workspace_id,
                    EvaluatorJob.status == "running",
                    EvaluatorJob.scorecard.is_(None),
                    live_at_generation(stamp.workspace_id, stamp.generation),
                )
                .values(scorecard=cmd.scorecard)
                .returning(EvaluatorJob.id)
            ).first()
            if landed is None:
                _explain_refusal(
                    session,
                    stamp,
                    checks=(
                        (
                            exists().where(
                                EvaluatorJob.id == stamp.origin_job_id,
                                EvaluatorJob.workspace_id == stamp.workspace_id,
                                EvaluatorJob.scorecard.is_not(None),
                            ),
                            _SCORECARD_TWICE,
                        ),
                    ),
                    fallback=_SCORECARD_TARGET,
                )

    def submit_review(self, stamp: FenceStamp, cmd: SubmitReview) -> None:
        run_is_active = exists().where(
            ReviewRun.id == stamp.origin_job_id,
            ReviewRun.workspace_id == stamp.workspace_id,
            ReviewRun.status == "running",
        )
        with self._sessions.begin() as session:
            acquire_shared_lease(session, stamp.workspace_id)
            landed = session.execute(
                update(ReviewRunItem)
                .where(
                    ReviewRunItem.id == cmd.review_run_item_id,
                    ReviewRunItem.review_run_id == stamp.origin_job_id,
                    ReviewRunItem.status == "running",
                    ReviewRunItem.raw_response_json.is_(None),
                    run_is_active,
                    live_at_generation(stamp.workspace_id, stamp.generation),
                )
                .values(raw_response_json={"markdown": cmd.markdown})
                .returning(ReviewRunItem.id)
            ).first()
            if landed is None:
                # ORDER MATTERS and it mirrors review_submit.py's today: item
                # identity/status, THEN the parent run's ownership, THEN
                # exactly-once. Checking the response first would tell a caller
                # holding a foreign item's id that a review already exists there
                # — cross-workspace state, and a changed message besides.
                item_is_claimable = exists().where(
                    ReviewRunItem.id == cmd.review_run_item_id,
                    ReviewRunItem.review_run_id == stamp.origin_job_id,
                    ReviewRunItem.status == "running",
                )
                _explain_refusal(
                    session,
                    stamp,
                    checks=(
                        (~item_is_claimable, _REVIEW_ITEM),
                        (~run_is_active, _REVIEW_RUN),
                        (
                            exists().where(
                                ReviewRunItem.id == cmd.review_run_item_id,
                                ReviewRunItem.review_run_id == stamp.origin_job_id,
                                ReviewRunItem.raw_response_json.is_not(None),
                            ),
                            _REVIEW_TWICE,
                        ),
                    ),
                    fallback=_REVIEW_ITEM,
                )

    def submit_uncited_claims(self, stamp: FenceStamp, cmd: SubmitUncitedClaims) -> None:
        with self._sessions.begin() as session:
            acquire_shared_lease(session, stamp.workspace_id)
            landed = session.execute(
                update(UncitedClaimsJob)
                .where(
                    UncitedClaimsJob.id == stamp.origin_job_id,
                    UncitedClaimsJob.workspace_id == stamp.workspace_id,
                    UncitedClaimsJob.status == "running",
                    UncitedClaimsJob.claims.is_(None),
                    live_at_generation(stamp.workspace_id, stamp.generation),
                )
                .values(claims=cmd.claims)
                .returning(UncitedClaimsJob.id)
            ).first()
            if landed is None:
                _explain_refusal(
                    session,
                    stamp,
                    checks=(
                        (
                            exists().where(
                                UncitedClaimsJob.id == stamp.origin_job_id,
                                UncitedClaimsJob.workspace_id == stamp.workspace_id,
                                UncitedClaimsJob.claims.is_not(None),
                            ),
                            _CLAIMS_TWICE,
                        ),
                    ),
                    fallback=_CLAIMS_TARGET,
                )
