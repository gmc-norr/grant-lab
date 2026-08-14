"""JSON /api surface for the funder rubric + setup gate (Cockpit Slice E).

GET rubric / activate / PUT rubric are OWNER-gated; GET setup-status is member-
gated (last_error owner-only). Replaces the Jinja rubric-review + setup-status
routes (deleted in this slice). No DB schema change.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import User, WorkspaceEvent, WorkspaceSetupJob
from call_manager.proposal_assist.collab.permissions import signal_from_request
from call_manager.proposal_assist.drafting.locking import (
    BUSY_ERROR_MESSAGE,
    CommitMutexTimeout,
    acquire_commit_mutex_live,
)
from call_manager.proposal_assist.row_locks import (
    SETUP_JOB_LOCK_TIMEOUT_SECONDS,
    WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS,
    RowLockTimeout,
    bounded_lock_waits,
)
from call_manager.proposal_assist.web._perms import require_member, require_owner

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
    weight: float
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
        # Slice C2 lock-order rule: the live mutex immediately after
        # require_owner and its lock-free validation, before any other lock.
        # WorkspaceDeleted -> main.py -> 404.
        try:
            acquire_commit_mutex_live(session, ws.id)
        except CommitMutexTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        # The workspace-row wait is BOUNDED. _apply_rubric UPDATEs the
        # `workspaces` row, and acquire_commit_mutex restores
        # lock_timeout = DEFAULT on success (locking.py), so without this the
        # route would wait indefinitely on that row WHILE HOLDING the commit
        # mutex — and a concurrent DELETE, every collab-kernel save and every
        # turn enqueue would 429 behind us after burning their full budget. The
        # holder is real: the four pass prefixes take this row FOR UPDATE while
        # holding NO mutex, so the mutex we hold does not exclude them.
        # WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS, not SETUP_JOB_LOCK_TIMEOUT_SECONDS:
        # this route never touches a WorkspaceSetupJob row.
        #
        # The bound wraps the WHOLE remainder including COMMIT — the under-mutex
        # re-read below takes this row explicitly and _apply_rubric's UPDATE
        # flushes into it at commit, so a wait can land at either end — for the
        # reason spelled out on activate below: a narrow wrap leaves the write
        # bounded but UNtranslated, and 55P03 escapes as a raw OperationalError
        # -> 500 instead of this route's contracted 429.
        try:
            with bounded_lock_waits(
                session,
                timeout_seconds=WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS,
                message="timed out waiting for a row lock in the rubric-update transaction",
            ):
                # Re-read the workspace row UNDER the mutex, BEFORE _apply_rubric
                # reads it. require_owner loaded `ws` before the mutex, and this
                # request may then have waited up to 10s there while another
                # writer changed the rubric and committed. Nothing refreshed the
                # instance in the meantime: acquire_commit_mutex_live's freshness
                # guard is a scalar SELECT of soft_deleted_at ONLY (it repopulates
                # no attribute), and db/session.py builds the sessionmaker with
                # expire_on_commit=False. The irony is worth stating plainly —
                # that helper's own docstring names this exact hazard ("can return
                # the identity-map instance the caller loaded before blocking on
                # the mutex, which is precisely the stale state this guard exists
                # to defeat") and then defeats it for one column, and these two
                # routes walked into it on the others.
                #
                # It matters here because _apply_rubric derives content_changed
                # and the rubric_edited event's old_criteria/old_funder_notes from
                # these attributes. Stale, an identical concurrent write records a
                # SECOND event for one edit, and a distinct one records old_* from
                # a rubric two versions back, breaking the audit chain. Before this
                # slice the read-to-write window was a few milliseconds; C2 made it
                # the whole mutex wait, so this slice owns the fix.
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
                # explicitly. The wait sits inside the bound, so its 55P03 is
                # translated to this route's contracted 429 like every other wait
                # in this transaction.
                session.refresh(ws, with_for_update=True)
                _apply_rubric(session, ws, user, criteria, body.funder_notes or "")
                session.commit()
        except RowLockTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
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
        # Slice C2 lock-order rule: the live mutex immediately after require_owner
        # and BEFORE the setup-job row lock below. Order matters here for a
        # concrete reason: a route that took the job row first would hold it for
        # the whole mutex wait, and the delete's retirement hook cancels setup
        # jobs with FOR UPDATE SKIP LOCKED — so it would skip this row and leave
        # the job non-terminal until the reconciliation tick. Taking the mutex
        # first means we raise WorkspaceDeleted (-> main.py -> 404) without ever
        # touching the row.
        #
        # This route also has its own resurrection shape, though a narrower one
        # than it looks: it cannot revive a job the delete cancelled, because it
        # refuses anything whose status is not exactly 'failed' (below) and the
        # delete writes 'cancelled'. What it CAN do is re-queue a job that was
        # already 'failed' when the workspace was deleted — 'failed' is terminal
        # and therefore not in the hook's _CANCELLABLE list, so the delete leaves
        # it alone. The under-mutex read is what stops that.
        try:
            acquire_commit_mutex_live(session, ws.id)
        except CommitMutexTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        # The job-row wait is BOUNDED. acquire_commit_mutex restores
        # lock_timeout = DEFAULT on success (locking.py:204), so without this the
        # route would hold the commit mutex while waiting indefinitely on a row
        # that _m3, _fail or requeue_stuck_setup_jobs may hold — and a concurrent
        # DELETE would 429 behind us after burning its whole 10s budget. That is
        # the unbounded-wait class Slice C1 closed for the four pass prefixes;
        # re-opening it here on the assumption "those holders are short" is
        # exactly the unwritten assumption that produced it the first time.
        # The bound wraps the WHOLE remainder of the transaction, COMMIT included
        # — not just the FOR UPDATE. bounded_lock_waits deliberately leaves
        # SET LOCAL lock_timeout in force on exit (row_locks.py's module
        # docstring: the bound "must extend past the acquisitions to the
        # commit"), but its 55P03 -> RowLockTimeout translation only covers its
        # own body. A narrow wrap would leave the job UPDATE and the commit
        # bounded yet UNtranslated, so a lock wait there escapes as a raw
        # OperationalError -> 500 instead of this route's contracted 429. The
        # four pass runners and phases/setup.py wrap the whole transaction for
        # exactly this reason.
        try:
            with bounded_lock_waits(
                session,
                timeout_seconds=SETUP_JOB_LOCK_TIMEOUT_SECONDS,
                message="timed out waiting for a row lock in the setup-retry transaction",
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
                session.commit()
        except RowLockTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
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
        # Slice C2 lock-order rule: the live mutex immediately after require_owner
        # and BEFORE the setup-job row lock below. Taking the job row first would
        # hold it across the whole mutex wait, and the delete's retirement hook
        # cancels setup jobs with FOR UPDATE SKIP LOCKED — it would skip this row
        # and leave the job non-terminal until the reconciliation tick.
        try:
            acquire_commit_mutex_live(session, ws.id)
        except CommitMutexTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        # Bounded, for the same reason as retry_setup: acquire_commit_mutex
        # restores lock_timeout = DEFAULT, so an unbounded row wait here would
        # pin the commit mutex and make a concurrent DELETE 429 after its full
        # budget. See row_locks.py's module docstring.
        #
        # The bound wraps the WHOLE remainder of the transaction, COMMIT included
        # — see retry_setup's note. It matters more here than there: the
        # under-mutex re-read below takes the `workspaces` row FOR UPDATE and
        # _apply_rubric UPDATEs it, while the four pass prefixes hold that row
        # FOR UPDATE for up to PASS_PREFIX_LOCK_TIMEOUT_SECONDS holding no
        # mutex, so this transaction really can block after the job lock. With a
        # narrow wrap that wait is still bounded but no longer translated, and
        # the caller gets a 500 where this slice contracts for a 429.
        try:
            with bounded_lock_waits(
                session,
                timeout_seconds=SETUP_JOB_LOCK_TIMEOUT_SECONDS,
                message="timed out waiting for a row lock in the activate transaction",
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
                # AFTER the job lock, deliberately. The normative order on this
                # branch is M -> J -> W and it matches _m3's; hoisting this above
                # the job SELECT would invert against _m3 and open a real deadlock
                # shape (Postgres 40P01). Nothing is lost by taking it here: the
                # 409 path above returns without ever needing the row.
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
                session.commit()
        except RowLockTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        result = _rubric_out(ws)   # build INSIDE the block (session still open)
    # setup gates BOTH artifacts to owner-only (resolve_access), so activating
    # lifts the write-gate for the draft AND an unlocked spec — reconcile both.
    signal_from_request(request, workspace_id=workspace_id, artifacts=["draft", "spec"])
    return result
