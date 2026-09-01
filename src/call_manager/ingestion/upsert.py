from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from call_manager.db.models import Call
from call_manager.sources.types import NormalizedCall, is_safe_url

_logger = logging.getLogger(__name__)


# Every value below arrives from upstream JSON. The adapters pass it through
# without a type check, so the DB column type is the ONLY thing standing between
# a changed upstream field and a `TypeError`/`DataError` — and that error escapes
# the upsert loop in `orchestrator.py`, which advances `sources.last_fetched_at`
# INSIDE the same transaction. The rollback leaves the timestamp unmoved, so the
# next tick re-fetches from the same `since`, meets the same record, and fails
# identically. Forever, discarding every other call in the batch each time.
#
# Measured before this existed: `currency=0` and `external_id=12345` raised
# `TypeError: object of type 'int' has no len()` inside the guards written to
# PREVENT poison records; `programme={"a": 1}` passed the length guard (a 1-key
# dict has len 1) and reached a VARCHAR(200); and budget/deadline/keywords/raw
# all reached the DB with a type their column cannot take.
#
# So types are settled here, once, before any guard or payload runs.


def _as_text(value) -> str | None:
    """A Text/VARCHAR column's value: None stays None, anything else becomes a
    string. Non-identity metadata is coerced rather than rejected so the call
    still ingests — the policy the currency/programme guards below already
    state."""
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _as_int(value) -> int | None:
    """A BigInteger column's value, or None when it cannot be one. A budget we
    cannot read is missing data, not a reason to lose the call."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_datetime(value) -> datetime | None:
    """A timestamptz column's value. Adapters are required to hand over real
    UTC datetimes (sources/types.py); anything else is dropped rather than
    parsed here, so a bad upstream format cannot become a wrong DEADLINE."""
    return value if isinstance(value, datetime) else None


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
    # Identity columns first, and by REJECTION not coercion: `external_id` is the
    # dedup key and `url` is what the safety check reads, so a coerced value
    # would change WHICH call this is or what was validated. Both are NOT NULL.
    if not isinstance(call.external_id, str) or not isinstance(call.url, str):
        _logger.warning(
            "ingestion.upsert.skip_untyped_identity",
            extra={
                "event": "skip_untyped_identity",
                "source_id": source_id,
                "external_id_type": type(call.external_id).__name__,
                "url_type": type(call.url).__name__,
            },
        )
        return False
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
    currency = _as_text(call.currency)
    if currency is not None and len(currency) > 8:
        _logger.warning(
            "ingestion.upsert.dropped_oversize_currency",
            extra={"event": "dropped_oversize_currency", "external_id": call.external_id},
        )
        currency = None
    programme = _as_text(call.programme)
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
        # `title` is NOT NULL: coerce to "" rather than reject, so a bad title
        # costs a label and not the call.
        "title": _as_text(call.title) or "",
        "summary": _as_text(call.summary),
        "full_text": _as_text(call.full_text),
        "deadline": _as_datetime(call.deadline),
        # Dropped by 8a297db1's typed-wrapper rewrite: backfill/queue filter on
        # it and the dashboard sorts by it, so its absence = never ranked.
        "published_at": _as_datetime(call.published_at),
        "budget_min": _as_int(call.budget_min),
        "budget_max": _as_int(call.budget_max),
        "currency": currency,
        "programme": programme,
        # ARRAY(Text) and JSONB: the element type matters as much as the outer
        # one, and a bare string is iterable — `list("ab")` would silently
        # become ["a", "b"], so the isinstance check is not redundant.
        "keywords": (
            [_as_text(k) or "" for k in call.keywords]
            if isinstance(call.keywords, (list, tuple))
            else []
        ),
        "raw": call.raw if isinstance(call.raw, dict) else {},
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
