from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from call_monitor.db.models import Call
from call_monitor.sources.types import NormalizedCall


def upsert_call(
    session: Session, *, source_id: int, call: NormalizedCall
) -> bool:
    """Insert or update one Call row keyed on (source_id, external_id).

    Returns True if a row was newly inserted, False if an existing row was
    updated. Caller is responsible for the surrounding transaction.

    Pre-check: was the row there already? Cheap one-row indexed lookup, one
    extra round-trip per call. At ~50 calls/week this is negligible. If
    profiling later says otherwise, switch to RETURNING (xmax = 0)::bool via
    raw SQL — same atomic guarantee, no read.
    """
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
        "currency": call.currency,
        "programme": call.programme,
        "keywords": call.keywords,
        "published_at": call.published_at,
        "raw": call.raw,
    }

    existed = session.execute(
        select(Call.id).where(
            Call.source_id == source_id, Call.external_id == call.external_id
        )
    ).first() is not None

    stmt = insert(Call).values(**payload, fetched_at=func.now())
    update_cols = {
        c: stmt.excluded[c] for c in payload if c not in {"source_id", "external_id"}
    }
    update_cols["fetched_at"] = func.now()  # refresh "last seen" on every encounter
    stmt = stmt.on_conflict_do_update(
        constraint="uq_calls_source_external",
        set_=update_cols,
    )
    session.execute(stmt)
    return not existed
