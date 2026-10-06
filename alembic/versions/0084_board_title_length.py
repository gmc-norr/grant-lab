"""boards: bound the title at 200 characters

Revision ID: 0084
Revises: 0083

The title is served to the anonymous public route on every poll, to every org
reader's board list and in the alert subject, and it had no upper bound: an
owner's 900 KB title went out at 900,145 bytes per poll (review 2026-10-06 M10).
The API bound is web/api/boards.py MAX_BOARD_TITLE_CHARS; this is the floor
under it, beside ck_boards_slug_length.
"""

from __future__ import annotations

from alembic import op

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The live database had one board, title 4 characters (read-only probe,
    # 2026-10-06). Trim anyway rather than fail a deploy on a row nobody can
    # see from here; an empty title was already refused by the API.
    op.execute("UPDATE boards SET title = left(title, 200) WHERE char_length(title) > 200")
    op.create_check_constraint(
        "ck_boards_title_length", "boards", "char_length(title) BETWEEN 1 AND 200"
    )


def downgrade() -> None:
    op.drop_constraint("ck_boards_title_length", "boards", type_="check")
