from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from call_monitor.db.models import Call, CallRanking, CallRankingCriterion, Profile
from call_monitor.ranking.criteria import GateStatus
from call_monitor.ranking.ollama_client import LLMError, OllamaClient
from call_monitor.ranking.prompt import ParseV2Error
from call_monitor.ranking.queue import (
    claim_pending_jobs,
    mark_done,
    mark_failed,
    requeue_stuck_running,
)
from call_monitor.ranking.rank_pair import RankResult, RankResultV2, rank_pair_v2
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
            (job.id, job.call_id, job.profile_id, job.attempts, job.scheme)
            for job in jobs
        ]
        session.commit()

    processed = 0
    for job_id, call_id, profile_id, attempts, scheme in claimed:
        processed += 1
        try:
            if scheme == "v2":
                try:
                    with session_factory() as session:
                        call = session.get(Call, call_id)
                        profile = session.get(Profile, profile_id)
                        if call is None or profile is None:
                            mark_failed(
                                session, job_id,
                                error="call or profile vanished",
                                terminal=True,
                            )
                            session.commit()
                            continue
                        if not profile.is_active:
                            mark_done(session, job_id)
                            session.commit()
                            continue
                        try:
                            v2_result = rank_pair_v2(
                                client=client,
                                profile=profile,
                                call=call,
                                now=datetime.now(UTC),
                            )
                        except (LLMError, ParseV2Error) as e:
                            terminal = (attempts + 1) >= MAX_ATTEMPTS
                            mark_failed(
                                session, job_id,
                                error=f"v2 ranking failed: {e}",
                                terminal=terminal,
                            )
                            session.commit()
                            continue

                        # Preserve a previously-recorded embed_similarity when
                        # this run produced None (e.g. degraded re-rank: the
                        # embedding model was swapped, profile/call vectors
                        # disagree, _safe_cosine returns None → UNKNOWN gate
                        # → LLM still ran). session.merge writes all
                        # attributes unconditionally, so without this the
                        # historical diagnostic value would be silently
                        # clobbered, leaving operators no signal to tune the
                        # cosine threshold against.
                        new_sim = v2_result.embed_similarity
                        if new_sim is None:
                            existing = session.get(
                                CallRanking, (call_id, profile_id, "v2")
                            )
                            if existing is not None:
                                new_sim = existing.embed_similarity

                        session.merge(CallRanking(
                            call_id=call_id,
                            profile_id=profile_id,
                            scheme="v2",
                            aggregate_score=v2_result.score,
                            aggregate_reason=v2_result.reason,
                            rationale=_v2_summary(v2_result),
                            model=v2_result.model,
                            prompt_version=v2_result.prompt_version,
                            raw_response=v2_result.raw_response or {},
                            embed_similarity=new_sim,
                            ranked_at=datetime.now(UTC),
                        ))
                        _write_v2_criteria_rows(session, call_id, profile_id, v2_result)
                        mark_done(session, job_id)
                        session.commit()
                except Exception as e:  # noqa: BLE001 — worker must not crash the scheduler
                    terminal = (attempts + 1) >= MAX_ATTEMPTS
                    with session_factory() as session:
                        mark_failed(
                            session, job_id,
                            error=f"{type(e).__name__}: {e}",
                            terminal=terminal,
                        )
                        session.commit()
                    _logger.exception(
                        "ranking.v2_job_unexpected",
                        extra={"event": "ranking_v2_job_unexpected", "job_id": job_id,
                               "attempts": attempts + 1, "terminal": terminal},
                    )
                continue

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
                        scheme=scheme,  # always 'v1' here (we continue'd above for v2)
                        aggregate_score=result.score,
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


def _v2_summary(r: RankResultV2) -> str:
    """One-line summary for the rationale column (v2's free-form text is
    the per-criterion rationales, which live in CallRankingCriterion).

    When must_include overrode a failing timeline gate, the summary
    explicitly notes that — otherwise users see a high score on a
    deadline-imminent call with no warning."""
    if r.reason == "embedding":
        # Surface the cosine value to 3 decimals so admins can see exactly
        # how far the call sat below the threshold and tune accordingly.
        sim_str = (
            f"{r.embed_similarity:.3f}" if r.embed_similarity is not None else "n/a"
        )
        return f"FAIL: topic_dissimilar (cosine {sim_str})"
    if r.parsed is None:
        return f"Short-circuited: {r.reason}"
    base = (
        f"Topic {r.parsed.topic_fit_rating}/5 · "
        f"Eligibility: {r.parsed.eligibility_rating} · "
        f"Exclusions: {r.parsed.exclusions_rating}"
    )
    if r.must_include_override and r.gates.timeline.status == GateStatus.FAIL:
        base += f" · (must-include override; timeline: {r.gates.timeline.reason})"
    return base


