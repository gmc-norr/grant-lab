"""Workspace lifecycle service functions: `create_workspace`,
`soft_delete_workspace_db` and `scrub_workspace_bytes`.

`create_workspace` does three things in one
unit of work:

  1. Insert a Workspace row.
  2. Write a workspace_events(kind='created') audit row.
  3. Enqueue a workspace_setup_jobs row so the Phase 0 worker
     (src/call_manager/proposal_assist/phases/setup.py) snapshots the
     call body, extracts the section + rubric templates, and yields at
     waiting_for_rubric_review for the workspace owner to confirm.

The caller commits. If any of the three fails, the caller rolls back and
no partial state lands.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from call_manager.db.models import TurnJob, Workspace, WorkspaceEvent, WorkspaceSetupJob
from call_manager.proposal_assist.attachments.storage import AttachmentStorage
from call_manager.proposal_assist.coherence.jobs import cancel_coherence_jobs_for_workspace
from call_manager.proposal_assist.drafting.locking import (
    acquire_commit_mutex,
    acquire_commit_mutex_live,
)
from call_manager.proposal_assist.evaluator.jobs import cancel_evaluator_jobs_for_workspace
from call_manager.proposal_assist.references.storage import ReferenceStorage
from call_manager.proposal_assist.review.jobs import cancel_review_runs_and_items_for_workspace
from call_manager.proposal_assist.turns.outcomes import record_terminal_outcome
from call_manager.proposal_assist.uncited_claims.jobs import (
    cancel_uncited_claims_jobs_for_workspace,
)


class RestoreOutcome(NamedTuple):
    generation: int
    setup_job_restored_to: str | None
    transitioned: bool  # False when the workspace was already live


# Lock order: mutex (M) → setup job (J) → workspace (W).
# See phases/setup.py:500 and web/api/rubric.py:341 for the canonical order.
_SETUP_RESTORE_MAP = {
    "queued": "queued",
    # NOT 'running'. See spec 5.2.1: the worker that owned it died with the
    # deletion, nothing recreates it, and requeue_stuck_setup_jobs will not
    # touch a 'running' row for STUCK_SETUP_AFTER (30 minutes). Restoring to
    # 'running' reproduces the stuck state. The residual double-claim: accepted
    # because the workspace WAS deleted, so any live worker has already failed
    # its liveness check or will at its next milestone — and with Task 2's _fail
    # fix that worker now re-queues rather than cancelling.
    "running": "queued",
    # Preserved exactly: extraction is DONE and a human is the next actor.
    # Re-queueing re-runs extraction over completed work.
    "waiting_for_rubric_review": "waiting_for_rubric_review",
}


def create_workspace(
    *,
    session: Session,
    owner_id: int,
    title: str,
    call_id: int | None = None,
    profile_id: int | None = None,
) -> Workspace:
    """Create a new workspace, write the 'created' audit event, and enqueue
    the Phase 0 setup job. Returns the Workspace (with `.id` populated via
    session.flush())."""
    ws = Workspace(
        owner_id=owner_id,
        title=title,
        call_id=call_id,
        profile_id=profile_id,
    )
    session.add(ws)
    session.flush()  # populates ws.id from the DB default

    session.add(
        WorkspaceEvent(
            workspace_id=ws.id,
            kind="created",
            payload={
                "title": title,
                "call_id": call_id,
                "profile_id": profile_id,
            },
            actor_id=owner_id,
        )
    )
    session.add(
        WorkspaceSetupJob(
            workspace_id=ws.id,
            status="queued",
        )
    )
    return ws


def bump_lifecycle_generation(session: Session, *, workspace_id: uuid.UUID) -> int:
    """Allocate the next lifecycle generation for *workspace_id* and return it.

    Computed as `lifecycle_generation + 1` IN THE DATABASE, never from a value
    read into Python first: two transitions that both read the old value would
    otherwise write the same new one, and two transitions sharing a generation
    makes staleness undetectable — which is the whole mechanism.

    Does NOT commit. The caller must already hold the commit mutex on this
    workspace, so that the mutex is what orders generations (spec 4.4).
    """
    return session.execute(
        update(Workspace)
        .where(Workspace.id == workspace_id)
        .values(lifecycle_generation=Workspace.lifecycle_generation + 1)
        .returning(Workspace.lifecycle_generation)
    ).scalar_one()


def soft_delete_workspace_db(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    timeout_seconds: int = 10,
) -> int:
    """The DB half of a soft-delete: mark the workspace deleted, cancel every
    active background job + queued turn so none can burn cloud tokens after
    deletion, terminalize any RUNNING turn, and COMMIT.

    Returns the lifecycle_generation allocated for this transition — the value
    bumped inside the same transaction as the state change, under the commit
    mutex (spec 4.4). Do NOT re-read it after commit: the return value is bound
    to this transition, not to when the sampler ran.

    The delete route interleaves in-process retirement after this durable DB
    commit: DB commit -> retire -> 204. There is deliberately no wrapper
    composing this with scrub_workspace_bytes — one existed until Slice C2 and
    was deleted, because it looked like the entry point for deleting a
    workspace while skipping retirement entirely.

    The caller resolves ownership (require_owner) before calling this.

    Takes the per-workspace commit mutex for the whole transaction: raises
    CommitMutexTimeout (route → 429) if a writer is wedged, having committed
    nothing, and WorkspaceDeleted (route → 404) if the workspace was already
    deleted — the documented non-idempotent re-delete contract.
    """
    ws = session.get(Workspace, workspace_id)
    if ws is None:
        # The caller resolves ownership (require_owner) first, so the row must
        # exist; a None here means the contract was bypassed — fail loudly rather
        # than silently skipping the soft_deleted_at stamp while cancelling jobs.
        raise ValueError(f"workspace not found: {workspace_id}")
    # Take the per-workspace commit mutex for this whole transaction, BEFORE any
    # mutation. Mutex-participating writers call acquire_commit_mutex_live, which
    # re-reads soft_deleted_at once it wins this lock — so holding it here is what
    # turns their check into a real linearization point rather than a hope.
    #
    # _live, not the plain acquire: `ws` above was loaded BEFORE we could block on
    # the mutex, so a concurrent deletion that committed while we waited would be
    # invisible to it and we would re-stamp soft_deleted_at off a stale instance
    # and report a second success. The fresh under-mutex read raises
    # WorkspaceDeleted instead — which is exactly the route's documented
    # re-delete 404.
    acquire_commit_mutex_live(session, workspace_id, timeout_seconds=timeout_seconds)
    ws.soft_deleted_at = datetime.now(UTC)
    # Fail any active (queued|running) background pass so it can't claim and burn
    # cloud tokens after the workspace is gone (each claim_next also excludes
    # soft-deleted workspaces; this also unwedges the active-unique slot). Same txn.
    cancel_coherence_jobs_for_workspace(session, workspace_id=workspace_id)
    cancel_evaluator_jobs_for_workspace(session, workspace_id=workspace_id)
    cancel_review_runs_and_items_for_workspace(session, workspace_id=workspace_id)
    cancel_uncited_claims_jobs_for_workspace(session, workspace_id=workspace_id)
    # Cancel any QUEUED turns so a deletion-race turn can't dispatch. Route through
    # the helper for audit parity (closes each cloud_turn_sent chain), but
    # emit_chat=False — the workspace is 404 the instant it's soft-deleted, so a
    # viewer-safe row would be dead-on-arrival. 'cancelled' (owner-initiated,
    # never started), not 'failed'.
    queued = session.execute(
        select(TurnJob.id).where(
            TurnJob.workspace_id == workspace_id,
            TurnJob.status == "queued",
        )
    ).all()
    for row in queued:
        record_terminal_outcome(
            session,
            turn_job_id=row.id,
            workspace_id=workspace_id,
            outcome="cancelled",
            expected_status="queued",
            raw_error="workspace soft-deleted",
            emit_chat=False,
        )
    # Terminalize any RUNNING turn in THIS transaction. The processor's
    # success-finalize is a conditional UPDATE ... WHERE status='running', so once
    # this commits both paths contend on the same row and the finalize becomes a
    # no-op — closing the delete-vs-finalize race durably, with no in-process
    # recheck. usage_unknown=True keeps the submit-time estimate: the turn HAS
    # consumed input we cannot measure yet, and stamp_post_terminal_usage replaces
    # it if the stream reports real usage.
    running = session.execute(
        select(TurnJob.id).where(
            TurnJob.workspace_id == workspace_id,
            TurnJob.status == "running",
        )
    ).all()
    for row in running:
        record_terminal_outcome(
            session,
            turn_job_id=row.id,
            workspace_id=workspace_id,
            outcome="cancelled",
            expected_status="running",
            raw_error="workspace soft-deleted",
            emit_chat=False,
            usage_unknown=True,
        )
    generation = bump_lifecycle_generation(session, workspace_id=workspace_id)
    session.commit()
    return generation


def restore_workspace_db(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    admin_id: int,
    timeout_seconds: int = 10,
) -> RestoreOutcome:
    """Clear soft_deleted_at, allocate the lifecycle generation, repair the setup
    job and write the audit event — all in ONE transaction, then COMMIT.

    Takes the plain commit mutex for the whole transaction, NEVER
    acquire_commit_mutex_live: the _live variant re-reads liveness and raises
    WorkspaceDeleted, which would refuse every workspace this function exists to
    serve. Restore is the one path that makes the mutex load-bearing rather than
    defensive — it FLIPS the liveness flag every other writer re-reads under that
    same mutex, which is what makes the flip a linearization point.

    Raises CommitMutexTimeout (route -> 429) having committed nothing.

    The returned generation is the one THIS transaction allocated. The caller
    passes it to signal_workspace_restored untouched and must never re-read one
    (spec 4.4).
    """
    # Step 1: Load the workspace row.
    ws = session.get(Workspace, workspace_id)
    if ws is None:
        raise ValueError(f"workspace not found: {workspace_id}")

    # Step 2: Acquire the plain commit mutex (NOT _live — _live raises
    # WorkspaceDeleted on deleted workspaces, which is every workspace this
    # function exists to serve). acquire_commit_mutex returns None.
    acquire_commit_mutex(session, workspace_id, timeout_seconds=timeout_seconds)

    # Step 3: Fresh under-mutex read of soft_deleted_at (plain SELECT, no FOR
    # UPDATE — we are not locking the workspace row yet; that comes in step 5).
    current_deleted_at = session.execute(
        select(Workspace.soft_deleted_at).where(Workspace.id == workspace_id)
    ).scalar_one_or_none()

    if current_deleted_at is None:
        # Already live — idempotent path. Read generation from the
        # identity-mapped instance (loaded in step 1) before rollback.
        gen = ws.lifecycle_generation
        session.rollback()
        return RestoreOutcome(generation=gen, setup_job_restored_to=None, transitioned=False)

    # Step 4: Lock and repair the setup job BEFORE mutating the workspace row.
    # Lock order: mutex (M) → setup job (J) → workspace (W).
    # See phases/setup.py:500 and web/api/rubric.py:341.
    jobs = session.execute(
        select(WorkspaceSetupJob)
        .where(WorkspaceSetupJob.workspace_id == workspace_id)
        .with_for_update()
    ).scalars().all()

    restored_to: str | None = None
    for job in jobs:
        source = job.cancelled_from if job.status == "cancelled" else job.status
        if source not in _SETUP_RESTORE_MAP:
            continue  # terminal (done/failed), or cancelled by something that
                      # was not a deletion (cancelled_from IS NULL) — not ours
        job.status = _SETUP_RESTORE_MAP[source]
        restored_to = job.status  # only one row should match, per multiplicity audit
        job.cancelled_from = None
        job.completed_at = None
        job.last_error = None
        if job.status == "queued":
            job.started_at = None
        # Do NOT touch job.attempts

    # Step 5: Only now mutate the workspace row.
    ws.soft_deleted_at = None

    # Step 6: Allocate the lifecycle generation inside this transaction.
    generation = bump_lifecycle_generation(session, workspace_id=workspace_id)

    # Step 7: Write the audit event.
    session.add(
        WorkspaceEvent(
            workspace_id=workspace_id,
            kind="admin_deleted_workspace_restored",
            actor_id=admin_id,
            payload={
                "workspace_id": str(workspace_id),
                "admin_user_id": admin_id,
                "setup_job_restored_to": restored_to,
            },
        )
    )

    # Step 8: Commit everything atomically.
    session.commit()
    return RestoreOutcome(
        generation=generation, setup_job_restored_to=restored_to, transitioned=True
    )


def scrub_workspace_bytes(workspace_id: uuid.UUID, *, workspaces_dir: Path) -> None:
    """Remove a workspace's attachment and reference bytes from disk.

    NOT called by the delete path. Slice A made deletion freeze-only: it destroys
    nothing, so a later restore is lossless. This function is retained for Slice
    E's admin purge, which is the deliberate act that removes a deleted
    workspace's files for real — and which will extend it to the whole workspace
    tree rather than these two subtrees.

    It therefore has NO production caller today. That is intentional; deleting it
    would mean re-deriving the on-disk layout from git history nobody will think
    to search.

    **Raises OSError.** Until Slice A this was best-effort and swallowed every
    OSError, because it ran inside the DELETE route where a failed cleanup must
    not block the 204. That caller is gone, and its only intended caller needs
    the opposite: the retention spec (§9, Slice E) requires a partial removal to
    surface as a controlled retryable failure that commits NO tombstone and NO
    audit event, so a purge can never report success over leftover bytes. A
    swallowed error would make "purged" unfalsifiable.

    Retry-safe: both subtree deletes are idempotent no-ops when the target is
    already absent, so re-running after a partial failure removes the remainder.

    Attachment and reference DB rows are never touched here — they are the audit
    trail.
    """
    # Attachment bytes (DB rows remain — audit trail).
    AttachmentStorage(root=workspaces_dir).delete_workspace(workspace_id=workspace_id)
    # Reference bytes (original files + extracted text.txt).
    ReferenceStorage(root=workspaces_dir).delete_workspace(workspace_id=workspace_id)
