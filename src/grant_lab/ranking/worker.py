from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import sessionmaker

from grant_lab.db.models import Call, CallRanking, CallRankingCriterion, Profile, RankingJob
from grant_lab.profiles.fingerprint import embedding_fingerprint, rank_fingerprint
from grant_lab.ranking.criteria import GateStatus
from grant_lab.ranking.ollama_client import LLMError, OllamaClient
from grant_lab.ranking.prompt import ParseError
from grant_lab.ranking.queue import (
    MAX_ATTEMPTS,
    claim_pending_jobs,
    mark_done,
    mark_failed,
    requeue_stuck_running,
    revert_to_pending,
    touch_running_job,
)
from grant_lab.ranking.rank_pair import RankResult, rank_pair
from grant_lab.ranking.remote_attempts import count_today, record_attempt

_logger = logging.getLogger(__name__)

# MAX_ATTEMPTS is the single source of truth in queue.py (B11 Defect 5) and
# re-exported here for the worker's terminal-attempt checks below. queue.py
# carries the full rationale (flat-retry deviation from the spec's backoff).

# A pair whose profile vector has not caught up is deferred rather than ranked
# against a stale cosine. Bounded because refresh_profile_embedding with
# force=False PRESERVES the old vector on an embedder outage, so the mismatch
# would otherwise persist forever. 24h is comfortably more than one 03:30
# embedding-tick cycle.
_STALE_VECTOR_MAX_DEFER_HOURS: int = 24


