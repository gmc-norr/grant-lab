"""store a board item's QR at approval instead of rendering it on the public path

THE BACKFILL IS O(rows) x ONE QR ENCODE, INSIDE THIS MIGRATION'S TRANSACTION, AND
IT IS UNBOUNDED. `board_items` is empty in production at the time of writing
(boards shipped 2026-09-06 and had not deployed), so this is a no-op there --
which is exactly why the cost is easy to miss. **Measure the row count before
running this anywhere else.** At the encode cost qr.py records (~45.5 ms at 800
bytes), 10k rows is ~7.5 minutes holding a transaction on `board_items`. If that
is unacceptable, split it: ship the three columns, backfill out of band, and let
the public route serve `qr_modules = 0` until it lands -- the comparison in
boards/public.py already makes a missing QR safe.

This caveat lives here rather than in the plan on purpose: this repo prunes
executed plans (CLAUDE.md), and a migration must carry its own hazard the way
0076 carries its downgrade's.

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
        # Skip the encoder for an empty url, matching what the application path
        # does at web/api/boards.py's approve_items. Call.url is Text NOT NULL
        # but permits "", and qr_for("") returns a real 21-module QR of the empty
        # string -- ~1.5 KB stored for a code that can never be served, since
        # qr_source_url is then NULL and boards/public.py's comparison is
        # permanently False.
        qr = qr_for(url) if url else None
        conn.execute(
            sa.text(
                "UPDATE board_items SET qr_path = :p, qr_modules = :m, qr_source_url = :u "
                "WHERE board_id = :b AND call_id = :c"
            ),
            {"p": (qr.path or None) if qr else None,
             "m": qr.modules if qr else 0,
             "u": url or None, "b": board_id, "c": call_id},
        )


def downgrade() -> None:
    op.drop_column("board_items", "qr_source_url")
    op.drop_column("board_items", "qr_modules")
    op.drop_column("board_items", "qr_path")
