"""Bootstrap-job worker — drains profile_bootstrap_jobs.

Mirrors run_phase0_tick: claim one queued job (FOR UPDATE SKIP LOCKED), run the
never-raising bootstrap, branch on the explicit outcome. ALL post-claim work is
wrapped so any exception transitions the job immediately (queued/failed) instead
of leaving it stuck 'running'. attempts is counted once at claim; exhaustion and
the reaper use `attempts >= MAX` (no +1)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from call_manager.db.models import Profile, ProfileBootstrapJob
from call_manager.embeddings.refresh import refresh_profile_embedding
from call_manager.profiles.bootstrap import bootstrap_profile
from call_manager.profiles.create import merge_bootstrap_into_profile
from call_manager.profiles.seed import Member, SeedInput
from call_manager.ranking.backfill import backfill_profile

logger = logging.getLogger(__name__)

MAX_BOOTSTRAP_ATTEMPTS = 5
STUCK_BOOTSTRAP_AFTER = timedelta(minutes=30)


def _seed_from_payload(payload: dict) -> SeedInput:
    members = []
    for m in payload.get("members", []):
        try:
            members.append(
                Member(orcid=m.get("orcid"), name=m.get("name"), affiliation=m.get("affiliation"))
            )
        except ValueError:
            # Payload was validated at creation (_parse_members_strict); a bad
            # entry here means corrupt stored data — drop it, don't abort the job.
            continue
    return SeedInput(
        name=payload.get("name", ""),
        kind=payload.get("kind", "organizational_unit"),
        level=payload.get("level"),
        members=members,
        pasted_grants=payload.get("pasted_grants", ""),
        mission_text=payload.get("mission_text", ""),
    )


def requeue_stuck_bootstrap_jobs(
    session: Session, *, max_attempts: int = MAX_BOOTSTRAP_ATTEMPTS
) -> int:
    """Recover jobs stuck 'running' past STUCK_BOOTSTRAP_AFTER (worker crash).
    attempts was already counted at claim -> dead-letter at `attempts >= max`,
    do NOT re-increment. Caller commits."""
    # NB: unlike requeue_stuck_setup_jobs, we do NOT increment attempts here —
    # the claim in run_profile_bootstrap_tick already counted this attempt before
    # the crash. Re-incrementing would double-count and dead-letter a tick early.
    cutoff = datetime.now(UTC) - STUCK_BOOTSTRAP_AFTER
    dead = session.execute(
        update(ProfileBootstrapJob)
        .where(
            ProfileBootstrapJob.status == "running",
            ProfileBootstrapJob.started_at.is_not(None),
            ProfileBootstrapJob.started_at < cutoff,
            ProfileBootstrapJob.attempts >= max_attempts,
        )
        .values(
            status="failed",
            last_error="dead-lettered: stuck running (worker crash)",
            seed=None,
            completed_at=datetime.now(UTC),
        )
    ).rowcount
    session.execute(
        update(ProfileBootstrapJob)
        .where(
            ProfileBootstrapJob.status == "running",
            ProfileBootstrapJob.started_at.is_not(None),
            ProfileBootstrapJob.started_at < cutoff,
        )
        .values(status="queued", started_at=None)
    )
    return dead


def _finalize_failure(session_factory, job_id, attempts: int, msg: str) -> None:
    with session_factory() as s:
        job = s.get(ProfileBootstrapJob, job_id)
        if job is None:
            return
        if attempts >= MAX_BOOTSTRAP_ATTEMPTS:
            job.status, job.completed_at, job.last_error, job.seed = (
                "failed",
                datetime.now(UTC),
                msg,
                None,
            )
        else:
            job.status, job.started_at, job.last_error = "queued", None, msg
        s.commit()


def run_profile_bootstrap_tick(
    *, session_factory: sessionmaker, pubmed, ollama, embed_client
) -> None:
    with session_factory() as s:
        dead = requeue_stuck_bootstrap_jobs(s)
        s.commit()
    if dead:
        logger.warning(
            "profiles.bootstrap.reaped_stuck",
            extra={"event": "bootstrap_reaped_stuck", "dead_lettered": dead},
        )

    with session_factory() as s:
        job = s.execute(
            select(ProfileBootstrapJob)
            .where(ProfileBootstrapJob.status == "queued")
            .order_by(ProfileBootstrapJob.queued_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        ).scalar_one_or_none()
        if job is None:
            return
        job.status, job.started_at, job.attempts = "running", datetime.now(UTC), job.attempts + 1
        job_id, profile_id, attempts = job.id, job.profile_id, job.attempts
        seed_payload = dict(job.seed or {})
        s.commit()

    # Crash-recovery: if this profile already has a description from a previous
    # run that crashed after merge but before marking 'done', skip the expensive
    # bootstrap (PubMed + LLM) and go straight to backfill + done.
    with session_factory() as s:
        _profile_now = s.get(Profile, profile_id)
        already_bootstrapped = bool(_profile_now and (_profile_now.description or "").strip())

    try:
        if not already_bootstrapped:
            draft = bootstrap_profile(
                seed=_seed_from_payload(seed_payload), pubmed=pubmed, ollama=ollama
            )
            if not draft.llm_succeeded:
                _finalize_failure(session_factory, job_id, attempts, "llm draft failed or empty")
                return
            # Merge + embed in one committed session; mark 'done' only AFTER backfill
            # completes, so the job stays 'running' (recoverable) while backfill runs.
            with session_factory() as s:
                merge_bootstrap_into_profile(s.get(Profile, profile_id), draft)
                refresh_profile_embedding(s, embed_client, profile_id)
                s.commit()
        backfill_profile(session_factory=session_factory, profile_id=profile_id)
        with session_factory() as s:
            job = s.get(ProfileBootstrapJob, job_id)
            if not already_bootstrapped:
                job.pubmed_status, job.publications_fetched, job.seed = (
                    draft.pubmed_status,
                    draft.publications_fetched,
                    None,
                )
            else:
                job.seed = None
            job.status, job.completed_at = "done", datetime.now(UTC)
            s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "profiles.bootstrap.worker_failed",
            extra={"event": "bootstrap_worker_failed", "profile_id": profile_id, "error": str(e)},
        )
        _finalize_failure(session_factory, job_id, attempts, f"{type(e).__name__}: {e}")
