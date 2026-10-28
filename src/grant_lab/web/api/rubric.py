"""JSON /api surface for the funder rubric + setup gate (Cockpit Slice E).

GET rubric / activate / PUT rubric are OWNER-gated; GET setup-status is member-
gated (last_error owner-only). Replaces the Jinja rubric-review + setup-status
routes (deleted in this slice). No DB schema change.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from grant_lab.auth.dependencies import get_current_user
from grant_lab.db.models import User, WorkspaceEvent, WorkspaceSetupJob
from grant_lab.proposal_assist.collab.permissions import signal_from_request
from grant_lab.proposal_assist.ports.stamp import stamp_for_workspace
from grant_lab.proposal_assist.row_locks import (
    SETUP_JOB_LOCK_TIMEOUT_SECONDS,
    WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS,
)
from grant_lab.proposal_assist.web._perms import require_member, require_owner
from grant_lab.proposal_assist.web._writes import workspace_write

router = APIRouter()


class CriterionOut(BaseModel):
    # extra="allow" round-trips passthrough fields (e.g. subcriteria) the editor
    # must not drop. Serialization includes them; OpenAPI emits additionalProperties.
    model_config = ConfigDict(extra="allow")
    name: str
    description: str
    weight: float
    scale_max: int


class RubricOut(BaseModel):
    criteria: list[CriterionOut]
    funder_notes: str | None
    updated_at: object  # a datetime; FastAPI serializes it to ISO at runtime


class SetupStatusOut(BaseModel):
    status: str
    last_error: str | None


def _rubric_out(ws) -> RubricOut:
    return RubricOut(
        criteria=[CriterionOut(**c) for c in (ws.rubric_criteria or [])],
        funder_notes=ws.rubric_funder_notes,
        updated_at=ws.rubric_updated_at,
    )


@router.get("/workspaces/{workspace_id}/rubric", response_model=RubricOut)
def get_rubric(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> RubricOut:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        return _rubric_out(ws)


@router.get("/workspaces/{workspace_id}/setup-status", response_model=SetupStatusOut)
def get_setup_status(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SetupStatusOut:
    with request.app.state.session_factory() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        job = session.scalar(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws.id)
        )
        is_owner = ws.owner_id == user.id
        return SetupStatusOut(
            status=job.status if job else "queued",
            last_error=(job.last_error if job else None) if is_owner else None,
        )


class CriterionIn(BaseModel):
    # extra="allow" carries subcriteria (and any unknown field) through unchanged.
    model_config = ConfigDict(extra="allow")
    name: str
    description: str
    # allow_inf_nan=False: _validate_criteria's semantic check is `weight < 0`,
    # and NaN fails every comparison while Infinity passes that one — so both
    # reached the JSONB write, where Postgres refuses them
    # (`InvalidTextRepresentation: Token "NaN" is invalid`) and the OWNER got a
    # 500 on their own rubric save. Python's json accepts the non-standard NaN /
    # Infinity literals and Starlette parses bodies with it, so a client really
    # can send them. A malformed number is the body's problem, so it is refused
    # as a 422 at the edge (review 2026-10-26, carried open from 10-06).
    #
    # Deliberately NOT applied to CriterionOut: that model is built from rows
    # already in the database, and constraining it would turn any pre-existing
    # bad value into a 500 on READ instead of a 422 on WRITE.
    weight: float = Field(allow_inf_nan=False)
    scale_max: int


class RubricUpdateIn(BaseModel):
    criteria: list[CriterionIn]
    funder_notes: str | None = None


def _validate_criteria(criteria: list[CriterionIn]) -> list[dict]:
    # Semantic validation -> 400 bad_request (parity with the Jinja form). Returns
    # plain dicts that PRESERVE passthrough fields (model_dump keeps extras).
    if not criteria:
        raise HTTPException(status_code=400, detail="rubric must have at least one criterion")
    out: list[dict] = []
    for i, c in enumerate(criteria):
        name = c.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail=f"criterion {i}: name required")
        if c.weight < 0:
            raise HTTPException(status_code=400, detail=f"criterion {i}: weight must be >= 0")
        if not (1 <= c.scale_max <= 10):
            raise HTTPException(status_code=400, detail=f"criterion {i}: scale_max must be 1..10")
        d = c.model_dump()
        d["name"] = name
        out.append(d)
    return out


def _apply_rubric(session, ws, user, criteria: list[dict], funder_notes: str) -> None:
    # Single saver for both activate and PUT. Writes rubric_edited ONLY on real
    # change. NOTE: we set actor_id=user.id (cleaner than the Jinja path, which
    # left actor_id NULL and stored payload['edited_by_user_id']); activity's
    # actor_user_id() prefers actor_id, so this resolves the editor correctly.
    old_c = ws.rubric_criteria
    old_n = ws.rubric_funder_notes or ""
    content_changed = criteria != old_c or funder_notes != old_n
    ws.rubric_criteria = criteria
    ws.rubric_funder_notes = funder_notes
    if content_changed:
        ws.rubric_updated_at = datetime.now(UTC)
        session.add(
            WorkspaceEvent(
                workspace_id=ws.id,
                kind="rubric_edited",
                actor_id=user.id,
                payload={
                    "old_criteria": old_c,
                    "new_criteria": criteria,
                    "old_funder_notes": old_n,
                    "new_funder_notes": funder_notes,
                },
            )
        )


@router.put("/workspaces/{workspace_id}/rubric", response_model=RubricOut)
def update_rubric(
    workspace_id: uuid.UUID,
    body: RubricUpdateIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> RubricOut:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        # Semantic validation runs AFTER require_owner. On master it ran before
        # the session opened, so a non-owner posting a malformed rubric got 400 —
        # telling someone with no access that their body shape was wrong.
        # Authorization decides first. It stays BEFORE the mutex because it takes
        # no lock: a malformed body must not queue behind the commit mutex.
        criteria = _validate_criteria(body.criteria)
        # Stamp minted at the access check (require_owner), before the mutex.
        stamp = stamp_for_workspace(ws)
        # Slice C2 lock-order rule: the write scope opens immediately after
        # require_owner and its lock-free validation, before any other lock.
        # WorkspaceDeleted -> main.py -> 404. workspace_write bounds every lock
        # wait in the rest of the transaction and commits inside that bound:
        # _apply_rubric UPDATEs the `workspaces` row and the re-read below takes
        # it explicitly, so a wait can land at either end, and an untranslated
        # 55P03 would answer 500 where this route contracts for 429. The holder
        # is real — the four pass prefixes take that row FOR UPDATE holding NO
        # mutex, so the mutex we hold does not exclude them.
        # WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS, not SETUP_JOB_LOCK_TIMEOUT_SECONDS:
        # this route never touches a WorkspaceSetupJob row.
        with workspace_write(
            session,
            stamp=stamp,
            timeout_seconds=WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS,
            what="rubric-update",
        ):
            # Re-read the workspace row UNDER the mutex, BEFORE _apply_rubric
            # reads it. require_owner loaded `ws` before the mutex, and this
            # request may then have waited up to 10s there while another
            # writer changed the rubric and committed. Nothing refreshed the
            # instance in the meantime: acquire_commit_mutex_live's freshness
            # guard is a scalar SELECT of soft_deleted_at ONLY (it repopulates
            # no attribute), and db/session.py builds the sessionmaker with
            # expire_on_commit=False.
            #
            # It matters here because _apply_rubric derives content_changed
            # and the rubric_edited event's old_criteria/old_funder_notes from
            # these attributes. Stale, an identical concurrent write records a
            # SECOND event for one edit, and a distinct one records old_* from
            # a rubric two versions back, breaking the audit chain.
            #
            # session.refresh, NOT session.get(..., with_for_update=True): with
            # `get`, a non-None with_for_update skips the identity lookup and
            # emits the SELECT ... FOR UPDATE, so the row lock is taken — but
            # the loader does not overwrite attributes that are ALREADY
            # populated, so the stale values survive the round trip (verified
            # against SQLAlchemy 2.0.49; `get` needs populate_existing=True to
            # refresh). Do not "simplify" this into that form; the two
            # identical-concurrent-write gates in
            # tests/proposal_assist/test_api_writers_mutex_straddle.py fail if
            # you do.
            #
            # Lock order is unchanged (M -> W): _apply_rubric's UPDATE already
            # took this row at flush; with_for_update only takes it earlier and
            # explicitly. The wait sits inside the managed bound, so its 55P03
            # is translated to this route's contracted 429 like every other wait
            # in this transaction.
            session.refresh(ws, with_for_update=True)
            _apply_rubric(session, ws, user, criteria, body.funder_notes or "")
        # No SQL: make_session_factory sets expire_on_commit=False (db/session.py),
        # so the commit does not expire `ws` and these are loaded attributes.
        return _rubric_out(ws)


class SetupRetryOut(BaseModel):
    status: str
    attempts: int


@router.post("/workspaces/{workspace_id}/setup/retry", response_model=SetupRetryOut)
def retry_setup(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SetupRetryOut:
    """Owner-only: re-queue a permanently-failed Phase-0 setup job.

    Resets status='queued', attempts=0, clears started_at/completed_at/last_error
    so the next scheduler tick re-claims it. Returns 404 if no failed job exists
    for this workspace; returns 409 if the job is already queued or running
    (not yet permanently failed). 404 also if the workspace was deleted while
    this request waited on the commit mutex; 429 if the mutex or the setup-job
    row could not be taken in time."""
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        # Stamp minted at the access check (require_owner), before the mutex.
        stamp = stamp_for_workspace(ws)
        # Slice C2 lock-order rule: the write scope opens immediately after
        # require_owner and BEFORE the setup-job row lock below. Order matters
        # here for a concrete reason: a route that took the job row first would
        # hold it for the whole mutex wait, and the delete's retirement hook
        # cancels setup jobs with FOR UPDATE SKIP LOCKED — so it would skip this
        # row and leave the job non-terminal until the reconciliation tick.
        # Opening the scope first means we raise WorkspaceDeleted (-> main.py ->
        # 404) without ever touching the row.
        #
        # This route also has its own resurrection shape, though a narrower one
        # than it looks: it cannot revive a job the delete cancelled, because it
        # refuses anything whose status is not exactly 'failed' (below) and the
        # delete writes 'cancelled'. What it CAN do is re-queue a job that was
        # already 'failed' when the workspace was deleted — 'failed' is terminal
        # and therefore not in the hook's _CANCELLABLE list, so the delete leaves
        # it alone. The under-mutex read is what stops that.
        #
        # The 404 and 409 below are raised INSIDE the scope. workspace_write lets
        # an HTTPException through untouched and commits nothing, so neither
        # early exit writes.
        with workspace_write(
            session,
            stamp=stamp,
            timeout_seconds=SETUP_JOB_LOCK_TIMEOUT_SECONDS,
            what="setup-retry",
        ):
            job = session.scalar(
                select(WorkspaceSetupJob)
                .where(WorkspaceSetupJob.workspace_id == ws.id)
                .with_for_update()
            )
            if job is None or job.status != "failed":
                if job is not None and job.status in ("queued", "running"):
                    raise HTTPException(
                        status_code=409,
                        detail="setup job is already queued or running",
                    )
                raise HTTPException(
                    status_code=404,
                    detail="no failed setup job for this workspace",
                )
            job.status = "queued"
            job.attempts = 0
            job.started_at = None
            job.completed_at = None
            job.last_error = None
        return SetupRetryOut(status=job.status, attempts=job.attempts)


@router.post("/workspaces/{workspace_id}/activate", response_model=RubricOut)
def activate(
    workspace_id: uuid.UUID,
    body: RubricUpdateIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> RubricOut:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        # Semantic validation AFTER require_owner (see update_rubric's note): a
        # non-owner with a malformed rubric gets 403, not a 400 confirming the
        # body shape. Before the mutex, because it takes no lock.
        criteria = _validate_criteria(body.criteria)
        # Stamp minted at the access check (require_owner), before the mutex.
        stamp = stamp_for_workspace(ws)
        # Slice C2 lock-order rule: the write scope opens immediately after
        # require_owner and BEFORE the setup-job row lock below. Taking the job
        # row first would hold it across the whole mutex wait, and the delete's
        # retirement hook cancels setup jobs with FOR UPDATE SKIP LOCKED — it
        # would skip this row and leave the job non-terminal until the
        # reconciliation tick.
        #
        # workspace_write bounds the WHOLE remainder including COMMIT. It matters
        # more here than on retry_setup: the re-read below takes the `workspaces`
        # row FOR UPDATE and _apply_rubric UPDATEs it, while the four pass
        # prefixes hold that row FOR UPDATE for up to
        # PASS_PREFIX_LOCK_TIMEOUT_SECONDS holding no mutex, so this transaction
        # really can block after the job lock.
        with workspace_write(
            session,
            stamp=stamp,
            timeout_seconds=SETUP_JOB_LOCK_TIMEOUT_SECONDS,
            what="activate",
        ):
            job = session.scalar(
                select(WorkspaceSetupJob)
                .where(WorkspaceSetupJob.workspace_id == ws.id)
                .with_for_update()
            )
            if job is None or job.status != "waiting_for_rubric_review":
                raise HTTPException(
                    status_code=409, detail="workspace is not awaiting rubric review"
                )
            # Re-read the workspace row under the mutex before _apply_rubric
            # reads it — same defect, same fix, full rationale on
            # update_rubric above (including why session.refresh and not
            # session.get(..., with_for_update=True)).
            #
            # It is worse on this route: phases/setup.py's _m3 WRITES the
            # rubric while holding job -> workspace, so the rubric activate
            # loaded at require_owner can have been replaced by the setup
            # worker while activate queued on the mutex — the very transition
            # this route is confirming.
            #
            # AFTER the job lock, deliberately. The normative order is
            # M -> J -> W and it matches _m3's; hoisting this above the job
            # SELECT would invert against _m3 and open a real deadlock shape
            # (Postgres 40P01). Nothing is lost by taking it here: the 409 path
            # above returns without ever needing the row.
            session.refresh(ws, with_for_update=True)
            _apply_rubric(session, ws, user, criteria, body.funder_notes or "")
            job.status = "done"
            job.completed_at = datetime.now(UTC)
            ws.current_phase = "active"
            session.add(
                WorkspaceEvent(
                    workspace_id=ws.id,
                    kind="workspace_activated",
                    actor_id=user.id,
                    payload={},
                )
            )
        result = _rubric_out(ws)  # build INSIDE the session block (still open)
    # setup gates BOTH artifacts to owner-only (resolve_access), so activating
    # lifts the write-gate for the draft AND an unlocked spec — reconcile both.
    signal_from_request(request, workspace_id=workspace_id, artifacts=["draft", "spec"])
    return result
