from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from call_manager.db.models import Call
from call_manager.sources.types import NormalizedCall, is_safe_url

_logger = logging.getLogger(__name__)


def upsert_call(session: Session, *, source_id: int, call: NormalizedCall) -> bool:
    """Insert or update one Call row keyed on (source_id, external_id).

    Returns True if a row was newly inserted, False if an existing row was
    updated. Caller is responsible for the surrounding transaction.

    Pre-check: was the row there already? Cheap one-row indexed lookup, one
    extra round-trip per call. At ~50 calls/week this is negligible. If
    profiling later says otherwise, switch to RETURNING (xmax = 0)::bool via
    raw SQL — same atomic guarantee, no read.

    Race-condition note: under concurrent ingestion of the same brand-new
    call, two workers could both observe "not present" in the pre-check;
    one would INSERT, the other would UPDATE via ON CONFLICT, but BOTH
    would return True. The DATA is correct (one row), only the inserted/
    updated accounting is wrong. v1 single-instance deploy makes this
    impossible. **Plan 3 must NOT use this boolean to detect "this call
    is new, enqueue ranking work" — query for calls without a ranking
    row instead.** That keeps ranking idempotent regardless of any
    future relaxation of the single-instance constraint.
    """
    # Central URL-safety chokepoint: even if a future adapter forgets to call
    # is_safe_url, a dangerous href (javascript:/data:/etc.) must never reach
    # the DB — it would render as click-to-XSS in the dashboard. `url` is
    # NOT NULL, so we reject the whole record rather than persist a bad href.
    if not is_safe_url(call.url):
        _logger.warning(
            "ingestion.upsert.skip_unsafe_url",
            extra={
                "event": "skip_unsafe_url",
                "source_id": source_id,
                "external_id": call.external_id,
            },
        )
        return False

    # Bounded-column guards. The DB columns external_id/currency/programme are
    # VARCHAR(200/8/200); adapters pass upstream values through str() unchecked,
    # so a single oversized identifier would raise DataError and roll back the
    # WHOLE batch transaction (and re-fail it every tick — a poison record).
    # external_id is the dedup IDENTITY (NOT NULL): we cannot truncate it
    # without risking a collision/identity change, so an over-length one means
    # the record is rejected outright (like an unsafe URL). currency/programme
    # are nullable, non-identity metadata: a junk-length currency is coerced to
    # NULL and an over-length programme is truncated, so the call still ingests.
    if len(call.external_id) > 200:
        _logger.warning(
            "ingestion.upsert.skip_oversize_external_id",
            extra={
                "event": "skip_oversize_external_id",
                "source_id": source_id,
                "external_id_len": len(call.external_id),
            },
        )
        return False
    currency = call.currency
    if currency is not None and len(currency) > 8:
        _logger.warning(
            "ingestion.upsert.dropped_oversize_currency",
            extra={"event": "dropped_oversize_currency", "external_id": call.external_id},
        )
        currency = None
    programme = call.programme
    if programme is not None and len(programme) > 200:
        _logger.warning(
            "ingestion.upsert.truncated_programme",
            extra={"event": "truncated_programme", "external_id": call.external_id},
        )
        programme = programme[:200]

    payload = {
        "source_id": source_id,
        "external_id": call.external_id,
        "url": call.url,
        "title": call.title,
        "summary": call.summary,
        "full_text": call.full_text,
        "deadline": call.deadline,
        "budget_min": call.budget_min,
        "budget_max": call.budget_max,
        "currency": currency,
        "programme": programme,
        "keywords": call.keywords,
        "published_at": call.published_at,
        "raw": call.raw,
    }

    existed = (
        session.execute(
            select(Call.id).where(Call.source_id == source_id, Call.external_id == call.external_id)
        ).first()
        is not None
    )

    stmt = insert(Call).values(**payload, fetched_at=func.now())
    update_cols = {c: stmt.excluded[c] for c in payload if c not in {"source_id", "external_id"}}
    # Preserve existing full_text on transient scrape failures (None in new payload).
    # Same intent as worker.py's embed_similarity preservation: don't clobber good
    # diagnostic/content data with NULL just because one fetch went bad.
    update_cols["full_text"] = func.coalesce(stmt.excluded.full_text, Call.full_text)
    update_cols["fetched_at"] = func.now()  # refresh "last seen" on every encounter
    stmt = stmt.on_conflict_do_update(
        constraint="uq_calls_source_external",
        set_=update_cols,
    )
    session.execute(stmt)
    return not existed
