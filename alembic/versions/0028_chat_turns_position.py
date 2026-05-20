"""chat_turns: add position column for monotonic ordering

Revision ID: 0028
Revises: 0027
Create Date: 2026-05-20
"""

import sqlalchemy as sa

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create a global sequence + add position column defaulted from it.
    # Strict monotonicity within a workspace's history requires a
    # row-insert-counter, and Postgres SEQUENCE is the simplest such
    # counter that's tie-free even within a single INSERT statement.
    op.execute("CREATE SEQUENCE chat_turns_position_seq")
    op.add_column(
        "chat_turns",
        sa.Column(
            "position",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("nextval('chat_turns_position_seq')"),
        ),
    )
    op.create_index(
        "ix_chat_turns_ws_position",
        "chat_turns",
        ["workspace_id", "position"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_turns_ws_position", table_name="chat_turns")
    op.drop_column("chat_turns", "position")
    op.execute("DROP SEQUENCE chat_turns_position_seq")
