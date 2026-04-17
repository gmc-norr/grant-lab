from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from call_monitor.db.models import Source
from call_monitor.ingestion.alerts import ConsecutiveFailureTracker
from call_monitor.ingestion.upsert import upsert_call
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
        with session_factory() as session:
            for call in adapter.fetch_new_calls(since=since):
                was_new = upsert_call(session, source_id=source_id, call=call)
                if was_new:
                    inserted += 1
                else:
                    updated += 1
            # Advance timestamp inside the same transaction.
            session.execute(
                Source.__table__.update()
                .where(Source.id == source_id)
                .values(last_fetched_at=run_started_at)
            )
            session.commit()
    except AdapterError as e:
        error_msg = str(e)
    except Exception as e:  # noqa: BLE001 — orchestrator must not crash the scheduler
        error_msg = f"{type(e).__name__}: {e}"

    if error_msg is not None:
        _logger.warning(
            "ingestion.failed",
            extra={"event": "ingestion_failed", "source": adapter.slug, "error": error_msg},
        )
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
