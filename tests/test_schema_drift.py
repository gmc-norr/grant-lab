"""Review 2026-09-02 §3 data layer: the ORM and the migrated database must agree.

Measured at the base commit: `compare_metadata` against a migrated pgvector
container reported exactly three ops, all `remove_index` — the HNSW vector index
(raw SQL in 0031), `ix_reference_chunks_ws_model` (0032) and the partial
`ix_references_indexing_backlog` (0031) existed in the database and nowhere in
`models.py`. The next `alembic revision --autogenerate` would have DROPPED the
vector index. Nothing in `tests/` ran `compare_metadata`. This does, and it is the
gate: any future column, index or constraint that lands in one place and not the
other fails here with the diff spelled out.
"""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from call_manager.db.models import Base


def _describe(op) -> str:
    kind = op[0] if isinstance(op, tuple) else type(op).__name__
    target = op[1] if isinstance(op, tuple) and len(op) > 1 else ""
    name = getattr(target, "name", None) or getattr(target, "key", None) or target
    return f"{kind}: {name}"


def test_orm_metadata_matches_the_migrated_database(migrated_engine):
    with migrated_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diffs = compare_metadata(ctx, Base.metadata)
    flat = [d for group in diffs for d in (group if isinstance(group, list) else [group])]
    assert flat == [], "ORM and migrations disagree:\n  " + "\n  ".join(_describe(d) for d in flat)
