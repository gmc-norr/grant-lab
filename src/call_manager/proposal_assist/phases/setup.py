"""Phase 0 worker — drains the workspace_setup_jobs queue.

Lifecycle:
    queued
      └─ claim_next_setup_job (FOR UPDATE SKIP LOCKED) → running, started_at
            ├─ _m1 → _m2 → _m3 OK, rubric review pending → waiting_for_rubric_review
            ├─ _m1 → _m2 → _m3 OK, rubric reviewed       → done, completed_at
            ├─ any milestone raises + attempts < 5       → queued, attempts++
            └─ any milestone raises + attempts == 5      → failed, completed_at

Each milestone (_m1, _m2, _m3) opens, uses and commits its own short-lived
session via Phase0Worker.session_factory — no session is held across two
milestones, which is what makes it safe for _m1 to take a database lock (see
its docstring). Each tick claims one job (FIFO via queued_at) under SELECT
FOR UPDATE SKIP LOCKED so multiple worker instances (future) wouldn't pick
the same row.
"""

from __future__ import annotations

import logging
import os
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from call_manager.db.models import (
    Call,
    CallRubric,
    CallSectionExtract,
    Profile,
    Workspace,
    WorkspaceEvent,
    WorkspaceSetupJob,
)
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex_live
from call_manager.proposal_assist.extractors.refresh import (
    compute_rubric,
    compute_section_extracts,
    persist_rubric,
    persist_section_extracts,
    rubric_is_fresh,
    section_extracts_are_fresh,
)
from call_manager.proposal_assist.extractors.sections import SECTION_NAMES
from call_manager.proposal_assist.hashing import source_text_hash

logger = logging.getLogger(__name__)

MAX_SETUP_ATTEMPTS = 5

# A setup job that's been 'running' longer than this is presumed abandoned —
# the worker process crashed between the status='running' commit and a terminal
# transition. Phase-0 extraction (a couple of LLM calls) is well under this, so
# a generous bound avoids reaping a slow-but-live job. NB: 'waiting_for_rubric_review'
# is a legitimate non-terminal state awaiting HUMAN action and is NEVER reaped.
STUCK_SETUP_AFTER = timedelta(minutes=30)

# Every git command in M1 runs inside a database lock, so each one needs a real
# ceiling: subprocess.run without a timeout can block forever and no CI duration
# assertion can prevent that in production.
_GIT_TIMEOUT_SECONDS = 30.0


