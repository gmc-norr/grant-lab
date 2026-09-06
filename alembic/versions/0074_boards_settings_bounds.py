"""boards.min_score / boards.items_per_page / boards.page_seconds CHECK constraints

Revision ID: 0074
Revises: 0073

items_per_page and max_pages already had a DB floor (ck_boards_items_per_page,
ck_boards_max_pages, migration 0072) but no ceiling. min_score and
page_seconds had neither floor nor ceiling. `BoardSettingsIn` exposed all
three on `PUT /api/boards/{id}` with no matching pydantic bound, so e.g.
`{"page_seconds": 0}` returned 200 and persisted silently, and
`{"items_per_page": 3_000_000_000}` cleared int4's range and died on the
UPDATE as an uncaught out-of-range error (500). The API-level fix
(web/api/boards.py's BoardSettingsIn) is the first line; this is the floor and
ceiling to match.

This migration and 0072 were both written on an unmerged branch that has
never run outside ephemeral test containers, so widening items_per_page's
bound is done here rather than in a separate 0075: this file DROPS the
ck_boards_items_per_page constraint that migration 0072 created as
"items_per_page > 0" and recreates it as a range, in the same commit that
adds the min_score and page_seconds ranges below -- rather than shipping an
0075 that widens a constraint 0074 added moments earlier.

min_score's range comes from what it is compared against: CallRanking.aggregate_
score (boards/queue.py), which ranking/aggregate.py documents as a 0-100 scale,
the same range calls.py's own `min_score` query param already enforces
(web/api/calls.py:718, `Query(ge=0, le=100)`).

items_per_page's ceiling (64) and page_seconds's ceiling (3600, one hour) are
guard-rails against nonsense rather than product limits: spec §6's real grids
are 8 (landscape) and 4 (portrait), so 64 is roughly eight times the largest
real layout, and an hour per corridor-display page is already absurd -- a
typo, not a setting.
"""

from __future__ import annotations

from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_boards_items_per_page", "boards", type_="check")
    op.create_check_constraint(
        "ck_boards_items_per_page",
        "boards",
        "items_per_page BETWEEN 1 AND 64",
    )
    op.create_check_constraint(
        "ck_boards_min_score_range",
        "boards",
        "min_score BETWEEN 0 AND 100",
    )
    op.create_check_constraint(
        "ck_boards_page_seconds",
        "boards",
        "page_seconds BETWEEN 1 AND 3600",
    )


def downgrade() -> None:
    op.drop_constraint("ck_boards_page_seconds", "boards", type_="check")
    op.drop_constraint("ck_boards_min_score_range", "boards", type_="check")
    op.drop_constraint("ck_boards_items_per_page", "boards", type_="check")
    op.create_check_constraint(
        "ck_boards_items_per_page",
        "boards",
        "items_per_page > 0",
    )
