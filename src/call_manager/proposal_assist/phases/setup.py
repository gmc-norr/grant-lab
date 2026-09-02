"""Phase 0 worker — drains the workspace_setup_jobs queue.

Lifecycle:
    queued
      └─ claim_next_setup_job (FOR UPDATE SKIP LOCKED) → running, started_at
            ├─ _m1 → _m2 → _m3 OK, rubric review pending    → waiting_for_rubric_review
            ├─ _m1 → _m2 → _m3 OK, rubric reviewed          → done, completed_at
            ├─ workspace deleted/job cancelled (any raise)  → cancelled, completed_at
            ├─ other raise + attempts < 5                   → queued, attempts++
            └─ other raise + attempts == 5                  → failed, completed_at

_fail (called from run_one's except block) is the single place a job leaves
'running' on any error path — see its docstring for the exact ordering.

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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
from call_manager.proposal_assist.branch_entity_backfill import backfill_branch_entities
from call_manager.proposal_assist.drafting.locking import (
    WorkspaceDeleted,
)
from call_manager.proposal_assist.extractors.refresh import (
    compute_rubric,
    compute_section_extracts,
    persist_rubric,
    persist_section_extracts,
    rubric_is_fresh,
    section_extracts_are_fresh,
)
from call_manager.proposal_assist.extractors.sections import SECTION_NAMES
from call_manager.proposal_assist.fencing import acquire_commit_mutex_at_generation
from call_manager.proposal_assist.hashing import source_text_hash
from call_manager.proposal_assist.ports.errors import UnfencedInvocation
from call_manager.proposal_assist.ports.stamp import FenceStamp
from call_manager.proposal_assist.row_locks import RowLockTimeout, bounded_lock_waits

logger = logging.getLogger(__name__)

MAX_SETUP_ATTEMPTS = 5


class CallSourceChanged(RuntimeError):
    """The Call's text was re-ingested mid-run, so the milestones would otherwise
    commit snapshots taken from different revisions.

    Deliberately a typed failure rather than an internal retry loop: a Call being
    re-scraped repeatedly would spin forever. It goes through the normal failure
    path, consuming an attempt and dead-lettering at MAX_SETUP_ATTEMPTS."""


class WorkspaceGoneDuringSetup(RuntimeError):
    """The workspace was soft-deleted, or the job was cancelled, between
    milestones. Phase0Worker._fail routes this — and WorkspaceDeleted raised
    by acquire_commit_mutex_at_generation in _m1 — straight to a terminal
    'cancelled' status rather than a requeue; see _fail's docstring."""


def _refuse_stale_claim(ws: Workspace, claimed_generation: int | None) -> None:
    """Spec mechanism 3 (review 2026-09-02 A10 F1): the caller holds `ws` under
    FOR UPDATE, so its `lifecycle_generation` is the truth for this transaction.
    A claim stamped at another generation belongs to a workspace that was
    deleted and restored since — writing its LLM-derived milestones now would
    land them in the restored workspace and terminalize ANOTHER worker's claim.
    None = legacy claim with no stamped generation; nothing to compare."""
    if claimed_generation is not None and ws.lifecycle_generation != claimed_generation:
        raise WorkspaceGoneDuringSetup(
            f"workspace {ws.id} moved from generation {claimed_generation} to "
            f"{ws.lifecycle_generation} (deleted and restored); this claim is stale"
        )


class SetupRowLockTimeout(RowLockTimeout):
    """A persist transaction's SET LOCAL lock_timeout fired while waiting on a
    lock — the Call row an ingestion batch upsert holds for its whole loop
    (src/call_manager/ingestion/orchestrator.py), a cache row the persist DELETE
    has to take, or CallRubric's unique index. Deliberately
    an ORDINARY failure, not a WorkspaceGoneDuringSetup: lock contention says
    nothing about whether the workspace is gone, so _fail's default branch is
    right — consume an attempt and requeue (or dead-letter at
    MAX_SETUP_ATTEMPTS) rather than cancel. The whole point of the timeout is
    to stop this milestone from pinning the workspace row indefinitely while
    it waits; letting the wait be unbounded is the bug this exists to avoid.
    """


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

