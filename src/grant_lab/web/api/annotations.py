"""Claim-analysis "Claims" tab /api surface. Re-exposes the existing annotation
workflow (read findings, insert, dismiss, reanalyze) as typed JSON for the React
cockpit. The endpoints are self-contained on shared domain primitives and add no
business logic or schema changes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from grant_lab.auth.dependencies import get_current_user
from grant_lab.db.models import (
    ClaimAnalysisJob,
    Reference,
    User,
    UserClaimAnnotation,
    Workspace,
    WorkspaceEvent,
)
from grant_lab.proposal_assist.analysis.insert import InsertConflict, insert_from_annotation
from grant_lab.proposal_assist.analysis.queue import (
    enqueue_containing_section,
    enqueue_section_job,
)
from grant_lab.proposal_assist.citations.collab_cite import insert_annotation_via_kernel
from grant_lab.proposal_assist.citations.core import _citation_snapshot
from grant_lab.proposal_assist.citations.gitio import GitCommitError, current_head
from grant_lab.proposal_assist.collab import state as collab_state
from grant_lab.proposal_assist.collab import store as collab_store
from grant_lab.proposal_assist.collab.canonical import canonical_artifact_markdown
from grant_lab.proposal_assist.drafting.locking import BUSY_ERROR_MESSAGE, CommitMutexTimeout
from grant_lab.proposal_assist.editing.save import PROPOSAL_FILENAME
from grant_lab.proposal_assist.editing.sectionizer import split_sections
from grant_lab.proposal_assist.paths import workspace_repo_dir
from grant_lab.proposal_assist.ports.stamp import FenceStamp, stamp_for_workspace
from grant_lab.proposal_assist.proposals.kernel_accept import RoomEvicted
from grant_lab.proposal_assist.web._perms import require_member, require_owner

router = APIRouter()

# ClaimAnalysisJob statuses that mean "analysis is still coming".
_JOB_PENDING_STATUSES = ("queued", "running")


class ClaimFindingOut(BaseModel):
    id: int
    kind: str
    claim_text: str
    evidence_quote: str | None
    ref_label: str
    can_insert: bool
    sentence_ordinal: int


class ClaimJobOut(BaseModel):
    status: str | None  # "pending" | "failed" | None (done / no job)
    error: str | None


class ClaimSectionOut(BaseModel):
    index: int
    heading: str
    section_sha: str
    occurrence: int
    job: ClaimJobOut
    findings: list[ClaimFindingOut]


class AnnotationsOut(BaseModel):
    is_owner: bool
    sections: list[ClaimSectionOut]


@router.get("/workspaces/{workspace_id}/annotations", response_model=AnnotationsOut)
def get_annotations(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> AnnotationsOut:
    """Claims-tab read-model: every live draft section with its open findings +
    the latest analysis-job state. Degrades to {is_owner, sections: []} (200) on
    ANY pre-draft state — no repo / no HEAD / no proposal.md — because the Claims
    tab is always mounted (a strip fragment may 404; an always-present tab cannot)."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        is_owner = ws.owner_id == user.id
        repo = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        proposal = repo / PROPOSAL_FILENAME
        if not proposal.exists():
            return AnnotationsOut(is_owner=is_owner, sections=[])
        try:
            current_head(repo)  # validate the repo has a commit; raises on pre-draft
        except GitCommitError:
            return AnnotationsOut(is_owner=is_owner, sections=[])
        # 3b-5d.D4 §3.4 (fixed in review #8): with draft collab enabled the tab
        # must sectionize the LIVE room ONLY once the room has diverged from
        # disk — an UNTOUCHED room (hydration seed only, no authored update
        # yet) is just a ProseMirror round-trip of the same git HEAD text, but
        # that round-trip canonicalizes block-boundary whitespace ("loose"),
        # while git HEAD and every pre-flip annotation's section_sha are the
        # raw ("tight") bytes. Serving the loose projection for an untouched
        # room would mismatch every pre-flip finding's tight sha and render
        # the Claims tab empty right after enable-on-touch. So: untouched ->
        # read raw disk (tight, matches pre-flip shas byte-for-byte); once the
        # room has a real edit -> read the room projection (claim rows keyed
        # to a live-edited section simply show no findings until the next
        # checkpoint's analysis lands and self-heals them). has_authored_updates
        # is the SAME predicate the kernel write path (apply_annotation_insert_
        # to_room) uses, so read and write can never disagree about which text
        # a section_sha was hashed against.
        if collab_state.is_collab_enabled(
            session, workspace_id=ws.id, artifact="draft"
        ) and collab_store.has_authored_updates(
            session, workspace_id=ws.id, artifact="draft"
        ):
            doc_raw = canonical_artifact_markdown(
                session, workspace_id=ws.id, artifact="draft", repo_dir=repo
            )
        else:
            doc_raw = proposal.read_text()
        sections = split_sections(doc_raw)

        # One query for all open annotations; group by (sha, occurrence).
        anns = list(
            session.scalars(
                select(UserClaimAnnotation)
                .where(
                    UserClaimAnnotation.workspace_id == ws.id,
                    UserClaimAnnotation.status == "open",
                )
                .order_by(UserClaimAnnotation.sentence_ordinal, UserClaimAnnotation.id)
            )
        )
        by_group: dict[tuple[str, int], list[UserClaimAnnotation]] = {}
        for a in anns:
            by_group.setdefault((a.section_sha, a.section_occurrence), []).append(a)

        # One query for "Family Year" ref labels (workspace-scoped — a
        # cross-workspace reference renders as the fallback).
        ref_ids = {a.reference_id for a in anns if a.reference_id is not None}
        labels: dict[uuid.UUID, str] = {}
        if ref_ids:
            for r in session.scalars(
                select(Reference).where(
                    Reference.workspace_id == ws.id,
                    Reference.id.in_(ref_ids),
                )
            ):
                labels[r.id] = _citation_snapshot(r.csl_json, r.citekey or r.title or r.filename)

        # One query for jobs; keep the latest per section_sha.
        latest_job: dict[str, ClaimAnalysisJob] = {}
        for j in session.scalars(
            select(ClaimAnalysisJob)
            .where(ClaimAnalysisJob.workspace_id == ws.id)
            .order_by(ClaimAnalysisJob.queued_at.desc(), ClaimAnalysisJob.id.desc())
        ):
            latest_job.setdefault(j.section_sha, j)

        out_sections: list[ClaimSectionOut] = []
        for sec in sections:
            # occurrence = count of earlier sections with the same sha (mirrors
            # the proven Jinja get_section_annotations mapping).
            occurrence = sum(1 for s in sections[: sec.index] if s.sha == sec.sha)
            group = by_group.get((sec.sha, occurrence), [])
            findings = [
                ClaimFindingOut(
                    id=a.id,
                    kind=a.kind,
                    claim_text=a.claim_text,
                    evidence_quote=a.evidence_quote,
                    ref_label=labels.get(a.reference_id, "reference"),
                    can_insert=(a.kind == "uncited_supported"),
                    sentence_ordinal=a.sentence_ordinal,
                )
                for a in group
            ]
            job = latest_job.get(sec.sha)
            if job is not None and job.status in _JOB_PENDING_STATUSES:
                job_out = ClaimJobOut(status="pending", error=None)
            elif job is not None and job.status == "failed":
                job_out = ClaimJobOut(status="failed", error=job.last_error or "")
            else:
                job_out = ClaimJobOut(status=None, error=None)
            out_sections.append(
                ClaimSectionOut(
                    index=sec.index,
                    heading=sec.heading,
                    section_sha=sec.sha,
                    occurrence=occurrence,
                    job=job_out,
                    findings=findings,
                )
            )
    return AnnotationsOut(is_owner=is_owner, sections=out_sections)


