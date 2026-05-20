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
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_monitor.db.models import (
    Call,
    CallRubric,
    CallSectionExtract,
    Profile,
    Workspace,
    WorkspaceEvent,
    WorkspaceSetupJob,
)
from call_monitor.proposal_assist.extractors.refresh import (
    ensure_rubric_fresh,
    ensure_section_extracts_fresh,
)
from call_monitor.proposal_assist.extractors.sections import SECTION_NAMES

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

    _PHASE_ARTIFACT_FILES: tuple[str, ...] = (
        "proposal-spec.md",
        "proposal-plan.md",
        "proposal.md",
    )

    def _workspace_repo_dir(self, workspace_id) -> Path:
        return Path(self.workspaces_dir) / str(workspace_id) / "repo"

    def _snapshot_and_init_repo(self, job: WorkspaceSetupJob) -> None:
        ws = self.session.get(Workspace, job.workspace_id)
        if ws is None:
            raise RuntimeError(f"workspace {job.workspace_id} vanished")

        # Step 1: init git repo with empty phase artifacts.
        repo_dir = self._workspace_repo_dir(ws.id)
        repo_dir.mkdir(parents=True, exist_ok=True)
        if not (repo_dir / ".git").is_dir():
            subprocess.run(
                ["git", "init", "-q", "-b", "main"],
                cwd=repo_dir,
                check=True,
            )
            # Author identity for the initial commit — set via env so we
            # don't depend on the running user's global gitconfig (CI / containers).
            env = os.environ.copy()
            env.setdefault("GIT_AUTHOR_NAME", "call-manager")
            env.setdefault("GIT_AUTHOR_EMAIL", "call-manager@local")
            env.setdefault("GIT_COMMITTER_NAME", "call-manager")
            env.setdefault("GIT_COMMITTER_EMAIL", "call-manager@local")
            for fname in self._PHASE_ARTIFACT_FILES:
                (repo_dir / fname).write_text("")
            subprocess.run(
                ["git", "add", "-A"],
                cwd=repo_dir,
                check=True,
                env=env,
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", "phase0: initialize empty artifacts"],
                cwd=repo_dir,
                check=True,
                env=env,
            )

        # Step 2: snapshot call body.
        if ws.call_id is None:
            raise RuntimeError(f"workspace {ws.id} has no call_id — Phase 0 requires one")
        call = self.session.get(Call, ws.call_id)
        if call is None:
            raise RuntimeError(f"call {ws.call_id} not found")
        ws.call_body_snapshot = (call.summary or "") + "\n\n" + (call.full_text or "")

        # Step 3: snapshot profile description (if profile linked).
        if ws.profile_id is not None:
            profile = self.session.get(Profile, ws.profile_id)
            if profile is not None:
                ws.profile_desc_snapshot = profile.description or ""

        ws.snapshot_refreshed_at = datetime.now(UTC)

    def _ensure_section_extracts_snapshot(self, job: WorkspaceSetupJob) -> None:
        ws = self.session.get(Workspace, job.workspace_id)
        if ws is None or ws.call_id is None:
            raise RuntimeError("workspace or call missing for section extracts")
        call = self.session.get(Call, ws.call_id)
        if call is None:
            raise RuntimeError(f"call {ws.call_id} not found")

        ensure_section_extracts_fresh(
            session=self.session,
            call=call,
            client=self.client,
            extractor_version=self.extractor_version,
        )
        # Read back into a workspace-owned dict; the workspace snapshot is
        # what every later phase reads via get_section_extract.
        rows = (
            self.session.query(CallSectionExtract)
            .filter(CallSectionExtract.call_id == call.id)
            .all()
        )
        snapshot = {r.section: r.extract_text for r in rows}
        # Defensive: ensure all six keys present (extract_sections backfills,
        # but if the template was seeded by a different path, fill any gaps).
        for name in SECTION_NAMES:
            snapshot.setdefault(name, "")
        ws.section_extracts = snapshot

    def _ensure_rubric_snapshot(self, job: WorkspaceSetupJob) -> None:
        ws = self.session.get(Workspace, job.workspace_id)
        if ws is None or ws.call_id is None:
            raise RuntimeError("workspace or call missing for rubric")
        call = self.session.get(Call, ws.call_id)
        if call is None:
            raise RuntimeError(f"call {ws.call_id} not found")

        ensure_rubric_fresh(
            session=self.session,
            call=call,
            client=self.client,
            extractor_version=self.rubric_extractor_version,
        )
        template = self.session.get(CallRubric, call.id)
        if template is None:
            raise RuntimeError(f"rubric template for call {call.id} not found after fresh")

        ws.rubric_criteria = template.criteria
        ws.rubric_funder_notes = template.funder_notes
        ws.rubric_updated_at = datetime.now(UTC)

        # Audit: write the canonical starting state for forward-replay of
        # rubric edits (per the phased spec's audit-reconstruction contract).
        self.session.add(
            WorkspaceEvent(
                workspace_id=ws.id,
                kind="rubric_initialized",
                payload={
                    "criteria": template.criteria,
                    "funder_notes": template.funder_notes,
                    "seeded_from_call_rubrics_extracted_at": (template.extracted_at.isoformat()),
                },
            )
        )

    def _run_steps(self, job: WorkspaceSetupJob) -> None:
        # Steps 1-3: git init + snapshot call body + snapshot profile
        self._snapshot_and_init_repo(job)
        # Step 4: ensure section_extracts template + copy to workspace
        self._ensure_section_extracts_snapshot(job)
        # Step 5: ensure rubric template + copy to workspace + write
        # rubric_initialized audit event
        self._ensure_rubric_snapshot(job)
        # Step 6: yield to owner rubric review. The rubric-review endpoint
        # (Task 18) advances the job to 'done' and the workspace to
        # current_phase='spec' when the owner confirms.
        job.status = "waiting_for_rubric_review"
        # NOT setting completed_at — the job isn't terminal yet.


from collections.abc import Callable  # noqa: E402

from sqlalchemy.orm import sessionmaker  # noqa: E402


def run_phase0_tick(
    *,
    session_factory: sessionmaker,
    client_factory: Callable[[], _AnthropicClientLike],
    extractor_version: str,
    rubric_extractor_version: str,
    workspaces_dir: str,
) -> None:
    """Scheduler-friendly tick: open one session, claim+drain at most one job.

    A single-job-per-tick discipline keeps individual ticks short (so the
    BackgroundScheduler worker pool doesn't pile up), gives Postgres' FOR
    UPDATE SKIP LOCKED a clean release point, and bounds the cost of a
    failed LLM call to one workspace per tick.

    Multiple ticks per second are fine — claim_next_setup_job returns None
    when the queue is empty and the function returns immediately.
    """
    with session_factory() as session:
        worker = Phase0Worker(
            session=session,
            client=client_factory(),
            extractor_version=extractor_version,
            rubric_extractor_version=rubric_extractor_version,
            workspaces_dir=workspaces_dir,
        )
        worker.run_one()