# M2's and M3's persist transactions hold the workspace row (and, in M3, the
# job row) from their first `with_for_update` until COMMIT — and everything
# they do in between can wait on a lock: the Call row (an ingestion batch
# upsert holds every touched Call row for its whole loop; see
# src/call_manager/ingestion/orchestrator.py), the cache rows the persist
# DELETE has to take, a unique-index wait on CallRubric. Unlike the commit
# mutex, a plain row lock has no timeout of its own, and M2/M3 hold no commit
# mutex, so nothing else caps them — an unbounded wait ANYWHERE in the
# transaction pins the workspace row against a DELETE for exactly that long.
# (Since Slice C3 a DELETE past the commit mutex gives up on that row after 2s
# and answers 429 having deleted nothing, rather than hanging — better, but
# still a delete the owner asked for and did not get.)
#
# Why 2 seconds specifically — it is derived from the DELETE route's budget,
# not borrowed from the commit mutex (M2/M3 don't hold that mutex, so its 10s
# default says nothing about them):
#   * The route's asserted budget is < 6.0s
#     (test_delete_route_returns_promptly_while_phase0_runs_its_extractors) and
#     the retirement barrier _RETIREMENT_BARRIER_SECONDS = 6.0 can consume that
#     budget on its own. The DB phase's contribution therefore has to be a small
#     fraction of the budget, not a multiple of it.
#   * The realistic contender for the Call row is ingestion's batch upsert loop,
#     and that loop is explicitly network-free: fetch_new_calls is materialized
#     BEFORE the write transaction opens, so the transaction only upserts bodies
#     that are already in memory (ingestion/orchestrator.py:160-197). 2s is far
#     above its real hold time.
#   * Losing an occasional race to contention is cheap: MAX_SETUP_ATTEMPTS = 5,
#     so a SetupRowLockTimeout costs a requeue, not a dead letter.
_ROW_LOCK_TIMEOUT_SECONDS = 2


@contextmanager
def _bounded_lock_waits(session: Session) -> Iterator[None]:
    """SET LOCAL lock_timeout over an ENTIRE persist transaction — every lock
    wait inside it, not just the `with_for_update` acquisitions.

    The mechanism lives in proposal_assist/row_locks.py (shared with the four
    pass prefixes, which need the identical bound for the identical reason); this
    wrapper only binds Phase-0's own timeout and typed exception. It reads
    _ROW_LOCK_TIMEOUT_SECONDS as a module global at call time, so a test can
    shorten it with monkeypatch.

    Borrows acquire_commit_mutex's idiom (drafting/locking.py) for the SET LOCAL
    and the 55P03 (lock_not_available) → typed-exception translation, but sets
    its own bound over the whole region rather than only around an acquisition.
    Since Slice C3 that mutex leaves a 2s cap in force for the rest of its
    caller's transaction; before it, it restored the server default (0 =
    disabled), which left exactly this stretch uncapped. Either way the caller's
    remaining statements are the point here: M2/M3 hold the workspace row from
    the first acquisition to COMMIT, and their cache DML, flush, audit-event
    insert and commit can all block on a row lock — and M2/M3 hold no mutex at
    all, so this wrapper is the only thing bounding them.

    Nothing needs restoring: SET LOCAL is scoped to the transaction, so the
    caller's commit (or the rollback on `with self.session_factory()` exit, or
    the implicit abort a timeout causes) clears it either way.
    """
    with bounded_lock_waits(
        session,
        timeout_seconds=_ROW_LOCK_TIMEOUT_SECONDS,
        error=SetupRowLockTimeout,
        message=(
            "timed out waiting for a row lock — likely the Call row, held "
            "by a concurrent ingestion batch or another Phase-0 worker"
        ),
    ):
        yield


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


