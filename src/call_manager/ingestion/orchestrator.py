from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from call_manager.db.models import Call, Profile, Source
from call_manager.embeddings.client import OllamaEmbedClient
from call_manager.embeddings.refresh import refresh_call_embedding
from call_manager.ingestion.alerts import ConsecutiveFailureTracker
from call_manager.ingestion.upsert import upsert_call
from call_manager.ranking.queue import enqueue_for_pair
from call_manager.sources.types import AdapterError, SourceAdapter

_logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _accepts_needs_body(fetch: Callable) -> bool:
    """True if `fetch_new_calls` accepts a `needs_body` keyword argument.

    Tolerates older `(self, since)` adapters / test doubles. A signature with
    **kwargs is treated as accepting it too.
    """
    try:
        params = inspect.signature(fetch).parameters
    except (TypeError, ValueError):
        return False
    if "needs_body" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


@dataclass
class IngestionResult:
    source_slug: str
    inserted: int
    updated: int
    error: str | None  # None on success, message on failure


def run_ingestion(
    *,
    session_factory: sessionmaker,
    adapter: SourceAdapter,
    alerter: ConsecutiveFailureTracker,
    embed_client: OllamaEmbedClient,
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

      Step 3 (one transaction): enqueue ranking jobs for touched calls
        × active profiles.

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
        is a no-op on no-change anyway). The daily refresh-missing tick
        eventually catches the missing embeddings; an admin must manually
        rerank to queue ranking jobs.
      - Step 2 partial failure (one embed call raises) → loop continues to
        next call; step 3 still runs; failed-embed calls go through LLM
        via the UNKNOWN gate when their job drains.
      - Step 3 crash (DB error during enqueue) → calls embedded, no jobs
        queued. Caught and recorded as `error_msg`; the alerter fires via
        record_failure. Admin must manually rerank to recover (the next
        ingest tick won't re-discover these calls because last_fetched_at
        was advanced in step 1).

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

    # Build a cheap `needs_body(external_id) -> bool` predicate from a single
    # indexed query of which calls for this source already have a non-null
    # `full_text`. Adapters that scrape a separate detail page for the body
    # (e.g. EU portal) consult this to SKIP re-scraping already-ingested calls
    # — turning hundreds of serial blocking HTTP+parse calls per run into a
    # scrape of only brand-new / body-less calls. Adapters whose source carries
    # the body inline (Vinnova) ignore the predicate.
    with session_factory() as session:
        have_body_ids = set(
            session.execute(
                select(Call.external_id).where(
                    Call.source_id == source_id,
                    Call.full_text.is_not(None),
                )
            )
            .scalars()
            .all()
        )

    def _needs_body(external_id: str) -> bool:
        return external_id not in have_body_ids

    # Only pass `needs_body` to adapters whose `fetch_new_calls` accepts it.
    # The SourceAdapter protocol declares the kwarg, but minimal/legacy
    # adapters (and some test doubles) may still use the older `(self, since)`
    # signature; calling those with an unexpected kwarg would raise TypeError.
    # Inspect once and degrade gracefully — a body-inline adapter that ignores
    # the predicate loses nothing.
    if _accepts_needs_body(adapter.fetch_new_calls):
        fetch = adapter.fetch_new_calls(since=since, needs_body=_needs_body)
    else:
        fetch = adapter.fetch_new_calls(since=since)

    inserted = updated = 0
    error_msg: str | None = None
    touched_call_ids: list[int] = []

    # --- Step 1: upsert calls + advance source timestamp (one transaction) ---
    try:
        # Materialize the (body-scraping) generator BEFORE opening the write
        # transaction. EU-portal's fetch_new_calls scrapes each call body with a
        # blocking HTTP request as it is iterated; doing that inside the tx below
        # would hold a write transaction open (idle-in-transaction, pinning a
        # connection + blocking autovacuum) across all that network I/O. Scrape
        # first — still inside this try so an AdapterError mid-scrape is caught —
        # then do the fast upsert loop in a short-lived transaction. (Bounded by
        # the source's new-call count per run; bodies were already in memory.)
        fetched_calls = list(fetch)
        with session_factory() as session:
            for call in fetched_calls:
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
                Source.__table__.update()
                .where(Source.id == source_id)
                .values(last_fetched_at=run_started_at)
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
        for cid in touched_call_ids:
            try:
                with session_factory() as embed_session:
                    refresh_call_embedding(embed_session, embed_client, cid)
                    embed_session.commit()
            except Exception as e:  # noqa: BLE001
                _logger.exception(
                    "ingestion.embedding_failed",
                    extra={
                        "event": "ingestion_embedding_failed",
                        "call_id": cid,
                        "error": str(e),
                    },
                )
                # Don't abort — enqueue the job anyway. UNKNOWN gate → LLM runs.

        # --- Step 3: enqueue ranking jobs (one transaction) ---
        # Wrap in try/except so an enqueue-time DB error (lock contention,
        # transient connection drop) is recorded as `error_msg` rather than
        # propagating out and bypassing the alerter below. Recovery on this
        # branch requires a manual rerank — last_fetched_at already advanced
        # in step 1, so the next ingest tick won't re-discover these calls.
        try:
            with session_factory() as session:
                active_profile_ids = (
                    session.execute(select(Profile.id).where(Profile.is_active.is_(True)))
                    .scalars()
                    .all()
                )
                for cid in touched_call_ids:
                    for pid in active_profile_ids:
                        enqueue_for_pair(
                            session,
                            call_id=cid,
                            profile_id=pid,
                        )
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
            },
        )
        alerter.record_success(adapter.slug)

    return IngestionResult(
        source_slug=adapter.slug,
        inserted=inserted,
        updated=updated,
        error=error_msg,
    )