def _annotation_or_404(
    session: Session, ws: Workspace, annotation_id: int
) -> UserClaimAnnotation:
    ann = session.get(UserClaimAnnotation, annotation_id)
    if ann is None or ann.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="annotation not found")
    return ann


class InsertResultOut(BaseModel):
    ev_id: str | None = None
    citekey: str | None = None
    commit_sha: str | None = None
    idempotent: bool = False


@router.post(
    "/workspaces/{workspace_id}/annotations/{annotation_id}/insert",
    response_model=InsertResultOut,
)
async def insert_annotation(
    workspace_id: uuid.UUID,
    annotation_id: int,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> InsertResultOut:
    """Insert the citation an uncited_supported annotation suggests. Owner-only.

    Flag-on:  routes through the live Yjs room via insert_annotation_via_kernel
              (the bespoke, occurrence-aware core — see apply_annotation_insert_
              to_room's docstring for why it does not reuse the generic citation
              kernel's compute).
    Flag-off: the canonical git write path (insert_from_annotation), byte-for-
              byte, threadpooled.
    On a genuine (non-idempotent) insert, either branch re-enqueues analysis
    for the now-changed section (Tier-2 immediacy parity).
    """
    settings = request.app.state.settings
    sf = request.app.state.session_factory

    def _route() -> bool:
        with sf() as session:
            return collab_state.is_collab_enabled(
                session, workspace_id=workspace_id, artifact="draft"
            )

    enabled = await run_in_threadpool(_route)

    if enabled:
        def _gate() -> FenceStamp:
            with sf() as session:
                ws = require_owner(workspace_id=workspace_id, user=user, session=session)
                _annotation_or_404(session, ws, annotation_id)
                # Mint the fence stamp at the owner access check (endgame Task 5):
                # apply_annotation_insert_to_room re-validates THIS generation under
                # the commit mutex, in a separate session, never a read at the write.
                return stamp_for_workspace(ws)
        stamp = await run_in_threadpool(_gate)
        try:
            out = await insert_annotation_via_kernel(
                session_factory=sf,
                workspaces_dir=settings.workspaces_dir,
                stamp=stamp,
                workspace_id=workspace_id,
                actor_id=user.id,
                annotation_id=annotation_id,
            )
        except InsertConflict as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except CommitMutexTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        except RoomEvicted as e:
            raise HTTPException(
                status_code=409, detail="the live session was reset — retry"
            ) from e
        except GitCommitError as e:
            raise HTTPException(
                status_code=500, detail="insert failed — nothing was written"
            ) from e
        except IndexError as e:
            # Out-of-range section_index (e.g. a peer deleted the section
            # mid-insert). Parity with the flag-off path (insert_from_annotation
            # -> InsertConflict 409) — a reachable concurrent-edit race must be
            # a 4xx, not a 500.
            raise HTTPException(
                status_code=409, detail="this section changed — reopen and re-cite"
            ) from e

        if out.get("idempotent"):
            return InsertResultOut(idempotent=True)

        def _enqueue() -> None:
            with sf() as session:
                enqueue_containing_section(
                    session,
                    workspace_id=workspace_id,
                    workspaces_dir=settings.workspaces_dir,
                    commit_sha=out["commit_sha"],
                    ev_id=out["ev_id"],
                )
        await run_in_threadpool(_enqueue)
        return InsertResultOut(
            ev_id=out["ev_id"], citekey=out["citekey"], commit_sha=out["commit_sha"]
        )

    # Flag-off: the CURRENT handler body, byte-for-byte, threadpooled.
    def _git() -> InsertResultOut:
        with sf() as session:
            ws = require_owner(workspace_id=workspace_id, user=user, session=session)
            ann = _annotation_or_404(session, ws, annotation_id)
            try:
                result = insert_from_annotation(
                    session=session,
                    workspace=ws,
                    user_id=user.id,
                    annotation=ann,
                    workspaces_dir=settings.workspaces_dir,
                )
                session.commit()
            except collab_state.CollabSeveranceConflict as e:
                # enable-on-touch flipped draft collab on AFTER _route() read False
                # but while insert_from_annotation held the mutex — abort writing
                # NOTHING; same 409 as the save routes' severance guard.
                session.rollback()
                raise HTTPException(
                    status_code=409, detail=collab_state.COLLAB_SEVERED_DETAIL
                ) from e
            except InsertConflict as e:
                session.rollback()
                raise HTTPException(status_code=409, detail=str(e)) from e
            except CommitMutexTimeout as e:
                session.rollback()
                # /api sanitizes >=500 -> remap the 503 busy signal to a retriable 429.
                raise HTTPException(
                    status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
                ) from e
            except GitCommitError as e:
                session.rollback()
                raise HTTPException(
                    status_code=500, detail="insert failed — nothing was written"
                ) from e

            if "idempotent" not in result:
                enqueue_containing_section(
                    session,
                    workspace_id=workspace_id,
                    workspaces_dir=settings.workspaces_dir,
                    commit_sha=result["commit_sha"],
                    ev_id=result["ev_id"],
                )
                return InsertResultOut(
                    ev_id=result["ev_id"],
                    citekey=result["citekey"],
                    commit_sha=result["commit_sha"],
                )
        return InsertResultOut(idempotent=True)

    return await run_in_threadpool(_git)


class DismissReasonIn(BaseModel):
    reason: str


class DismissResultOut(BaseModel):
    dismissed: bool


@router.post(
    "/workspaces/{workspace_id}/annotations/{annotation_id}/dismiss",
    response_model=DismissResultOut,
)
def dismiss_annotation(
    workspace_id: uuid.UUID,
    annotation_id: int,
    body: DismissReasonIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> DismissResultOut:
    """Dismiss an open annotation with a required, audited reason. Owner-only.
    Guarded UPDATE WHERE status='open' (race -> 409) + user_claim_dismissed event."""
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        ann = _annotation_or_404(session, ws, annotation_id)
        reason = body.reason.strip()
        if not reason:
            raise HTTPException(status_code=422, detail="a reason is required")
        updated = (
            session.query(UserClaimAnnotation)
            .filter(
                UserClaimAnnotation.id == ann.id,
                UserClaimAnnotation.status == "open",
            )
            .update(
                {
                    "status": "dismissed",
                    "dismissed_reason": reason,
                    "resolved_by_user_id": user.id,
                    "resolved_at": datetime.now(UTC),
                },
                synchronize_session=False,
            )
        )
        if not updated:
            session.rollback()
            raise HTTPException(status_code=409, detail="annotation is no longer actionable")
        session.add(
            WorkspaceEvent(
                workspace_id=ws.id,
                kind="user_claim_dismissed",
                actor_id=user.id,
                payload={"annotation_id": ann.id, "reason": reason},
            )
        )
        session.commit()
    return DismissResultOut(dismissed=True)


class ReanalyzeResultOut(BaseModel):
    enqueued: bool


@router.post(
    "/workspaces/{workspace_id}/sections/{n}/reanalyze",
    response_model=ReanalyzeResultOut,
)
def reanalyze_section(
    workspace_id: uuid.UUID,
    n: int,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> ReanalyzeResultOut:
    """Re-enqueue claim analysis for the section's LIVE content at HEAD.
    Owner-only. 404 if n is out of range or there is no draft yet."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        repo = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        proposal = repo / PROPOSAL_FILENAME
        if not proposal.exists():
            raise HTTPException(status_code=404, detail="no such section")
        try:
            head = current_head(repo)
        except GitCommitError as e:
            raise HTTPException(status_code=404, detail="no such section") from e
        sections = split_sections(proposal.read_text())
        if n < 0 or n >= len(sections):
            raise HTTPException(status_code=404, detail="no such section")
        sec = sections[n]
        enqueued = enqueue_section_job(
            session,
            workspace_id=ws.id,
            section_index=sec.index,
            section_heading=sec.heading,
            section_sha=sec.sha,
            commit_sha=head,
        )
        session.commit()
    return ReanalyzeResultOut(enqueued=enqueued)
