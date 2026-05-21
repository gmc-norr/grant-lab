"""b7_attachment_dimensions

Revision ID: 0030
Revises: 0029
Create Date: 2026-05-21

Adds nullable ``width_px``, ``height_px``, ``tokens_estimate`` columns to
the ``attachments`` table. Populated at upload time from a Pillow probe
(see ``proposal_assist/web/files.py``); ``submit_user_turn`` re-probes
from disk and persists if NULL is encountered, so NULL is NEVER treated
as zero in the daily-token cap sum.

NULLability is intentional and bounded:
  1. Rows inserted by the T2 panel before the T3 upload-handler backfill
     have NULL dims until probed on submit.
  2. Pre-B7 rows from any B5-demo data (none in prod).

Manually authored (no autogenerate) — autogen has a history of misbehaving
on this codebase.
"""

import sqlalchemy as sa

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "attachments",
        sa.Column("width_px", sa.Integer(), nullable=True),
    )
    op.add_column(
        "attachments",
        sa.Column("height_px", sa.Integer(), nullable=True),
    )
    op.add_column(
        "attachments",
        sa.Column("tokens_estimate", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("attachments", "tokens_estimate")
    op.drop_column("attachments", "height_px")
    op.drop_column("attachments", "width_px")
