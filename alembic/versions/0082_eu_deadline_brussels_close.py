"""eu_portal deadlines: a bare SEDIA date is the 17:00 Brussels close

Revision ID: 0082
Revises: 0081

SEDIA encodes a deadline DATE as midnight UTC, and until this revision the
adapter stored it unchanged, so every EU call closed in the feed at 02:00
Stockholm on its last day (review 2026-10-06 H3: 670 of 676 live eu_portal
deadlines at exactly 00:00 UTC). The adapter now bumps the date to 17:00
Europe/Brussels; this moves the rows already stored.

The deadline is part of `call_rank_fingerprint`, so moving it would make every
open EU pair look changed and re-rank the whole EU feed — Gemma GPU time plus
Sonnet escalations against the daily cap, for a re-encoding of the SAME
intended deadline. So each ranking (and failed-job stamp) whose call
fingerprint was current for the old row is re-stamped with the fingerprint of
the new row, in the same transaction. A stamp that was already stale stays
stale and re-ranks as it would have anyway.

The time arithmetic is inlined rather than imported from the adapter so this
revision keeps meaning what it meant today; tests/test_migration_0082_* pins
that it agrees with `eu_portal.brussels_close_if_midnight`. The fingerprint IS
imported: it has to be byte-identical to what the app compares.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from alembic import op

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None

_BRUSSELS = ZoneInfo("Europe/Brussels")
_CLOSE = time(17, 0)

# Every column call_rank_fingerprint reads today, by name.
_SELECT = sa.text(
    """
    SELECT c.id, c.url, c.title, c.summary, c.full_text, c.deadline, c.budget_min,
           c.budget_max, c.currency, c.programme, c.keywords, c.published_at, c.raw,
           c.embedding_fingerprint, c.embedding_model
    FROM calls c JOIN sources s ON s.id = c.source_id
    WHERE s.slug = 'eu_portal' AND c.deadline IS NOT NULL
    """
)


class _Row(dict):
    """Attribute access for call_rank_fingerprint. A column it reads that this
    revision does not select hashes as None, which can only make the computed
    'old' fingerprint miss the stored one -- and a miss means no re-stamp, i.e.
    the safe outcome (one re-rank), never a wrong stamp."""

    def __getattr__(self, name):
        return self.get(name)


def _to_close(value: datetime) -> datetime | None:
    utc = value.astimezone(UTC)
    if utc.time() != time.min:
        return None
    return datetime.combine(utc.date(), _CLOSE, tzinfo=_BRUSSELS).astimezone(UTC)


def _to_midnight(value: datetime) -> datetime | None:
    local = value.astimezone(_BRUSSELS)
    if local.time() != _CLOSE:
        return None
    return datetime.combine(value.astimezone(UTC).date(), time.min, tzinfo=UTC)


def _move(transform: Callable[[datetime], datetime | None]) -> None:
    from grant_lab.profiles.fingerprint import call_rank_fingerprint

    conn = op.get_bind()
    for mapping in conn.execute(_SELECT).mappings().all():
        row = _Row(mapping)
        moved = transform(row["deadline"])
        if moved is None:
            continue
        old_fp = call_rank_fingerprint(row)
        new_fp = call_rank_fingerprint(_Row(row, deadline=moved))
        conn.execute(
            sa.text("UPDATE calls SET deadline = :d WHERE id = :id"),
            {"d": moved, "id": row["id"]},
        )
        for table in ("call_rankings", "ranking_jobs"):
            conn.execute(
                sa.text(
                    f"UPDATE {table} SET call_fingerprint = :new "
                    "WHERE call_id = :id AND call_fingerprint = :old"
                ),
                {"new": new_fp, "old": old_fp, "id": row["id"]},
            )


def upgrade() -> None:
    _move(_to_close)


def downgrade() -> None:
    # Lossy at one edge, documented: an eu_portal deadline that ARRIVED at
    # exactly 17:00 Brussels (none on 2026-10-06; the two non-midnight live rows
    # sit at 17:00 and 22:00 UTC) is indistinguishable from a moved one and is
    # moved back to midnight too. The 0081 adapter writes midnight anyway.
    _move(_to_midnight)
