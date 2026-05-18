"""Null existing profile embeddings so the daily tick rebuilds them.

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-24

Follow-up to a code change (not a schema change): Codex #2 extends
`build_profile_text` to include `structured.areas` alongside
`description`. Every stored profile.embedding was computed from
description-only text and is now stale relative to the new text.

Fix: NULL out `profiles.embedding` and `profiles.embedding_model` so
the daily `embeddings:refresh-missing` tick picks each row up through
its `embedding IS NULL OR embedding_model != current` filter. During
the window between this migration and the tick, `rank_pair`'s
`_safe_cosine` returns None → embedding gate UNKNOWN → LLM runs as the
safe fallback. No ranking-correctness regression, just a one-day
lag before the pre-gate is active again.

Operators who can't wait for the nightly tick can run the backfill
CLI for immediate refresh:

    uv run python -m call_monitor.embeddings.cli backfill

No-op column changes; this migration is pure DML. No downgrade path
for the DML (the old vectors are derivable; nothing's lost).
"""
from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL both columns together — leaving embedding_model set would make
    # the `embedding IS NULL OR embedding_model != current` filter in
    # refresh_missing_embeddings_tick skip the row (NULL embedding + same
    # model would still satisfy the OR, but keeping the columns in lockstep
    # matches the force-mode semantics in refresh.py).
    op.execute(
        "UPDATE profiles "
        "SET embedding = NULL, embedding_model = NULL "
        "WHERE embedding IS NOT NULL"
    )


def downgrade() -> None:
    # Nothing to undo — the vectors are derived from description + areas,
    # rerun the backfill CLI to get them back.
    pass
