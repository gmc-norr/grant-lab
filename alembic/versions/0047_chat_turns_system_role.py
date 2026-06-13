"""Add 'system' to chat_turns.role check constraint (turn-outcome rows).

Revision ID: 0047
Revises: 0046
Create Date: 2026-06-13
"""

from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_chat_turns_role", "chat_turns", type_="check")
    op.create_check_constraint(
        "ck_chat_turns_role",
        "chat_turns",
        "role IN ('user','assistant','tool','system')",
    )


def downgrade() -> None:
    # Forward-only in spirit: a downgrade fails if any 'system' rows exist.
    op.execute("ALTER TABLE chat_turns DROP CONSTRAINT IF EXISTS ck_chat_turns_role")
    op.create_check_constraint(
        "ck_chat_turns_role",
        "chat_turns",
        "role IN ('user','assistant','tool')",
    )