def _run_git(args: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
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
    Sets status='running', started_at, and claimed_at_generation. Caller commits.

    Jobs for soft-deleted workspaces are excluded in SQL, mirroring the four
    analysis-pass claim queries. This is the durable half of retirement: it
    survives a restart, needs no in-process tombstone, and holds even when the
    "jobs" hook could not reach the row.

    The join captures ``lifecycle_generation`` IN THE SAME STATEMENT as the FOR
    UPDATE lock (F3a rule / Global Constraint 2), copying the shape of
    ``claim_next_evaluator_job``. A separate SELECT would reopen the
    delete/restore race the fence exists to close. ``of=WorkspaceSetupJob`` keeps
    the lock on the JOB row only — without it PostgreSQL would also lock the
    joined Workspace row, inverting the M -> J -> W lock order against the delete
    path."""
    row = session.execute(
        select(WorkspaceSetupJob, Workspace.lifecycle_generation)
        .join(Workspace, Workspace.id == WorkspaceSetupJob.workspace_id)
        .where(
            WorkspaceSetupJob.status == "queued",
            Workspace.soft_deleted_at.is_(None),
        )
        .order_by(WorkspaceSetupJob.queued_at)  # FIFO
        .limit(1)
        .with_for_update(skip_locked=True, of=WorkspaceSetupJob)
    ).first()
    if row is None:
        return None
    job, generation = row
    job.status = "running"
    job.started_at = datetime.now(UTC)
    job.claimed_at_generation = generation
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
            # Read the generation the claim stamped, in the SAME session that holds
            # it, and thread it into _m1 — never re-read it at the write (F3a rule).
            claimed_generation = job.claimed_at_generation
            s.commit()

        try:
            pinned = self._m1(job_id, workspace_id, claimed_generation)
            self._m2(job_id, workspace_id, pinned, claimed_generation=claimed_generation)
            self._m3(job_id, workspace_id, pinned, claimed_generation=claimed_generation)
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

    def _m1(
        self, job_id: uuid.UUID, workspace_id: uuid.UUID, claimed_generation: int | None
    ) -> str:
        """Milestone 1: filesystem init + call/profile snapshots. Returns the
        pinned Call source hash for the rest of the run.

        Takes the commit mutex BEFORE the workspace row lock. That order is what
        bounds a concurrent DELETE: soft_delete_workspace_db takes the same mutex
        first, so a delete blocked behind a wedged M1 raises CommitMutexTimeout
        (the route's 429) instead of waiting on a row lock that has no timeout.

        ``claimed_generation`` is the ``lifecycle_generation`` the claim stamped on
        the job row (F3a), threaded down from ``run_one`` — never re-read here. It is
        carried into a FenceStamp, the background-job twin of ``Invocation.stamp()``:
        the actor's belief is the generation it claimed, and the mutex is taken at
        ``stamp.generation``. A None claimed_generation is a legacy row claimed before
        the fence existed and has no belief to fence against, so it raises
        UnfencedInvocation rather than defaulting (generation 0 is REAL).

        acquire_commit_mutex_at_generation supplies M1's liveness check: it raises
        WorkspaceDeleted for an already-deleted workspace (deleted takes precedence
        over stale), or WorkspaceGenerationStale if the workspace moved past the
        claimed generation — before anything is created on disk.
        """
        if claimed_generation is None:
            raise UnfencedInvocation(workspace_id)
        stamp = FenceStamp(
            workspace_id=workspace_id,
            generation=claimed_generation,
            active_turn_job_id=None,
            origin_job_id=job_id,
        )
        with self.session_factory() as s:
            acquire_commit_mutex_at_generation(s, stamp.workspace_id, generation=stamp.generation)
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

            # Found this workspace's first branch row and root pointer, in the same
            # transaction that creates the repo (spec §8 S1 step 4). `_init_repo`
            # has just run `git init -b main` and committed, so the ref exists.
            #
            # Without this, a workspace created AFTER migration 0068 has a repo, no
            # branch row, and a NULL root pointer — the migration's backfill only
            # ever saw the workspaces that existed when it ran. S2 resolves every
            # filesystem path through that pointer, so a null one is not cosmetic.
            #
            # The reconciler is reused rather than reimplemented: it already handles
            # ref filtering, a detached HEAD and orphaned rows, and a second founding
            # path is the one that would drift.
            # The RESULT IS CHECKED. `_init_repo` has just run `git init -b main`
            # and committed, so a founding that founds nothing means git could not
            # be read — and the reconciler reports that by SKIPPING, not raising
            # (one bad repo out of 500 must not halt a migration). Discarding the
            # counters here turned that into a silent success: the workspace
            # shipped with a NULL root pointer, nothing re-runs the reconciler for
            # an established workspace, and S2 resolves every path through that
            # pointer. Failing the setup job is recoverable — it retries up to 5
            # times; a permanently rootless workspace is not.
            #
            # Asserted on the POINTER, not on `rooted == 1`. `_m1` is documented
            # re-run-safe, and on a second pass the pointer is already set, so the
            # reconciler correctly reports `rooted=0` — a count check would fail a
            # healthy retry.
            result = backfill_branch_entities(
                s, workspaces_dir=self.workspaces_dir, workspace_id=ws.id
            )
            if s.get(Workspace, ws.id).root_branch_id is None:
                raise RuntimeError(
                    f"workspace {ws.id}: the branch founding produced no root pointer ({result})"
                )

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

        commit_count_proc = _run_git(["rev-list", "--count", "HEAD"], cwd=repo_dir, check=False)
        has_initial_commit = (
            commit_count_proc.returncode == 0 and commit_count_proc.stdout.strip() not in ("", "0")
        )
        if not has_initial_commit:
            for fname in self._PHASE_ARTIFACT_FILES:
                (repo_dir / fname).write_text("")
            _run_git(["add", "-A"], cwd=repo_dir)
            _run_git(["commit", "-q", "-m", "phase0: initialize empty artifacts"], cwd=repo_dir)

    def _assert_live(
        self,
        job_id: uuid.UUID,
        workspace_id: uuid.UUID,
        claimed_generation: int | None = None,
    ) -> None:
        """Short, lock-free pre-check. Used BEFORE computing so a workspace that
        has moved on costs no cloud spend; the locked persist re-checks for the
        race that opens after this returns.

        Liveness alone is not enough (review 2026-09-02 A10 F1): after delete →
        restore, `soft_deleted_at` is NULL again and the re-queued, re-claimed
        row is 'running' again — for ANOTHER worker. The generation is what
        tells this worker's claim from that one. None = a legacy row claimed
        before the claim stamped a generation; only liveness can be checked.
        """
        with self.session_factory() as s:
            row = s.execute(
                select(Workspace.soft_deleted_at, Workspace.lifecycle_generation).where(
                    Workspace.id == workspace_id
                )
            ).one_or_none()
            status = s.scalar(
                select(WorkspaceSetupJob.status).where(WorkspaceSetupJob.id == job_id)
            )
        deleted = None if row is None else row.soft_deleted_at
        generation = None if row is None else row.lifecycle_generation
        if row is None or deleted is not None or status != "running":
            raise WorkspaceGoneDuringSetup(
                f"workspace {workspace_id} deleted or job {job_id} no longer running"
            )
        if claimed_generation is not None and generation != claimed_generation:
            raise WorkspaceGoneDuringSetup(
                f"workspace {workspace_id} moved from generation {claimed_generation} to "
                f"{generation} (deleted and restored); this claim is stale"
            )

    def _m2(
        self,
        job_id: uuid.UUID,
        workspace_id: uuid.UUID,
        pinned: str,
        claimed_generation: int | None = None,
    ) -> None:
        """Milestone 2: section extracts. The LLM call happens with NO
        transaction open; only the write is under lock. `claimed_generation` is
        the generation the claim stamped (threaded from run_one, never re-read
        at the write); the locked persist refuses any other."""
        self._assert_live(job_id, workspace_id, claimed_generation)

        with self.session_factory() as s:
            ws = s.get(Workspace, workspace_id)
            call = s.get(Call, ws.call_id) if ws is not None else None
            if call is None:
                raise RuntimeError("workspace or call missing for section extracts")
            call_id = call.id
            call_text = call.full_text or ""
            fresh = section_extracts_are_fresh(
                session=s, call=call, extractor_version=self.extractor_version
            )

        sections = (
            None if fresh else compute_section_extracts(call_text=call_text, client=self.client)
        )

        # _bounded_lock_waits wraps the WHOLE transaction, commit included: the
        # workspace row is held from the acquisition below until COMMIT, and the
        # persist DELETE further down waits on row locks of its own.
        with self.session_factory() as s, _bounded_lock_waits(s):
            ws = s.get(Workspace, workspace_id, with_for_update=True)
            if ws is None or ws.soft_deleted_at is not None:
                raise WorkspaceGoneDuringSetup(f"workspace {workspace_id} deleted")
            _refuse_stale_claim(ws, claimed_generation)
            # Workspace row THEN Call row — the normative order. Two
            # workspaces sharing a Call therefore queue rather than
            # deadlock, and neither inverts against the delete
            # transaction's workspace-row update.
            call = s.get(Call, call_id, with_for_update=True)
            if call is None:
                raise RuntimeError(f"call {call_id} not found")
            if source_text_hash(call.full_text or "") != pinned:
                raise CallSourceChanged(
                    f"call {call_id} was re-ingested during setup of workspace {workspace_id}"
                )

            # Re-check under the lock: another worker may have made the cache
            # fresh while we computed. Snapshot the canonical rows instead of
            # overwriting them with our now-redundant payload.
            if sections is not None and not section_extracts_are_fresh(
                session=s, call=call, extractor_version=self.extractor_version
            ):
                persist_section_extracts(
                    session=s,
                    call=call,
                    sections=sections,
                    model=self.client.model,
                    extractor_version=self.extractor_version,
                    source_hash=pinned,
                )
                s.flush()

            rows = s.scalars(
                select(CallSectionExtract).where(CallSectionExtract.call_id == call_id)
            ).all()
            snapshot = {r.section: r.extract_text for r in rows}
            for name in SECTION_NAMES:
                snapshot.setdefault(name, "")
            ws.section_extracts = snapshot
            s.commit()

    def _m3(
        self,
        job_id: uuid.UUID,
        workspace_id: uuid.UUID,
        pinned: str,
        claimed_generation: int | None = None,
    ) -> None:
        """Milestone 3: rubric snapshot, the rubric_initialized audit event, and
        the job's terminal transition — all in ONE transaction. `claimed_generation`
        as in _m2: the locked persist refuses a workspace at any other generation.

        The status write lives here, not in a later step, for two reasons: a
        crash between them would re-run M3 and append a second audit event
        (which corrupts forward-replay of rubric edits), and a cancel landing in
        that gap would be overwritten by the success finalizer. The status write
        is a compare-and-set on 'running' so a concurrent cancel wins.

        Lock order: job (workspace_setup_jobs) THEN workspace THEN Call. Job
        first because the web `activate` route (rubric.py) locks
        workspace_setup_jobs before it flushes its workspace-row update —
        locking workspace before job here would invert against that route and
        open a real deadlock shape (Postgres 40P01). The delete path never
        locks setup-job rows, so putting job first costs delete nothing. The
        whole persist transaction below — acquisitions, rubric DML and commit
        alike — runs under _bounded_lock_waits, so no wait anywhere in it (the
        Call row, held for a whole ingestion batch's upsert loop; CallRubric's
        unique index) can pin the workspace or job row against a DELETE.
        """
        self._assert_live(job_id, workspace_id, claimed_generation)

        with self.session_factory() as s:
            ws = s.get(Workspace, workspace_id)
            call = s.get(Call, ws.call_id) if ws is not None else None
            if call is None:
                raise RuntimeError("workspace or call missing for rubric")
            call_id = call.id
            call_text = call.full_text or ""
            fresh = rubric_is_fresh(
                session=s, call=call, extractor_version=self.rubric_extractor_version
            )

        payload = None if fresh else compute_rubric(call_text=call_text, client=self.client)

        # _bounded_lock_waits wraps the WHOLE transaction, commit included: the
        # job and workspace rows are held from the acquisitions below until
        # COMMIT, and the rubric persist can wait on CallRubric's unique index.
        with self.session_factory() as s, _bounded_lock_waits(s):
            # job → ws → call: see the docstring's "Lock order" note.
            job = s.get(WorkspaceSetupJob, job_id, with_for_update=True)
            if job is None or job.status != "running":
                raise WorkspaceGoneDuringSetup(f"job {job_id} is no longer running (a cancel won)")

            ws = s.get(Workspace, workspace_id, with_for_update=True)
            if ws is None or ws.soft_deleted_at is not None:
                raise WorkspaceGoneDuringSetup(f"workspace {workspace_id} deleted")
            _refuse_stale_claim(ws, claimed_generation)
            call = s.get(Call, call_id, with_for_update=True)
            if call is None:
                raise RuntimeError(f"call {call_id} not found")
            if source_text_hash(call.full_text or "") != pinned:
                raise CallSourceChanged(
                    f"call {call_id} was re-ingested during setup of workspace {workspace_id}"
                )

            if payload is not None and not rubric_is_fresh(
                session=s, call=call, extractor_version=self.rubric_extractor_version
            ):
                persist_rubric(
                    session=s,
                    call=call,
                    payload=payload,
                    model=self.client.model,
                    extractor_version=self.rubric_extractor_version,
                    source_hash=pinned,
                )
                s.flush()

            template = s.get(CallRubric, call_id)
            if template is None:
                raise RuntimeError(f"rubric template for call {call_id} not found after fresh")

            ws.rubric_criteria = template.criteria
            ws.rubric_funder_notes = template.funder_notes
            ws.rubric_updated_at = datetime.now(UTC)

            s.add(
                WorkspaceEvent(
                    workspace_id=ws.id,
                    kind="rubric_initialized",
                    payload={
                        "criteria": template.criteria,
                        "funder_notes": template.funder_notes,
                        "seeded_from_call_rubrics_extracted_at": (
                            template.extracted_at.isoformat()
                        ),
                    },
                )
            )

            # Yield to the owner's rubric review. No completed_at — not terminal.
            job.status = "waiting_for_rubric_review"
            s.commit()

    def _fail(self, job_id: uuid.UUID, exc: BaseException) -> None:
        """Record the outcome of a failed run in its own short transaction.

        Order matters: a job that is already terminal is left alone, and a
        deleted/cancelled job goes terminal rather than back to 'queued'. The
        old code requeued unconditionally while attempts < MAX, which would send
        a cancelled job straight back into the queue.
        """
        now = datetime.now(UTC)
        with self.session_factory() as s:
            job = s.get(WorkspaceSetupJob, job_id, with_for_update=True)
            if job is None:
                logger.error("phase0 setup job %s vanished before failure write", job_id)
                return
            if job.status in ("cancelled", "failed", "done", "waiting_for_rubric_review"):
                return  # already terminal (or already yielded) — never resurrect

            row = s.execute(
                select(Workspace.id, Workspace.soft_deleted_at).where(
                    Workspace.id == job.workspace_id
                )
            ).one_or_none()
            gone = row is None
            deleted = None if gone else row.soft_deleted_at

            # Slice C1: the FRESH read decides, not the exception. A
            # WorkspaceGoneDuringSetup / WorkspaceDeleted raised while the
            # workspace was deleted is STALE once a restore has superseded it,
            # and restore has already put this job into a state that progresses.
            #
            # Return rather than falling through to the generic retry path:
            # that path runs `attempts += 1` below and dead-letters to 'failed'
            # at MAX_SETUP_ATTEMPTS (5). Charging the job an attempt — possibly
            # its last — for an exception the restore already answered is a
            # second bug, not a recovery.
            #
            # `gone` is separated from `deleted` because a NULL soft_deleted_at
            # means "live" OR "no such row", and only the first may return here.
            if (
                not gone
                and deleted is None
                and isinstance(exc, (WorkspaceGoneDuringSetup, WorkspaceDeleted))
            ):
                return

            job.attempts += 1
            job.last_error = f"{type(exc).__name__}: {exc}"

            # WorkspaceDeleted is _m1's own signal (raised by
            # acquire_commit_mutex_at_generation before anything is mutated);
            # WorkspaceGoneDuringSetup is _m2/_m3's. Both mean the same thing
            # here — route terminal, not just the locally-defined exception.
            if gone or deleted is not None:
                # Record the state we are cancelling FROM before overwriting it
                # (Slice C1, spec 5.2.2). The guard above already returned for
                # every terminal status, so this is one of the three the CHECK
                # allows.
                job.cancelled_from = job.status
                job.status = "cancelled"
                job.completed_at = now
                logger.info(
                    "phase0.cancelled",
                    extra={
                        "event": "phase0_cancelled",
                        "job_id": str(job_id),
                        "workspace_id": str(job.workspace_id),
                    },
                )
            elif job.attempts >= MAX_SETUP_ATTEMPTS:
                job.status = "failed"
                job.completed_at = now
                logger.exception(
                    "phase0 setup job %s failed permanently after %d attempts",
                    job_id,
                    job.attempts,
                )
            else:
                job.status = "queued"
                job.started_at = None
                logger.warning(
                    "phase0 setup job %s transient failure (attempt %d/%d): %s",
                    job_id,
                    job.attempts,
                    MAX_SETUP_ATTEMPTS,
                    exc,
                )
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
