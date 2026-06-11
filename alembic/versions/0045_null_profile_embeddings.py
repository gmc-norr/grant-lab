"""Data migration: NULL all profile embeddings so they re-embed under the new
build_profile_text (org capabilities + patient_populations). Call vectors are
untouched. Downgrade is a no-op — a dropped vector can't be reconstructed; the
daily refresh tick recomputes within 24h.

Revision ID: 0045
Revises: 0044
Create Date: 2026-06-11

See docs/superpowers/plans/2026-06-11-ranking-v2.1-improvements.md Task 31.
"""

from __future__ import annotations

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The paired-NULL CHECK constraint (ck_profiles_embedding_model_paired)
    # requires both columns move together — this sets both NULL in one UPDATE.
    op.execute("UPDATE profiles SET embedding = NULL, embedding_model = NULL")


def downgrade() -> None:
    # No-op: vectors are recomputed by the daily refresh-missing tick.
    pass
