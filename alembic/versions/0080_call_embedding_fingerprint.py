"""call embedding fingerprint: record what a call's stored vector describes

Revision ID: 0080
Revises: 0079
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "calls",
        sa.Column("embedding_fingerprint", sa.Text(), nullable=True),
    )
    # Deliberately NOT grandfathered, mirroring migration 0075's
    # profiles.embedding_fingerprint: NULL means stale, and every call is
    # re-embedded on the first tick after deploy. This was 58 calls when the
    # spec was written (2026-09-08); 686 on 2026-09-24, when this migration
    # was authored -- either way, seconds against a local Ollama, not the
    # profile-count x 90-days-of-calls cost a full re-RANK would be. See
    # docs/superpowers/specs/2026-09-08-call-embedding-freshness.md, D4.
    #
    # 0075's UPDATE ... SET profile_fingerprint = 'legacy' on call_rankings
    # has no counterpart here: that grandfathered call_rankings.
    # profile_fingerprint, a different column on a different table, because
    # re-ranking the whole corpus is expensive. This column is not that one.


def downgrade() -> None:
    op.drop_column("calls", "embedding_fingerprint")
