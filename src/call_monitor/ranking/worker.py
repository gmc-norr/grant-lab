from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from call_monitor.db.models import Call, CallRanking, Profile
from call_monitor.ranking.ollama_client import LLMError, OllamaClient
from call_monitor.ranking.queue import (
    claim_pending_jobs,
    mark_done,
    mark_failed,
    requeue_stuck_running,
)
from call_monitor.ranking.rank_pair import RankResult
from call_monitor.ranking.rank_pair import rank_pair as default_rank_pair

_logger = logging.getLogger(__name__)

# Per spec: 5 attempts before dead-letter.
#
# Spec deviation (intentional): the spec describes exponential backoff
# (30s, 2m, 10m). v1 ships with FLAT retries — a failed job goes back to
# PENDING and the next scheduler tick (60s default) retries it. So the
# effective wait is one tick per attempt, regardless of attempt index.
# Acceptable at v1 because failure rates are low (Ollama is local; the
# JSON-mode contract eliminates parse churn). True exponential backoff
# would require a `next_attempt_at` column on ranking_jobs and a WHERE
# clause on `claim_pending_jobs` — straightforward to add when needed.
MAX_ATTEMPTS = 5

# Type for an injectable rank function (real one is rank_pair from
# rank_pair.py; tests inject a static double).
Ranker = Callable[..., RankResult]


def drain_queue(
    *,
    session_factory: sessionmaker,
    client: OllamaClient | None,
    ranker: Ranker = default_rank_pair,
    batch_size: int = 10,
) -> int:
    """Claim up to `batch_size` pending jobs, rank each, persist, mark
    done-or-failed. Returns the number of jobs processed (whether or not
    they succeeded).

    `client` is the OllamaClient. It's optional in tests where `ranker`
    is a static double that doesn't call the LLM.
    """
    # First: requeue any RUNNING jobs that have been stuck past the
    # threshold (a previous worker crashed mid-tick and never reached
    # mark_done/mark_failed). Without this, those jobs are invisible to
    # claim_pending_jobs forever.
    with session_factory() as session:
        requeue_stuck_running(session)
        session.commit()

    with session_factory() as session:
        jobs = claim_pending_jobs(session, limit=batch_size)
        if not jobs:
            return 0
        # Capture identifying fields before commit + close — we'll re-open
        # per-job so a single bad job doesn't poison the whole batch.
        claimed = [
            (job.id, job.call_id, job.profile_id, job.attempts) for job in jobs
        ]
        session.commit()

    processed = 0
    for job_id, call_id, profile_id, attempts in claimed:
        processed += 1
        try:
            with session_factory() as session:
                call = session.get(Call, call_id)
                profile = session.get(Profile, profile_id)
                if call is None or profile is None:
                    # Either deleted between enqueue and run — dead-letter.
                    mark_failed(
                        session,
                        job_id,
                        error="call or profile vanished",
                        terminal=True,
                    )
                    session.commit()
                    continue
                if not profile.is_active:
                    # Profile was soft-deleted after the job was enqueued.
                    # Spec: inactive profiles are out of ranking backfills —
                    # don't burn Ollama capacity or refresh call_rankings
                    # for a profile that shouldn't be in circulation.
                    # Mark the job DONE (resolved without work) rather than
                    # FAILED (didn't actually fail) so the dead-letter queue
                    # stays meaningful.
                    mark_done(session, job_id)
                    session.commit()
                    _logger.info(
                        "ranking.job_skipped_inactive_profile",
                        extra={
                            "event": "ranking_job_skipped_inactive",
                            "job_id": job_id,
                            "profile_id": profile_id,
                        },
                    )
                    continue
                result = ranker(client=client, profile=profile, call=call)
                session.merge(
                    CallRanking(
                        call_id=call_id,
                        profile_id=profile_id,
                        score=result.score,
                        rationale=result.rationale,
                        model=result.model,
                        prompt_version=result.prompt_version,
                        raw_response=result.raw_response,
                        # Explicitly set ranked_at so re-rank refreshes the
                        # timestamp. Otherwise session.merge() on the
                        # composite-PK row sees no change to ranked_at and
                        # the column keeps its first-insert server_default
                        # value forever — the spec expects ranked_at to
                        # reflect the most recent ranking event.
                        ranked_at=datetime.now(UTC),
                    )
                )
                mark_done(session, job_id)
                session.commit()
        except LLMError as e:
            terminal = (attempts + 1) >= MAX_ATTEMPTS
            with session_factory() as session:
                mark_failed(session, job_id, error=str(e), terminal=terminal)
                session.commit()
            _logger.warning(
                "ranking.job_failed",
                extra={
                    "event": "ranking_job_failed",
                    "job_id": job_id,
                    "attempts": attempts + 1,
                    "terminal": terminal,
                    "error": str(e),
                },
            )
        except Exception as e:  # noqa: BLE001 — worker must not crash the scheduler
            terminal = (attempts + 1) >= MAX_ATTEMPTS
            with session_factory() as session:
                mark_failed(
                    session, job_id, error=f"{type(e).__name__}: {e}", terminal=terminal
                )
                session.commit()
            _logger.exception(
                "ranking.job_unexpected",
                extra={
                    "event": "ranking_job_unexpected",
                    "job_id": job_id,
                    "attempts": attempts + 1,
                    "terminal": terminal,
                },
            )
    return processed
