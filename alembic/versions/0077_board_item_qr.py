"""store a board item's QR at approval instead of rendering it on the public path

Revision ID: 0077
Revises: 0076
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("board_items", sa.Column("qr_path", sa.Text(), nullable=True))
    op.add_column(
        "board_items",
        sa.Column("qr_modules", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("board_items", sa.Column("qr_source_url", sa.Text(), nullable=True))

    from grant_lab.boards.qr import qr_for

    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT bi.board_id, bi.call_id, c.url FROM board_items bi "
            "JOIN calls c ON c.id = bi.call_id"
        )
    ).all()
    for board_id, call_id, url in rows:
        qr = qr_for(url or "")
        conn.execute(
            sa.text(
                "UPDATE board_items SET qr_path = :p, qr_modules = :m, qr_source_url = :u "
                "WHERE board_id = :b AND call_id = :c"
            ),
            {"p": qr.path or None, "m": qr.modules, "u": url or None,
             "b": board_id, "c": call_id},
        )


def downgrade() -> None:
    op.drop_column("board_items", "qr_source_url")
    op.drop_column("board_items", "qr_modules")
    op.drop_column("board_items", "qr_path")
