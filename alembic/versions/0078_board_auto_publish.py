"""board auto-publish: the flag, suppressions, and an optional approver

DOWNGRADE HAZARD: see `downgrade()`. Dropping `board_suppressions` destroys
every takedown decision, and the next 05:00 run re-publishes every card an
operator had taken off a wall. This caveat lives in the migration rather than
in the plan because this repo prunes executed plans (CLAUDE.md) -- 0077
carries its own hazard the same way.

Revision ID: 0078
Revises: 0077
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "boards",
        sa.Column(
            "auto_publish",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # `approved_by` becomes optional. NULL means "published automatically" and
    # is the only thing that distinguishes an auto card from a human-approved
    # one for the life of the row -- so this is not merely relaxing a
    # constraint, it is adding a value with meaning.
    op.alter_column("board_items", "approved_by", existing_type=sa.BigInteger(), nullable=True)
    op.create_table(
        "board_suppressions",
        sa.Column("board_id", sa.BigInteger(), nullable=False),
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column("suppressed_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "suppressed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["board_id"], ["boards.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["suppressed_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("board_id", "call_id"),
    )


def downgrade() -> None:
    # DOWNGRADE HAZARD, stated here because this repo prunes executed plans and
    # a migration must carry its own caveats: dropping board_suppressions
    # DESTROYS every takedown decision. Re-upgrading gives an empty table, and
    # the next 05:00 run re-publishes every call an operator had taken off a
    # wall. Export the table before downgrading in an environment that has
    # been live.
    op.drop_table("board_suppressions")
    # An auto board's rows have approved_by = NULL and cannot satisfy NOT NULL.
    # Delete them: they are derived data the next run rebuilds, whereas
    # inventing an approver would write a false audit record.
    op.execute("DELETE FROM board_items WHERE approved_by IS NULL")
    op.alter_column("board_items", "approved_by", existing_type=sa.BigInteger(), nullable=False)
    op.drop_column("boards", "auto_publish")
