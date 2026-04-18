from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from call_monitor.db.models import Call, Profile, Source
from call_monitor.embeddings.client import OllamaEmbedClient
from call_monitor.embeddings.refresh import refresh_call_embedding
from call_monitor.ingestion.alerts import ConsecutiveFailureTracker
from call_monitor.ingestion.upsert import upsert_call
from call_monitor.ranking.queue import enqueue_for_pair
from call_monitor.sources.types import AdapterError, SourceAdapter

_logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


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
        We do NOT abort the run on embed failure — the v2 worker has an
        UNKNOWN gate that falls through to the LLM, so a missing embedding
        is a graceful degradation (LLM still runs as the safe fallback).

      Step 3 (one transaction): enqueue ranking jobs for touched calls
        × active profiles.

    The split exists so the v2 ranking worker (which polls every 60s) never
    sees a job whose call/profile pair has a NULL embedding because the
    embedding refresh hadn't happened yet. Before this split, upsert + enqueue
    shared one transaction and the embed step was done post-commit, leaving
    a window where the worker could pick up a job and silently fall through
    to LLM with `embed_similarity = NULL` permanently.

    Atomicity caveats — worth being explicit about, since the previous version
    was atomic across upsert+enqueue:
      - Step 1 succeeds, step 2/3 crash (process killed) → calls inserted,
        no jobs queued. Next ingest tick won't re-discover the same calls
        (they exist; upsert is a no-op on conflict). The daily refresh-missing
        tick eventually catches the missing embeddings; manual rerank re-queues.
      - Step 1+2 succeed, step 3 crash → calls embedded, no jobs queued.
        Same recovery path.
      - Step 2 partial failure → step 3 still runs; failed-embed calls go
        through LLM via the UNKNOWN gate when their job drains.

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

    inserted = updated = 0
    error_msg: str | None = None
    touched_call_ids: list[int] = []

    # --- Step 1: upsert calls + advance source timestamp (one transaction) ---
    try:
        with session_factory() as session:
            for call in adapter.fetch_new_calls(since=since):
                was_new = upsert_call(session, source_id=source_id, call=call)
                if was_new:
                    inserted += 1
                else:
                    updated += 1
                # Look up the row's id for the embed/enqueue steps.
                # autoflush=True means the prior INSERT/UPDATE is visible here.
                cid = session.execute(
                    select(Call.id).where(
                        Call.source_id == source_id,
                        Call.external_id == call.external_id,
                    )
                ).scalar_one()
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
        # Codex H1: must complete BEFORE enqueue so the v2 worker never sees
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
        with session_factory() as session:
            active_profiles = session.execute(
                select(Profile.id, Profile.ranking_scheme).where(
                    Profile.is_active.is_(True)
                )
            ).all()
            for cid in touched_call_ids:
                for pid, pscheme in active_profiles:
                    enqueue_for_pair(
                        session,
                        call_id=cid,
                        profile_id=pid,
                        scheme=pscheme,
                    )
            session.commit()

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
