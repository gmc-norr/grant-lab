"""ranking_remote_attempts: error_class, tokens_in, tokens_out

Revision ID: 0085
Revises: 0084

The remote-escalation ledger counted attempts by outcome only, so a failure
could not say why (refusal or max_tokens truncation vs a bad parse vs an
outage) and its billed tokens were lost (review 2026-10-06 M1). All three
columns are nullable: NULL on every earlier row, and tokens NULL when no reply
came back.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ranking_remote_attempts", sa.Column("error_class", sa.Text(), nullable=True))
    op.add_column("ranking_remote_attempts", sa.Column("tokens_in", sa.Integer(), nullable=True))
    op.add_column("ranking_remote_attempts", sa.Column("tokens_out", sa.Integer(), nullable=True))


def downgrade() -> None:
    for col in ("tokens_out", "tokens_in", "error_class"):
        op.drop_column("ranking_remote_attempts", col)
