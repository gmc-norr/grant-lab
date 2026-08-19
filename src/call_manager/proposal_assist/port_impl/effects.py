"""SqlEffects — the semantic operations, each owning its own fence.

Every operation here follows mechanism 1 of spec 3.5: acquire the SHARED lease,
then perform ONE predicate-bearing write, then inspect what it returned.

Two rules govern every method and neither is negotiable:

  1. The generation predicate is INSIDE the write statement. A predicate in its
     own statement guards nothing — both statements take fresh READ COMMITTED
     snapshots. That defect shipped twice (28abbc4, 701fbb7).
  2. The lease is taken BEFORE the write and never upgraded to the exclusive
     mutex. Shared-then-exclusive is the one deadlock shape this protocol has.

Call these operations with no other transaction open for the same workspace.
Each method opens its own session via `session_factory()`, so calling one from
inside an already-open transaction runs it on a second, separate connection.
Today's three write tools each hold their target row under
`with_for_update=True` in an enclosing `ctx.session_factory()` session
(`tools/scorecard.py:93`, `tools/uncited_claims.py:44`,
`tools/review_submit.py:20`). A conversion that swaps only the write call for
one of these operations, while leaving that outer session open, leaves
connection A holding the job row while connection B — this module's own
`session_factory()` call — waits on the shared lease for the same workspace's
mutex key. That is the M → J inversion §5 rule 3 forbids, but split across two
connections it evades detection: neither the spec's per-transaction lock-
ordering rules nor PostgreSQL's deadlock detector sees a single-transaction
violation, because each connection is merely blocked, not part of a cycle
visible to the server. Under load it surfaces as a `CommitMutexTimeout`, not a
deadlock — commit (or roll back) the outer transaction first.
"""

from __future__ import annotations

import uuid
from typing import Any, NoReturn

from sqlalchemy import exists, insert, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, sessionmaker

from call_manager.db.models import (
    EvaluatorJob,
    ProposedChange,
    ReviewRun,
    ReviewRunItem,
    UncitedClaimsJob,
    Workspace,
)
from call_manager.proposal_assist.fencing import acquire_shared_lease, live_at_generation
from call_manager.proposal_assist.ports.effects import (
    ProposeArtifactEdit,
    ProposeCitationInsert,
    SubmitReview,
    SubmitScorecard,
    SubmitUncitedClaims,
)
from call_manager.proposal_assist.ports.errors import (
    EffectRejected,
    WorkspaceGenerationStale,
)
from call_manager.proposal_assist.ports.stamp import FenceStamp, Provenance

_SCORECARD_TWICE = "a scorecard has already been submitted for this run"
_SCORECARD_TARGET = "scorecard target is not an active evaluator run for this workspace"
_REVIEW_TWICE = "a review has already been submitted for this item"
_REVIEW_RUN = "submit_review target run is not active for this workspace"
_REVIEW_ITEM = "submit_review target is not a running item for this run"
_CLAIMS_TWICE = "claims have already been submitted for this run"
_CLAIMS_TARGET = "target is not an active uncited-claims run for this workspace"
_PROPOSE_TARGET = "this workspace is no longer accepting proposed changes"


def _explain_refusal(
    session: Session,
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

    def _insert_proposed_change(
        self,
        stamp: FenceStamp,
        *,
        kind: str,
        target_artifact: str,
        section_index: int,
        base_commit_sha: str,
        base_section_sha: str,
        payload: dict[str, Any],
        provenance: Provenance,
    ) -> uuid.UUID:
        """The shared INSERT for both propose_* effects.

        INSERT … SELECT … WHERE, never INSERT … VALUES after a check: an INSERT
        has no target row, so there is no READ COMMITTED re-check to mask a
        missing fence, and the check-then-insert shape is the exact race
        reproduced on PostgreSQL 16.

        `id` is supplied explicitly because FROM SELECT does not apply
        Python-side column defaults. `status` and `created_at` are SERVER
        defaults and are correctly left out.
        """
        pc_id = uuid.uuid4()
        with self._sessions.begin() as session:
            acquire_shared_lease(session, stamp.workspace_id)
            landed = session.execute(
                insert(ProposedChange)
                .from_select(
                    [
                        "id",
                        "workspace_id",
                        "target_artifact",
                        "section_index",
                        "base_commit_sha",
                        "base_section_sha",
                        "kind",
                        "payload",
                        "origin_kind",
                        "origin_session_id",
                        "origin_job_id",
                        "triggering_turn_id",
                        "triggering_prompt",
                        "created_by",
                    ],
                    select(
                        literal(pc_id),
                        literal(stamp.workspace_id),
                        literal(target_artifact),
                        literal(section_index),
                        literal(base_commit_sha),
                        literal(base_section_sha),
                        literal(kind),
                        literal(payload, type_=JSONB),
                        literal(provenance.origin_kind),
                        literal(provenance.origin_session_id),
                        # From the stamp, not from provenance: stamp.origin_job_id
                        # is the SAME field the live_at_generation predicate above
                        # authorizes this INSERT against, so the actor this row is
                        # attributed to is provably the actor the fence checked.
                        # Provenance carries no origin_job_id of its own — see
                        # FenceStamp's and Provenance's docstrings in ports/stamp.py.
                        literal(stamp.origin_job_id),
                        literal(provenance.triggering_turn_id),
                        literal(provenance.triggering_prompt),
                        literal(provenance.created_by),
                    ).where(live_at_generation(stamp.workspace_id, stamp.generation)),
                )
                .returning(ProposedChange.id)
            ).first()
            if landed is None:
                _explain_refusal(session, stamp, checks=(), fallback=_PROPOSE_TARGET)
            return landed[0]

    def propose_artifact_edit(self, stamp: FenceStamp, cmd: ProposeArtifactEdit) -> uuid.UUID:
        payload: dict[str, Any] = {"new_text": cmd.new_text}
        if cmd.target_artifact == "draft" and cmd.selection:
            payload |= {
                "selection_anchor": cmd.selection.get("anchor"),
                "selection_from": cmd.selection.get("from_offset"),
                "selection_to": cmd.selection.get("to_offset"),
            }
        return self._insert_proposed_change(
            stamp,
            kind="prose_edit",
            target_artifact=cmd.target_artifact,
            section_index=cmd.section_index,
            base_commit_sha=cmd.base_commit_sha,
            base_section_sha=cmd.base_section_sha,
            payload=payload,
            provenance=cmd.provenance,
        )

    def propose_citation_insert(
        self, stamp: FenceStamp, cmd: ProposeCitationInsert
    ) -> uuid.UUID:
        return self._insert_proposed_change(
            stamp,
            kind="citation_insert",
            target_artifact="draft",
            section_index=cmd.section_index,
            base_commit_sha=cmd.base_commit_sha,
            base_section_sha=cmd.base_section_sha,
            payload={
                "reference_id": str(cmd.reference_id),
                "anchor": cmd.anchor,
                "chunk_index": cmd.chunk_index,
                "evidence_quote": cmd.evidence_quote,
                "stance": cmd.stance,
                "confidence": cmd.confidence,
            },
            provenance=cmd.provenance,
        )
