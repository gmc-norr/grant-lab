"""pgvector + embedding columns

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-18

Adds the vector extension and embedding storage:
- calls.embedding vector(1024) — BGE-M3 of title+summary+full_text[:8000]
- calls.embedding_model text — model identifier for cache invalidation
- profiles.embedding vector(1024) — BGE-M3 of description
- profiles.embedding_model text
- call_rankings.embed_similarity float — cosine; persisted on every rank
- partial NULL-only indexes for the daily backfill tick

Per spec §4. No data backfill here — historical embeddings are populated
by `python -m grant_lab.embeddings.cli backfill` (Task 9), which is
part of the deploy procedure between `alembic upgrade head` and app start.
"""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op

# revision identifiers, used by Alembic.
revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.add_column("calls", sa.Column("embedding", Vector(1024), nullable=True))
    op.add_column("calls", sa.Column("embedding_model", sa.Text(), nullable=True))

    op.add_column("profiles", sa.Column("embedding", Vector(1024), nullable=True))
    op.add_column("profiles", sa.Column("embedding_model", sa.Text(), nullable=True))

    op.add_column("call_rankings", sa.Column("embed_similarity", sa.Float(), nullable=True))

    # Partial indexes for the daily refresh-missing tick. Both NULL filters
    # are heavily selective until backfill completes, then become very small
    # — partial index keeps them cheap.
    op.create_index(
        "ix_calls_embedding_null",
        "calls",
        ["id"],
        postgresql_where=sa.text("embedding IS NULL"),
    )
    op.create_index(
        "ix_profiles_embedding_null",
        "profiles",
        ["id"],
        postgresql_where=sa.text("embedding IS NULL"),
    )

    # Embedding + model are set together. Either both NULL (not yet embedded)
    # or both non-NULL (embedded with a known model). Prevents orphan vectors
    # whose model is unknown.
    op.create_check_constraint(
        "ck_calls_embedding_model_paired",
        "calls",
        "(embedding IS NULL) = (embedding_model IS NULL)",
    )
    op.create_check_constraint(
        "ck_profiles_embedding_model_paired",
        "profiles",
        "(embedding IS NULL) = (embedding_model IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_profiles_embedding_model_paired", "profiles", type_="check")
    op.drop_constraint("ck_calls_embedding_model_paired", "calls", type_="check")
    op.drop_index("ix_profiles_embedding_null", table_name="profiles")
    op.drop_index("ix_calls_embedding_null", table_name="calls")
    op.drop_column("call_rankings", "embed_similarity")
    op.drop_column("profiles", "embedding_model")
    op.drop_column("profiles", "embedding")
    op.drop_column("calls", "embedding_model")
    op.drop_column("calls", "embedding")
    # Don't drop the extension — could be used by other future migrations.
