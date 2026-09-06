"""boards + board_items

Revision ID: 0072
Revises: 0071
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "boards",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("profile_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "is_published",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "min_score", sa.Integer(), server_default=sa.text("60"), nullable=False
        ),
        sa.Column(
            "orientation",
            sa.String(length=16),
            server_default=sa.text("'landscape'"),
            nullable=False,
        ),
        sa.Column(
            "items_per_page", sa.Integer(), server_default=sa.text("8"), nullable=False
        ),
        sa.Column(
            "max_pages", sa.Integer(), server_default=sa.text("3"), nullable=False
        ),
        sa.Column(
            "page_seconds", sa.Integer(), server_default=sa.text("30"), nullable=False
        ),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("published_by", sa.BigInteger(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # RESTRICT, per §4.3: deleting a profile must not silently blank a wall.
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["published_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_boards_slug"),
        sa.CheckConstraint(
            "orientation IN ('landscape','portrait')", name="ck_boards_orientation"
        ),
        # >= 16 chars, per §4.3. Enforced in the DB so a hand-written INSERT
        # cannot create a guessable public URL.
        sa.CheckConstraint("char_length(slug) >= 16", name="ck_boards_slug_length"),
        sa.CheckConstraint("items_per_page > 0", name="ck_boards_items_per_page"),
        sa.CheckConstraint("max_pages > 0", name="ck_boards_max_pages"),
    )
    op.create_index("ix_boards_profile_id", "boards", ["profile_id"])

    op.create_table(
        "board_items",
        sa.Column("board_id", sa.BigInteger(), nullable=False),
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("approved_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["board_id"], ["boards.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("board_id", "call_id"),
    )
    op.create_index(
        "ix_board_items_board_position", "board_items", ["board_id", "position"]
    )


def downgrade() -> None:
    op.drop_index("ix_board_items_board_position", table_name="board_items")
    op.drop_table("board_items")
    op.drop_index("ix_boards_profile_id", table_name="boards")
    op.drop_table("boards")