def _write_v2_criteria_rows(session, job_call_id, job_profile_id, r: RankResultV2) -> None:
    """Write sparse CallRankingCriterion rows. Only the criteria actually
    evaluated up to (and including) the failing gate are inserted. Plan A's
    schema makes the table accept inserts; this function is the only writer.

    Special case: when r.must_include_override is True AND the failing gate
    was timeline OR embedding, we DO continue past that row to write the
    subsequent rows (and the LLM rows when reached), because the LLM was
    actually called (rank_pair_v2's must_include bypass — Plan D extends
    the bypass to the embedding gate, mirroring the timeline bypass).

    Deletes prior-run rows first so the breakdown UI never displays a
    mix of fresh and stale criteria. Wrapped in the same transaction as
    the matching CallRanking.merge() so the headline score and the
    breakdown can never diverge."""
    from sqlalchemy import delete

    # Wipe any prior-run rows for this (call, profile, scheme=v2). Sparse
    # writes that follow re-establish the current state. Without this,
    # a previous run's LLM rows persist when this run gate-fails before
    # the LLM step (e.g. profile budget changed and now budget gate FAILs).
    session.execute(
        delete(CallRankingCriterion).where(
            CallRankingCriterion.call_id == job_call_id,
            CallRankingCriterion.profile_id == job_profile_id,
            CallRankingCriterion.scheme == "v2",
        )
    )
    session.flush()

    now = datetime.now(UTC)

    # Gate rows — write up to and including the failing one (or all if no gate failed).
    # Order matches rank_pair_v2's evaluation order (budget/trl unconditional,
    # then timeline, then embedding) — but the visual order in the breakdown
    # UI follows this list. We keep timeline/budget/trl first (the historical
    # display order) and append embedding at the end so the embedding row
    # always appears after the cheap structural gates it depends on having
    # passed.
    gate_results = [
        ("timeline",  r.gates.timeline),
        ("budget",    r.gates.budget),
        ("trl",       r.gates.trl),
        ("embedding", r.gates.embedding),
    ]
    for name, gr in gate_results:
        session.add(CallRankingCriterion(
            call_id=job_call_id, profile_id=job_profile_id, scheme="v2",
            criterion=name,
            rating=gr.status.value,
            rating_expected=None,
            # 'too_soon' | 'expired' | 'currency_mismatch' | 'topic_dissimilar' | None
            rationale=gr.reason,
            quoted_evidence=None,
            ranked_at=now,
        ))
        if gr.status == GateStatus.FAIL:
            # Continue past timeline OR embedding FAIL when must_include_override
            # is set — rank_pair_v2 bypasses both for must_include matches and
            # the LLM was actually called, so we have parsed criteria to write.
            if name in ("timeline", "embedding") and r.must_include_override:
                continue
            return  # stop — subsequent criteria not evaluated

    # LLM rows — only if we got that far (parsed is not None).
    if r.parsed is None:
        return
    session.add(CallRankingCriterion(
        call_id=job_call_id, profile_id=job_profile_id, scheme="v2",
        criterion="topic_fit",
        rating=str(r.parsed.topic_fit_rating),
        rating_expected=r.topic_expected,
        rationale=r.parsed.topic_fit_rationale,
        quoted_evidence=r.parsed.topic_fit_quoted,
        ranked_at=now,
    ))
    session.add(CallRankingCriterion(
        call_id=job_call_id, profile_id=job_profile_id, scheme="v2",
        criterion="eligibility",
        rating=r.parsed.eligibility_rating,
        rating_expected=None,
        rationale=r.parsed.eligibility_rationale,
        quoted_evidence=r.parsed.eligibility_quoted,
        ranked_at=now,
    ))
    session.add(CallRankingCriterion(
        call_id=job_call_id, profile_id=job_profile_id, scheme="v2",
        criterion="exclusions",
        rating=r.parsed.exclusions_rating,
        rating_expected=None,
        rationale=r.parsed.exclusions_rationale,
        quoted_evidence=r.parsed.exclusions_quoted,
        ranked_at=now,
    ))