def _git_env() -> dict[str, str]:
    """Author identity plus a hermetic environment: no site/global config, no
    credential prompt. Combined with the -c flags in _run_git, nothing an
    operator configures can introduce a hook, a signing helper or an
    interactive prompt into M1's locked region."""
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "call-manager")
    env.setdefault("GIT_AUTHOR_EMAIL", "call-manager@local")
    env.setdefault("GIT_COMMITTER_NAME", "call-manager")
    env.setdefault("GIT_COMMITTER_EMAIL", "call-manager@local")
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run_git(
    args: list[str], *, cwd: Path, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run one git command hermetically and bounded. Raises
    subprocess.TimeoutExpired on overrun, which M1 turns into a rollback that
    releases both the mutex and the row lock."""
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=check,
        env=_git_env(),
        timeout=_GIT_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
    )


def requeue_stuck_setup_jobs(session: Session, *, max_attempts: int = MAX_SETUP_ATTEMPTS) -> int:
    """Recover setup jobs stuck in 'running' past STUCK_SETUP_AFTER (worker
    crashed before a terminal transition). Jobs with attempts left go back to
    'queued' (started_at cleared) so the next tick re-claims them; jobs that
    have exhausted attempts are dead-lettered to 'failed'. Mirrors the ranking
    queue's requeue_stuck_running. Returns the number of rows touched. Caller
    commits.

    Without this a crash leaves the job 'running' forever — run_phase0_tick only
    looks for 'queued' rows, so the workspace never finishes setup."""
    now = datetime.now(UTC)
    cutoff = now - STUCK_SETUP_AFTER
    # Dead-letter first (so the requeue UPDATE below can't re-touch these rows).
    dead = session.execute(
        update(WorkspaceSetupJob)
        .where(
            WorkspaceSetupJob.status == "running",
            WorkspaceSetupJob.started_at.is_not(None),
            WorkspaceSetupJob.started_at < cutoff,
            WorkspaceSetupJob.attempts + 1 >= max_attempts,
        )
        .values(
            status="failed",
            attempts=WorkspaceSetupJob.attempts + 1,
            last_error="dead-lettered: stuck running past max_attempts (worker crash)",
            completed_at=now,
        )
    )
    requeued = session.execute(
        update(WorkspaceSetupJob)
        .where(
            WorkspaceSetupJob.status == "running",
            WorkspaceSetupJob.started_at.is_not(None),
            WorkspaceSetupJob.started_at < cutoff,
        )
        .values(
            status="queued",
            attempts=WorkspaceSetupJob.attempts + 1,
            started_at=None,
        )
    )
    n = (dead.rowcount or 0) + (requeued.rowcount or 0)
    if n:
        logger.warning(
            "phase0.requeued_stuck",
            extra={
                "event": "phase0_requeued_stuck",
                "dead_lettered": dead.rowcount or 0,
                "requeued": requeued.rowcount or 0,
            },
        )
    return n


class _AnthropicClientLike(Protocol):
    # `model` is stamped onto persisted-row .extracted_by_model by the
    # downstream caller (persist_section_extracts / persist_rubric).
    model: str

    def chat(
        self,
        *,
        messages: list[dict],
        response_format: dict | None = None,
    ) -> dict: ...


def claim_next_setup_job(*, session: Session) -> WorkspaceSetupJob | None:
    """Claim the oldest queued job under SELECT FOR UPDATE SKIP LOCKED.
    Sets status='running' and started_at. Caller commits.

    Jobs for soft-deleted workspaces are excluded in SQL, mirroring the four
    analysis-pass claim queries. This is the durable half of retirement: it
    survives a restart, needs no in-process tombstone, and holds even when the
    "jobs" hook could not reach the row."""
    live_ws = select(Workspace.id).where(Workspace.soft_deleted_at.is_(None))
    stmt = (
        select(WorkspaceSetupJob)
        .where(
            WorkspaceSetupJob.status == "queued",
            WorkspaceSetupJob.workspace_id.in_(live_ws),
        )
        .order_by(WorkspaceSetupJob.queued_at)  # FIFO
        .limit(1)
        .with_for_update(skip_locked=True, of=WorkspaceSetupJob)
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

    Holds a session_factory, never a session: each milestone opens, uses and
    closes its own short transaction. The previous long-lived session is what
    made a row lock unsafe here — it outlived two LLM calls — so removing it is
    the point, not an incidental tidy-up.
    """

    session_factory: sessionmaker
    client: _AnthropicClientLike
    extractor_version: str
    rubric_extractor_version: str
    workspaces_dir: str

    def run_one(self) -> bool:
        """Claim one job and drive its three milestones. Returns True if a job
        was processed (success or failure), False if the queue was empty."""
        with self.session_factory() as s:
            job = claim_next_setup_job(session=s)
            if job is None:
                return False
            job_id = job.id
            workspace_id = job.workspace_id
            s.commit()

        try:
            pinned = self._m1(job_id, workspace_id)
            self._m2(job_id, workspace_id, pinned)
            self._m3(job_id, workspace_id, pinned)
        except Exception as exc:  # noqa: BLE001 — every outcome is recorded on the row
            self._fail(job_id, exc)
        return True

    _PHASE_ARTIFACT_FILES: tuple[str, ...] = (
        "proposal-spec.md",
        "proposal-plan.md",
        "proposal.md",
    )

    def _workspace_repo_dir(self, workspace_id) -> Path:
        return Path(self.workspaces_dir) / str(workspace_id) / "repo"

    def _m1(self, job_id: uuid.UUID, workspace_id: uuid.UUID) -> str:
        """Milestone 1: filesystem init + call/profile snapshots. Returns the
        pinned Call source hash for the rest of the run.

        Takes the commit mutex BEFORE the workspace row lock. That order is what
        bounds a concurrent DELETE: soft_delete_workspace_db takes the same mutex
        first, so a delete blocked behind a wedged M1 raises CommitMutexTimeout
        (the route's 429) instead of waiting on a row lock that has no timeout.
        acquire_commit_mutex_live also supplies M1's liveness check — it raises
        WorkspaceDeleted for an already-deleted workspace, before anything is
        created on disk.
        """
        with self.session_factory() as s:
            acquire_commit_mutex_live(s, workspace_id)
            ws = s.get(Workspace, workspace_id, with_for_update=True)
            if ws is None:
                raise RuntimeError(f"workspace {workspace_id} vanished")
            if ws.call_id is None:
                raise RuntimeError(f"workspace {ws.id} has no call_id — Phase 0 requires one")
            call = s.get(Call, ws.call_id)
            if call is None:
                raise RuntimeError(f"call {ws.call_id} not found")

            pinned = source_text_hash(call.full_text or "")

            self._init_repo(ws.id)

            ws.call_body_snapshot = (call.summary or "") + "\n\n" + (call.full_text or "")
            if ws.profile_id is not None:
                profile = s.get(Profile, ws.profile_id)
                if profile is not None:
                    ws.profile_desc_snapshot = profile.description or ""
            ws.snapshot_refreshed_at = datetime.now(UTC)
            s.commit()
            return pinned

    def _init_repo(self, workspace_id: uuid.UUID) -> None:
        """The filesystem half of M1, unchanged in behaviour and still
        recovery-safe: git init is skipped when .git exists, and the
        rev-list check finishes a crash between init and the first commit."""
        repo_dir = self._workspace_repo_dir(workspace_id)
        repo_dir.mkdir(parents=True, exist_ok=True)
        if not (repo_dir / ".git").is_dir():
            _run_git(["init", "-q", "-b", "main"], cwd=repo_dir)

        commit_count_proc = _run_git(
            ["rev-list", "--count", "HEAD"], cwd=repo_dir, check=False
        )
        has_initial_commit = (
            commit_count_proc.returncode == 0
            and commit_count_proc.stdout.strip() not in ("", "0")
        )
        if not has_initial_commit:
            for fname in self._PHASE_ARTIFACT_FILES:
                (repo_dir / fname).write_text("")
            _run_git(["add", "-A"], cwd=repo_dir)
            _run_git(
                ["commit", "-q", "-m", "phase0: initialize empty artifacts"], cwd=repo_dir
            )

    def _ensure_section_extracts_snapshot_on(
        self, session: Session, workspace_id: uuid.UUID
    ) -> None:
        ws = session.get(Workspace, workspace_id)
        if ws is None or ws.call_id is None:
            raise RuntimeError("workspace or call missing for section extracts")
        call = session.get(Call, ws.call_id)
        if call is None:
            raise RuntimeError(f"call {ws.call_id} not found")

        if not section_extracts_are_fresh(
            session=session, call=call, extractor_version=self.extractor_version
        ):
            sections = compute_section_extracts(call_text=call.full_text or "", client=self.client)
            persist_section_extracts(
                session=session,
                call=call,
                sections=sections,
                model=self.client.model,
                extractor_version=self.extractor_version,
                source_hash=source_text_hash(call.full_text or ""),
            )
        # Read back into a workspace-owned dict; the workspace snapshot is
        # what every later phase reads via get_section_extract.
        rows = (
            session.query(CallSectionExtract)
            .filter(CallSectionExtract.call_id == call.id)
            .all()
        )
        snapshot = {r.section: r.extract_text for r in rows}
        # Defensive: ensure all six keys present (extract_sections backfills,
        # but if the template was seeded by a different path, fill any gaps).
        for name in SECTION_NAMES:
            snapshot.setdefault(name, "")
        ws.section_extracts = snapshot

    def _ensure_rubric_snapshot_on(self, session: Session, workspace_id: uuid.UUID) -> None:
        ws = session.get(Workspace, workspace_id)
        if ws is None or ws.call_id is None:
            raise RuntimeError("workspace or call missing for rubric")
        call = session.get(Call, ws.call_id)
        if call is None:
            raise RuntimeError(f"call {ws.call_id} not found")

        if not rubric_is_fresh(
            session=session, call=call, extractor_version=self.rubric_extractor_version
        ):
            payload = compute_rubric(call_text=call.full_text or "", client=self.client)
            persist_rubric(
                session=session,
                call=call,
                payload=payload,
                model=self.client.model,
                extractor_version=self.rubric_extractor_version,
                source_hash=source_text_hash(call.full_text or ""),
            )
        template = session.get(CallRubric, call.id)
        if template is None:
            raise RuntimeError(f"rubric template for call {call.id} not found after fresh")

        ws.rubric_criteria = template.criteria
        ws.rubric_funder_notes = template.funder_notes
        ws.rubric_updated_at = datetime.now(UTC)

        # Audit: write the canonical starting state for forward-replay of
        # rubric edits (per the phased spec's audit-reconstruction contract).
        session.add(
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

    def _m2(self, job_id: uuid.UUID, workspace_id: uuid.UUID, pinned: str) -> None:
        """Interim: preserves today's behaviour on its own session. Task 6
        replaces this with the pre-compute liveness check, the Call pin and the
        locked persist. `pinned` is accepted but unused until then."""
        with self.session_factory() as s:
            self._ensure_section_extracts_snapshot_on(s, workspace_id)
            s.commit()

    def _m3(self, job_id: uuid.UUID, workspace_id: uuid.UUID, pinned: str) -> None:
        """Interim: preserves today's behaviour, including the terminal status
        transition that `run_one` used to perform. Task 7 replaces this with the
        atomic version."""
        with self.session_factory() as s:
            self._ensure_rubric_snapshot_on(s, workspace_id)
            job = s.get(WorkspaceSetupJob, job_id)
            if job is not None and job.status == "running":
                job.status = "waiting_for_rubric_review"
            s.commit()

    def _fail(self, job_id: uuid.UUID, exc: BaseException) -> None:
        """Interim: the failure semantics `run_one` had before this task —
        attempts++, requeue below MAX_SETUP_ATTEMPTS, dead-letter at it. Task 8
        adds the terminal path for cancelled/deleted workspaces."""
        now = datetime.now(UTC)
        with self.session_factory() as s:
            job = s.get(WorkspaceSetupJob, job_id, with_for_update=True)
            if job is None:
                logger.error("phase0 setup job %s vanished before failure write", job_id)
                return
            job.attempts += 1
            job.last_error = f"{type(exc).__name__}: {exc}"
            if job.attempts >= MAX_SETUP_ATTEMPTS:
                job.status = "failed"
                job.completed_at = now
            else:
                job.status = "queued"
                job.started_at = None
            s.commit()


def run_phase0_tick(
    *,
    session_factory: sessionmaker,
    client_factory: Callable[[], _AnthropicClientLike],
    extractor_version: str,
    rubric_extractor_version: str,
    workspaces_dir: str,
) -> None:
    """Scheduler-friendly tick: open one session, claim+drain at most one job.

    Peeks the queue before constructing the LLM client — avoids paying the
    client-construction cost on every empty tick. A short race window
    between peek and claim is benign: worst case the client is built and
    immediately unused.
    """
    with session_factory() as session:
        # First recover any job abandoned in 'running' by a crashed worker —
        # without this it stays invisible to the queued-only peek below forever.
        requeue_stuck_setup_jobs(session)
        session.commit()

        # Cheap peek: any queued job at all?
        has_work = session.scalar(
            select(WorkspaceSetupJob.id).where(WorkspaceSetupJob.status == "queued").limit(1)
        )
    if has_work is None:
        return

    worker = Phase0Worker(
        session_factory=session_factory,
        client=client_factory(),
        extractor_version=extractor_version,
        rubric_extractor_version=rubric_extractor_version,
        workspaces_dir=workspaces_dir,
    )
    worker.run_one()
