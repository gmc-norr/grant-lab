"""boards.min_score / boards.page_seconds CHECK constraints

Revision ID: 0074
Revises: 0073

items_per_page and max_pages already had a DB floor (ck_boards_items_per_page,
ck_boards_max_pages, migration 0072). min_score and page_seconds did not --
`BoardSettingsIn` exposed both on `PUT /api/boards/{id}` with no pydantic bound
either, so `{"min_score": -50}` or `{"page_seconds": 0}` returned 200 and
persisted. The API-level fix (web/api/boards.py's BoardSettingsIn) is the
first line; this is the floor, matching the pattern the existing two columns
already set.

min_score's range comes from what it is compared against: CallRanking.aggregate_
score (boards/queue.py), which ranking/aggregate.py documents as a 0-100 scale,
the same range calls.py's own `min_score` query param already enforces
(web/api/calls.py:718, `Query(ge=0, le=100)`).
"""

from __future__ import annotations

from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_boards_min_score_range",
        "boards",
        "min_score BETWEEN 0 AND 100",
    )
    op.create_check_constraint(
        "ck_boards_page_seconds",
        "boards",
        "page_seconds > 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_boards_page_seconds", "boards", type_="check")
    op.drop_constraint("ck_boards_min_score_range", "boards", type_="check")
