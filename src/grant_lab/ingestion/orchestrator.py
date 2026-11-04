from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from grant_lab.db.models import Call, Source
from grant_lab.embeddings.client import OllamaEmbedClient
from grant_lab.embeddings.refresh import (
    CallEmbedOutcome,
    refresh_call_embedding_with_outcome,
)
from grant_lab.ingestion.alerts import FailureAlerter
from grant_lab.ingestion.upsert import upsert_call
from grant_lab.ranking.freshness import FreshnessContext
from grant_lab.ranking.queue import bulk_enqueue_for_calls
from grant_lab.sources.types import AdapterError, SourceAdapter

_logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass
class IngestionResult:
    source_slug: str
    inserted: int
    updated: int
    # Touched calls step 2 did NOT re-embed because their vector, model and
    # fingerprint were already current. Reported so a working skip is
    # distinguishable from a step 2 that silently stopped running: the defect
    # the skip closed was found as "419 POST /api/embed for 0 inserts" in a
    # log, and after the fix both states show zero embeds. A COUNT, not
    # per-call logging — a 764-call night must not become 764 log lines.
    calls_skipped_current: int
    error: str | None  # None on success, message on failure
    # Open rows this run CLOSED because the source said so (NormalizedCall.closed).
    closed: int = 0


def run_ingestion(
    *,
    session_factory: sessionmaker,
    adapter: SourceAdapter,
    alerter: FailureAlerter,
    embed_client: OllamaEmbedClient,
    freshness: FreshnessContext,
) -> IngestionResult:
    """Run one adapter end-to-end.

    Contract:
    - Reads `sources.last_fetched_at` to pass to `adapter.fetch_new_calls()`.
    - Per call: `upsert_call`. Counts inserts vs. updates.
    - On success: advances `sources.last_fetched_at` to run-start time, commits.
    - On failure (data step): rolls back the data step; does NOT advance the timestamp.
    - Notifies the alerter either way (success resets streak; failure increments).

    Codex H1 fix — three serialized steps:

      Step 1 (one transaction): upsert calls + advance `last_fetched_at`.
        On adapter or DB failure here, the whole step rolls back, no jobs
        get queued, alerter records failure, we exit.

      Step 2 (per-row transactions, best-effort): embed every touched call.
        A single embed failure is logged and skipped; the loop continues.
        We do NOT abort the run on embed failure — the ranking worker has an
        UNKNOWN gate that falls through to the LLM, so a missing embedding
        is a graceful degradation (LLM still runs as the safe fallback).

      Step 3 (one transaction): enqueue the touched calls' STALE pairs
        (ranking/freshness.py).

    The split exists so the ranking worker (which polls every 60s) never
    sees a job whose call/profile pair has a NULL embedding because the
    embedding refresh hadn't happened yet. Before this split, upsert + enqueue
    shared one transaction and the embed step was done post-commit, leaving
    a window where the worker could pick up a job and silently fall through
    to LLM with `embed_similarity = NULL` permanently.

    Atomicity caveats — worth being explicit about, since the previous version
    was atomic across upsert+enqueue:
      - Step 1 succeeds, process killed before step 2/3 → calls inserted,
        no jobs queued. Next ingest tick won't re-discover the same calls
        (last_fetched_at was advanced atomically with the inserts; upsert
        is a no-op on no-change anyway). The daily 03:30 refresh-missing tick
        catches the missing embeddings, and the 04:00 reconcile
        (reconcile_missing_rankings) enqueues every stale pair over all open
        calls -- a pair with no ranking is stale -- so no manual rerank is
        needed.
      - Step 2 partial failure (one embed call raises) → loop continues to
        next call; step 3 still runs and enqueues that call's pairs if they
        are stale (a new or changed call's always are); a missing vector
        goes through the LLM via the UNKNOWN gate when the job drains.
      - Step 3 crash (DB error during enqueue) → calls embedded, no jobs
        queued. Caught and recorded as `error_msg`; the alerter fires via
        record_failure. The next ingest tick won't re-discover these calls
        (last_fetched_at was advanced in step 1); the 04:00 reconcile
        enqueues their stale pairs, as for the step-1 crash above.

    This matches the prior single-commit's behavior under mid-flight crash
    (which also left no jobs queued), so no atomicity regression.
    """
    run_started_at = datetime.now(UTC)

    with session_factory() as session:
        src = session.execute(
            select(Source).where(Source.slug == adapter.slug)
        ).scalar_one_or_none()
        if src is None:
            raise LookupError(f"unknown source slug: {adapter.slug!r}")
        source_id = src.id
        since = src.last_fetched_at or _EPOCH

    fetch = adapter.fetch_new_calls(since=since)

    inserted = updated = closed = 0
    calls_skipped_current = 0
    error_msg: str | None = None
    touched_call_ids: list[int] = []

    # --- Step 1: upsert calls + advance source timestamp (one transaction) ---
    try:
        # Materialize the generator BEFORE opening the write transaction. An
        # adapter makes blocking HTTP requests as it is iterated (EU portal
        # pages through its search API); doing that inside the tx below would
        # hold a write transaction open (idle-in-transaction, pinning a
        # connection + blocking autovacuum) across all that network I/O. Fetch
        # first — still inside this try so an AdapterError mid-fetch is caught —
        # then do the fast upsert loop in a short-lived transaction.
        fetched_calls = list(fetch)
        with session_factory() as session:
            for call in fetched_calls:
                if call.closed:
                    # UPDATE-ONLY, and only while our row still reads as open
                    # (deadline unset or in the future): a closed call is never
                    # a new row, and a row that is already closed has nothing
                    # left to learn. Not added to touched_call_ids, so no
                    # embedding and no ranking work: nobody can apply for it.
                    held = session.execute(
                        select(Call.id, Call.deadline).where(
                            Call.source_id == source_id,
                            Call.external_id == call.external_id,
                        )
                    ).one_or_none()
                    if (
                        call.deadline is None  # says closed but not when: nothing to apply
                        or held is None
                        or (held.deadline is not None and held.deadline <= run_started_at)
                    ):
                        continue
                    upsert_call(session, source_id=source_id, call=call)
                    # Counted from the row, not upsert_call's bool: False means
                    # "updated" and also "skipped" (an unsafe URL), and only the
                    # first one closed anything.
                    after = session.scalar(select(Call.deadline).where(Call.id == held.id))
                    if after is not None and after <= datetime.now(UTC):
                        closed += 1
                    continue
                was_new = upsert_call(session, source_id=source_id, call=call)
                # Look up the row's id for the embed/enqueue steps.
                # autoflush=True means the prior INSERT/UPDATE is visible here.
                # upsert_call returns False without writing a row when it SKIPS
                # the record (e.g. the is_safe_url chokepoint rejected the href).
                # For a brand-new skipped call there is no row, so scalar_one()
                # would raise NoResultFound and abort the ENTIRE batch
                # transaction. Use scalar_one_or_none() and skip the absent row.
                cid = session.execute(
                    select(Call.id).where(
                        Call.source_id == source_id,
                        Call.external_id == call.external_id,
                    )
                ).scalar_one_or_none()
                if cid is None:
                    # Skipped (unsafe URL); no row persisted. Don't count it as
                    # inserted/updated and don't enqueue downstream work.
                    continue
                if was_new:
                    inserted += 1
                else:
                    updated += 1
                touched_call_ids.append(cid)
            # Advance timestamp inside the same transaction as the upserts so
            # a failure rolls back atomically.
            session.execute(
                update(Source).where(Source.id == source_id).values(last_fetched_at=run_started_at)
            )
            session.commit()
    except AdapterError as e:
        # Expected operational failure (network blip, 5xx, malformed JSON).
        # WARN-level: noisy alerts here would train operators to ignore them.
        # The 3-strike alerter pings the admin if the streak persists.
        error_msg = str(e)
        _logger.warning(
            "ingestion.adapter_error",
            extra={
                "event": "ingestion_adapter_error",
                "source": adapter.slug,
                "error": error_msg,
            },
        )
    except Exception as e:  # noqa: BLE001 — orchestrator must not crash the scheduler
        # Unexpected failure (TypeError, KeyError, etc.) — almost always a
        # code bug. Log at ERROR with traceback so Sentry / log aggregator
        # surfaces it immediately, separate from the routine adapter-error
        # noise above. Still feed the streak counter so the alerter can
        # email if the bug persists past the threshold.
        error_msg = f"{type(e).__name__}: {e}"
        _logger.exception(
            "ingestion.unexpected_error",
            extra={
                "event": "ingestion_unexpected_error",
                "source": adapter.slug,
                "error": error_msg,
            },
        )

    # On Step 1 failure, skip embed + enqueue: we already rolled back, so there
    # are no touched calls to embed and nothing legitimate to enqueue.
    if error_msg is None:
        # --- Step 2: embed touched calls (best-effort, per-row transaction) ---
        # Codex H1: must complete BEFORE enqueue so the ranking worker never sees
        # a job whose call has NULL embedding (except the rare embed-fail
        # case, which falls through to LLM via the UNKNOWN gate).
        #
        # `touched_call_ids` is every upserted call, not every CHANGED one:
        # upsert_call UPDATEs on conflict whether or not a column moved, so on a
        # steady feed this loop visits the whole corpus every tick. It no longer
        # EMBEDS the whole corpus: refresh_call_embedding_with_outcome skips a
        # row whose vector, `embedding_model` and `embedding_fingerprint` are
        # already current, writing nothing. The skip deliberately lives there, at
        # the one site that stamps those columns, rather than being duplicated
        # here -- a second fingerprint comparison in this loop is a second thing
        # that must agree with the stamp, and the day they disagree the corpus
        # re-embeds forever or never. This loop only COUNTS the skips it is told
        # about, which is why it reads the outcome rather than the bool: the
        # bool wrapper collapses EMBEDDED and SKIPPED_CURRENT into True.
        for cid in touched_call_ids:
            try:
                with session_factory() as embed_session:
                    outcome = refresh_call_embedding_with_outcome(embed_session, embed_client, cid)
                    embed_session.commit()
                if outcome is CallEmbedOutcome.SKIPPED_CURRENT:
                    calls_skipped_current += 1
            except Exception as e:  # noqa: BLE001
                _logger.exception(
                    "ingestion.embedding_failed",
                    extra={
                        "event": "ingestion_embedding_failed",
                        "call_id": cid,
                        "error": str(e),
                    },
                )
                # Don't abort. Step 3 still judges this call: its pairs are
                # enqueued if stale (a new or changed call's always are), and a
                # missing vector falls through to the LLM via the UNKNOWN gate.

        # --- Step 3: enqueue the touched calls' STALE pairs (ranking/freshness.py;
        # one transaction, at most three statements) ---
        # enqueue_stale_pairs reads the active profiles and the touched OPEN
        # calls (two column-limited SELECTs), then issues ONE bulk INSERT …
        # SELECT over (call × profile) that skips every pair whose ranking is
        # current. Perf (D2-2) still holds: the statement count does not grow
        # with |calls| × |profiles| (50 calls × 3 profiles: 150 per-pair
        # INSERTs → 3 statements). ON CONFLICT DO NOTHING on the partial unique
        # index keeps it idempotent, as with the per-pair enqueue_for_pair.
        # Wrap in try/except so an enqueue-time DB error (lock contention,
        # transient connection drop) is recorded as `error_msg` rather than
        # propagating out and bypassing the alerter below. The next ingest tick
        # won't re-discover these calls (last_fetched_at already advanced in
        # step 1), but no manual rerank is needed: the 04:00 reconcile enqueues
        # every stale pair over all open calls, and a pair with no ranking is
        # stale.
        try:
            with session_factory() as session:
                bulk_enqueue_for_calls(session, touched_call_ids, ctx=freshness)
                session.commit()
        except Exception as e:  # noqa: BLE001 — must not crash the scheduler
            error_msg = f"step 3 (enqueue) failed: {e}"
            _logger.exception(
                "ingestion.enqueue_failed",
                extra={
                    "event": "ingestion_enqueue_failed",
                    "source": adapter.slug,
                    "error": str(e),
                },
            )
            # Fall through — error_msg is now set so the alerter check below fires.

    if error_msg is not None:
        alerter.record_failure(adapter.slug, error=error_msg)
    else:
        _logger.info(
            "ingestion.ok",
            extra={
                "event": "ingestion_ok",
                "source": adapter.slug,
                "inserted": inserted,
                "updated": updated,
                # Zero embeds with a non-zero count here is the skip working;
                # zero embeds with zero here means step 2 did nothing at all.
                "calls_skipped_current": calls_skipped_current,
                "closed": closed,
            },
        )
        alerter.record_success(adapter.slug)

    return IngestionResult(
        source_slug=adapter.slug,
        inserted=inserted,
        updated=updated,
        calls_skipped_current=calls_skipped_current,
        error=error_msg,
        closed=closed,
    )
