"""Phase 0 worker — drains the workspace_setup_jobs queue.

Lifecycle:
    queued
      └─ claim_next_setup_job (FOR UPDATE SKIP LOCKED) → running, started_at
            ├─ run_steps OK + rubric review pending → waiting_for_rubric_review
            ├─ run_steps OK + rubric reviewed       → done, completed_at
            ├─ run_steps raises + attempts < 5      → queued, attempts++
            └─ run_steps raises + attempts == 5     → failed, completed_at

Each tick claims one job (FIFO via id) under SELECT FOR UPDATE SKIP LOCKED
so multiple worker instances (future) wouldn't pick the same row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_monitor.db.models import WorkspaceSetupJob

logger = logging.getLogger(__name__)

MAX_SETUP_ATTEMPTS = 5


class _AnthropicClientLike(Protocol):
    # `model` is stamped onto persisted-row .extracted_by_model by the
    # downstream caller (ensure_section_extracts_fresh / ensure_rubric_fresh).
    model: str

    def chat(
        self,
        *,
        messages: list[dict],
        response_format: dict | None = None,
    ) -> dict: ...


def claim_next_setup_job(*, session: Session) -> WorkspaceSetupJob | None:
    """Claim the oldest queued job under SELECT FOR UPDATE SKIP LOCKED.
    Sets status='running' and started_at. Caller commits."""
    stmt = (
        select(WorkspaceSetupJob)
        .where(WorkspaceSetupJob.status == "queued")
        .order_by(WorkspaceSetupJob.id)  # FIFO-ish; UUIDs sort lexically
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = session.scalar(stmt)
    if job is None:
        return None
    job.status = "running"
    job.started_at = datetime.now(UTC)
    return job


@dataclass
class Phase0Worker:
    """Single-threaded worker; one instance per process.

    `_run_steps` is the long-running sub-step sequence — split out so tests
    can monkeypatch it. Tasks 14–16 fill in its implementation.
    """

    session: Session
    client: _AnthropicClientLike
    extractor_version: str
    rubric_extractor_version: str
    workspaces_dir: str

    def run_one(self) -> bool:
        """Claim one job, execute it, advance its status. Returns True if a
        job was processed (success or failure), False if the queue was empty."""
        job = claim_next_setup_job(session=self.session)
        if job is None:
            return False
        self.session.commit()  # release the FOR UPDATE row lock early
        job_id = job.id

        try:
            self._run_steps(job)
        except Exception as exc:
            # CRITICAL: rollback any partial work (half-written extracts, etc.)
            # before writing the failure state. Without rollback we'd commit
            # destructive intermediate state alongside the job-status row.
            self.session.rollback()

            # Re-fetch in a clean session so we can write failure state on
            # a non-detached instance.
            job = self.session.get(WorkspaceSetupJob, job_id)
            if job is None:
                logger.error(
                    "phase0 setup job %s vanished after rollback",
                    job_id,
                )
                return True
            job.attempts += 1
            job.last_error = f"{type(exc).__name__}: {exc}"
            if job.attempts >= MAX_SETUP_ATTEMPTS:
                job.status = "failed"
                job.completed_at = datetime.now(UTC)
                logger.exception(
                    "phase0 setup job %s failed permanently after %d attempts",
                    job.id,
                    job.attempts,
                )
            else:
                job.status = "queued"
                job.started_at = None  # ready to be re-claimed on next tick
                logger.warning(
                    "phase0 setup job %s transient failure (attempt %d/%d): %s",
                    job.id,
                    job.attempts,
                    MAX_SETUP_ATTEMPTS,
                    exc,
                )
            self.session.commit()
            return True

        # Success path. _run_steps decides between waiting_for_rubric_review
        # and done by mutating job.status directly. If it left status as
        # 'running', treat that as 'done'.
        if job.status == "running":
            job.status = "done"
            job.completed_at = datetime.now(UTC)
        elif job.status == "waiting_for_rubric_review":
            # Yield: do NOT set completed_at; the rubric review endpoint
            # will advance to 'done' when the owner confirms.
            pass
        self.session.commit()
        return True

    def _run_steps(self, job: WorkspaceSetupJob) -> None:
        """Filled in by Tasks 14–16. Raises on transient/permanent failure;
        mutates job.status to 'waiting_for_rubric_review' to yield."""
        raise NotImplementedError("filled in by Tasks 14–16")