def drain_queue(
    *,
    session_factory: sessionmaker,
    client: OllamaClient | None,
    embedding_threshold: float,
    embedding_hard_fail_threshold: float | None = None,
    remote_client=None,  # AnthropicClient | None
    escalation_min_score: int = 45,
    remote_daily_cap: int = 100,
    batch_size: int = 10,
    tick_budget_seconds: float | None = None,
    _clock=None,
) -> int:
    """Claim up to `batch_size` pending jobs, rank each, persist, mark
    done-or-failed. Returns the number of jobs the loop CONSUMED — successes,
    failures, and jobs deferred back to PENDING for a stale vector alike.

    A deferral counts, so this return is not "jobs that reached a terminal
    state". A caller written as `while drain_queue(...) > 0:` would spin on a
    pair that keeps deferring; the only production caller (`scheduler.py`) is
    interval-driven and discards the value.

    `tick_budget_seconds` is a SOFT per-tick wall-clock budget (B11 Defect 6).
    A single job can take ~240s (local Gemma + a Sonnet escalation); an
    escalation-heavy batch_size=10 could otherwise monopolize the scheduler
    slot for minutes. Once the elapsed time since claiming exceeds the budget,
    the loop stops processing and reverts the remaining claimed jobs to PENDING
    (no attempt burned) so the next tick picks them up. The budget is checked
    BEFORE each job, so a job already in flight always completes — it is a stop
    line, not a hard kill. None disables the budget (process the whole batch).
    `_clock` is an injection seam for tests (defaults to time.monotonic).

    `embedding_threshold` is the cosine cutoff at which the embedding
    pre-gate PASSES. Sole source of truth:
    Settings.embedding_pregate_threshold (callers in scheduler.py /
    main.py read it from there).

    `embedding_hard_fail_threshold` is the cutoff below which the gate
    short-circuits to FAIL (no LLM). Pairs in the [hard_fail, threshold)
    band surface as UNKNOWN and are adjudicated by the LLM — Codex
    finding #1. When None the gate collapses to single-cut legacy
    behaviour; in production this is always threaded from
    Settings.embedding_hard_fail_threshold.

    `escalation_min_score` is the local-score floor at which rank_pair
    escalates to the remote (Sonnet) client — forwarded verbatim.

    `remote_daily_cap` bounds remote spend: when today's ledger row count
    (ranking_remote_attempts, Europe/Stockholm day) has reached the cap,
    the job runs with `remote_client=None` — pure local, no escalation.
    Every attempt rank_pair actually makes (any outcome, success or
    failure — both billed) is recorded as one ledger row.
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
        claimed = [(job.id, job.call_id, job.profile_id, job.attempts) for job in jobs]
        session.commit()

    if _clock is None:
        import time

        _clock = time.monotonic
    tick_start = _clock()

    processed = 0
    for idx, (job_id, call_id, profile_id, attempts) in enumerate(claimed):
        # Soft per-tick budget (B11 Defect 6): once exceeded, stop and hand the
        # remaining claimed-but-untouched jobs back to PENDING (no attempt
        # burned) so the next tick processes them. Checked BEFORE the job so a
        # job already in flight always finishes.
        if tick_budget_seconds is not None and (_clock() - tick_start) > tick_budget_seconds:
            remaining = [c[0] for c in claimed[idx:]]
            with session_factory() as session:
                reverted = revert_to_pending(session, remaining)
                session.commit()
            _logger.info(
                "ranking.tick_budget_exceeded",
                extra={
                    "event": "ranking_tick_budget_exceeded",
                    "processed": processed,
                    "reverted_to_pending": reverted,
                    "budget_seconds": tick_budget_seconds,
                },
            )
            break
        processed += 1
        try:
            with session_factory() as session:
                # Heartbeat the running job so a long serial batch keeps this
                # row "fresh" and the stuck-reaper won't requeue an in-flight
                # job (which would double-process it once ticks could overlap).
                touch_running_job(session, job_id)
                call = session.get(Call, call_id)
                profile = session.get(Profile, profile_id)
                if call is None or profile is None:
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
                # Defer rather than rank against a vector that has not caught
                # up. post_edit.py:33-34 states the ordering the request path
                # honours ("the ranking jobs must see the post-edit vector");
                # the repair path has no equivalent, so a pair could be ranked
                # early, stamped with the current text fingerprint, and become
                # permanently invisible to repair.
                #
                # A NULL fingerprint is unknown provenance, NOT a mismatch: on
                # the first deploy every profile has a vector and a NULL
                # fingerprint, and deferring on that would pause all ranking
                # until the 03:30 tick. The pair is stamped with
                # embedding_fingerprint=None in its payload, so it re-ranks
                # itself once the tick fills the real value.
                job_row = session.get(RankingJob, job_id)
                too_old = job_row is not None and job_row.created_at < datetime.now(
                    UTC
                ) - timedelta(hours=_STALE_VECTOR_MAX_DEFER_HOURS)
                if (
                    profile.embedding is not None
                    and profile.embedding_fingerprint is not None
                    and profile.embedding_fingerprint != embedding_fingerprint(profile)
                    and not too_old
                ):
                    revert_to_pending(session, [job_id])
                    # revert_to_pending issues an UPDATE and does NOT commit.
                    # Without this the revert rolls back on session close, the
                    # job stays RUNNING on its committed claim, and
                    # requeue_stuck_running bumps attempts and eventually
                    # dead-letters the pair -- burning the very retry this
                    # deferral exists to protect.
                    session.commit()
                    _logger.info(
                        "ranking.job_deferred_stale_vector",
                        extra={
                            "event": "ranking_job_deferred_stale_vector",
                            "job_id": job_id,
                            "profile_id": profile_id,
                        },
                    )
                    continue
                # Daily-cap gate: once today's ledger has `remote_daily_cap`
                # attempts, disable escalation for the rest of the day by
                # passing remote_client=None — rank_pair then never consults
                # the remote backend. Counted per attempt (not per success)
                # because failures are billed too (Codex R1).
                effective_remote = remote_client
                if remote_client is not None and count_today(session) >= remote_daily_cap:
                    effective_remote = None
                # Capture BEFORE the LLM call. Today `profile` cannot change
                # between here and the merge below -- the session is not
                # committed, refreshed or expired, and the spend ledger uses a
                # separate session on purpose -- so this is equivalent to
                # hashing at write time. It is captured here anyway so that a
                # future refactor introducing a commit or a session.refresh in
                # between cannot silently stamp a profile the ranker never saw.
                captured_fingerprint = rank_fingerprint(profile)
                try:
                    result = rank_pair(
                        client=client,
                        profile=profile,
                        call=call,
                        now=datetime.now(UTC),
                        embedding_threshold=embedding_threshold,
                        embedding_hard_fail_threshold=embedding_hard_fail_threshold,
                        remote_client=effective_remote,
                        escalation_min_score=escalation_min_score,
                    )
                except (LLMError, ParseError) as e:
                    terminal = (attempts + 1) >= MAX_ATTEMPTS
                    mark_failed(
                        session,
                        job_id,
                        error=f"ranking failed: {e}",
                        terminal=terminal,
                    )
                    session.commit()
                    continue

                # Ledger in its OWN committed short transaction, BEFORE the
                # main merge/commit (B11 Defect 2). rank_pair has already made
                # the billed remote call; if we deferred the ledger row to the
                # main transaction and that commit then failed (deadlock,
                # transient connection drop), the row would roll back, the job
                # would revert to PENDING, and the next tick would re-run the
                # FULL rank_pair — a SECOND paid Sonnet call — none of them
                # counted. Committing the ledger here makes spend honest even
                # when the main commit fails: each actual rank_pair invocation
                # records exactly one ledger row. Worst case is one already-paid
                # attempt whose ranking didn't persist (retried next tick) —
                # bounded by MAX_ATTEMPTS — rather than uncounted re-billing.
                if result.escalation_attempted and result.escalation_outcome is not None:
                    with session_factory() as ledger_session:
                        record_attempt(
                            ledger_session,
                            call_id=call_id,
                            profile_id=profile_id,
                            model=getattr(remote_client, "model", "") or result.model,
                            outcome=result.escalation_outcome,
                        )
                        ledger_session.commit()

                # Cosine is written verbatim — a None current value
                # OVERWRITES any stale prior one (Codex R4: once cosine
                # affects ordering, preserving a model-mismatch-era value
                # would silently rank on a meaningless number).
                new_sim = result.embed_similarity

                # Preserve a previously-recorded analysis when this run
                # produced None (e.g. a re-rank under Gemma after a
                # previous Sonnet run, or a gate-fail re-rank where the
                # LLM never ran). Without this, dropping below the
                # cosine threshold or short-circuiting on a new gate
                # would silently clobber the richer Sonnet analysis with
                # NULLs. analysis_model travels with the preserved text
                # so attribution can never point at the wrong backend.
                new_summary = result.summary
                new_notes = result.applicant_notes
                new_analysis_model = result.analysis_model
                # The analysis is ONE attributable unit (summary + applicant_notes
                # + analysis_model). Preserve the prior run's analysis ONLY when
                # THIS run produced none of it (both fields None — e.g. a gate-fail
                # re-rank where the LLM never ran, or a cosine short-circuit), and
                # restore the triple TOGETHER. A run that produced EITHER field
                # keeps its own triple verbatim — otherwise a partial result would
                # graft this run's summary onto the prior run's applicant_notes
                # under a single analysis_model, mis-attributing one of them.
                if new_summary is None and new_notes is None:
                    existing = session.get(CallRanking, (call_id, profile_id))
                    # Only inherit advice that describes THIS profile. After an
                    # edit the retained triple describes the old one, and
                    # stamping it with the new fingerprint would make it
                    # permanently invisible to repair.
                    if (
                        existing is not None
                        and existing.profile_fingerprint == captured_fingerprint
                    ):
                        new_summary = existing.summary
                        new_notes = existing.applicant_notes
                        new_analysis_model = existing.analysis_model

                session.merge(
                    CallRanking(
                        call_id=call_id,
                        profile_id=profile_id,
                        aggregate_score=result.score,
                        aggregate_reason=result.reason,
                        rationale=_rank_summary(result),
                        model=result.model,
                        prompt_version=result.prompt_version,
                        raw_response=result.raw_response or {},
                        embed_similarity=new_sim,
                        topic_fit_expected=result.topic_fit_expected,
                        summary=new_summary,
                        applicant_notes=new_notes,
                        analysis_model=new_analysis_model,
                        escalated=result.escalated,
                        escalated_from_score=result.escalated_from_score,
                        profile_fingerprint=captured_fingerprint,
                        ranked_at=datetime.now(UTC),
                    )
                )
                _write_criteria_rows(session, call_id, profile_id, result)
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
                mark_failed(session, job_id, error=f"{type(e).__name__}: {e}", terminal=terminal)
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


def _rank_summary(r: RankResult) -> str:
    """One-line summary for the rationale column (the per-criterion
    rationales live in CallRankingCriterion).

    When must_include overrode a failing timeline gate, the summary
    explicitly notes that — otherwise users see a high score on a
    deadline-imminent call with no warning."""
    if r.reason == "embedding":
        # Surface the cosine value to 3 decimals so admins can see exactly
        # how far the call sat below the threshold and tune accordingly.
        sim_str = f"{r.embed_similarity:.3f}" if r.embed_similarity is not None else "n/a"
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


def _write_criteria_rows(session, job_call_id, job_profile_id, r: RankResult) -> None:
    """Write sparse CallRankingCriterion rows. Only the criteria actually
    evaluated up to (and including) the failing gate are inserted.

    Special case: when r.must_include_override is True AND the failing gate
    was timeline OR embedding, we DO continue past that row to write the
    subsequent rows (and the LLM rows when reached), because the LLM was
    actually called (rank_pair's must_include bypass extends the bypass to
    the embedding gate, mirroring the timeline bypass).

    Deletes prior-run rows first so the breakdown UI never displays a
    mix of fresh and stale criteria. Wrapped in the same transaction as
    the matching CallRanking.merge() so the headline score and the
    breakdown can never diverge."""
    from sqlalchemy import delete

    # Wipe any prior-run rows for this (call, profile). Sparse writes that
    # follow re-establish the current state. Without this, a previous
    # run's LLM rows persist when this run gate-fails before the LLM step
    # (e.g. profile budget changed and now budget gate FAILs).
    session.execute(
        delete(CallRankingCriterion).where(
            CallRankingCriterion.call_id == job_call_id,
            CallRankingCriterion.profile_id == job_profile_id,
        )
    )
    session.flush()

    now = datetime.now(UTC)

    # Gate rows — write up to and including the failing one (or all if no gate failed).
    # Iteration order MUST match rank_pair's / aggregate's short-circuit
    # precedence: budget → trl → timeline → embedding. When several gates
    # FAIL, aggregate() picks the *first* in that precedence as the headline
    # aggregate_reason; if this loop iterated in a different order it would
    # stop at (and write a FAIL row for) a different gate than the headline,
    # so the breakdown's terminal FAIL row would contradict aggregate_reason.
    # Keeping the two orders identical guarantees the row we stop at is the
    # one the headline reason names. (The breakdown UI renders rows in this
    # written order — embedding still lands last, after the cheap structural
    # gates it depends on having passed.)
    gate_results = [
        ("budget", r.gates.budget),
        ("trl", r.gates.trl),
        ("timeline", r.gates.timeline),
        ("embedding", r.gates.embedding),
    ]
    for name, gr in gate_results:
        session.add(
            CallRankingCriterion(
                call_id=job_call_id,
                profile_id=job_profile_id,
                criterion=name,
                rating=gr.status.value,
                # 'too_soon' | 'expired' | 'currency_mismatch' | 'topic_dissimilar' | None
                rationale=gr.reason,
                quoted_evidence=None,
                ranked_at=now,
            )
        )
        if gr.status == GateStatus.FAIL:
            # Continue past timeline OR embedding FAIL when must_include_override
            # is set — rank_pair bypasses both for must_include matches and
            # the LLM was actually called, so we have parsed criteria to write.
            if name in ("timeline", "embedding") and r.must_include_override:
                continue
            return  # stop — subsequent criteria not evaluated

    # LLM rows — only if we got that far (parsed is not None).
    if r.parsed is None:
        return
    session.add(
        CallRankingCriterion(
            call_id=job_call_id,
            profile_id=job_profile_id,
            criterion="topic_fit",
            rating=str(r.parsed.topic_fit_rating),
            rationale=r.parsed.topic_fit_rationale,
            quoted_evidence=r.parsed.topic_fit_quoted,
            ranked_at=now,
        )
    )
    session.add(
        CallRankingCriterion(
            call_id=job_call_id,
            profile_id=job_profile_id,
            criterion="eligibility",
            rating=r.parsed.eligibility_rating,
            rationale=r.parsed.eligibility_rationale,
            quoted_evidence=r.parsed.eligibility_quoted,
            ranked_at=now,
        )
    )
    session.add(
        CallRankingCriterion(
            call_id=job_call_id,
            profile_id=job_profile_id,
            criterion="exclusions",
            rating=r.parsed.exclusions_rating,
            rationale=r.parsed.exclusions_rationale,
            quoted_evidence=r.parsed.exclusions_quoted,
            ranked_at=now,
        )
    )
