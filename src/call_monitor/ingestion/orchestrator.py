from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from call_monitor.db.models import Call, Profile, Source
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
) -> IngestionResult:
    """Run one adapter end-to-end.

    Contract:
    - Reads `sources.last_fetched_at` to pass to `adapter.fetch_new_calls()`.
    - Per call: `upsert_call`. Counts inserts vs. updates.
    - On success: advances `sources.last_fetched_at` to run-start time, commits.
    - On failure: rolls back the whole run; does NOT advance the timestamp.
    - Notifies the alerter either way (success resets streak; failure increments).
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

    try:
        # Track Call ids touched this run (both new and updated). We enqueue
        # ranking jobs for ALL of them, not just inserts — an updated call
        # might have a meaningful field change (deadline shifted, summary
        # rewritten) that warrants re-ranking. The job queue's partial
        # unique index dedup-guards if a job is already pending.
        touched_call_ids: list[int] = []
        with session_factory() as session:
            for call in adapter.fetch_new_calls(since=since):
                was_new = upsert_call(session, source_id=source_id, call=call)
                if was_new:
                    inserted += 1
                else:
                    updated += 1
                # Look up the row's id for the enqueue step. autoflush=True
                # means the prior INSERT/UPDATE is visible here.
                cid = session.execute(
                    select(Call.id).where(
                        Call.source_id == source_id,
                        Call.external_id == call.external_id,
                    )
                ).scalar_one()
                touched_call_ids.append(cid)
            # Advance timestamp inside the same transaction.
            session.execute(
                Source.__table__.update()
                .where(Source.id == source_id)
                .values(last_fetched_at=run_started_at)
            )
            # Enqueue ranking jobs for every touched call × active profile.
            # This happens INSIDE the same transaction as the upsert + timestamp
            # advance so a failure here rolls back everything atomically.
            active_profile_ids = session.execute(
                select(Profile.id).where(Profile.is_active.is_(True))
            ).scalars().all()
            for cid in touched_call_ids:
                for pid in active_profile_ids:
                    enqueue_for_pair(session, call_id=cid, profile_id=pid)
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
