"""owner-scoped profile-name uniqueness

A globally-unique active name let any authenticated caller confirm the NAME of a
private profile they cannot read, by POSTing it and reading the 409 -- in the same
module whose `get_profile` carries a comment stating that profile names are the
thing worth not confirming.

`upgrade()` RELAXES a uniqueness constraint and so cannot fail on existing rows,
whatever is already stored. `downgrade()` TIGHTENS one and can -- see its own
comment below. Do not read the upgrade's safety as the rollback's.

Revision ID: 0076
Revises: 0075
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_profiles_active_name", table_name="profiles")
    op.create_index(
        "uq_profiles_active_owner_name",
        "profiles",
        ["created_by", "name"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )


def downgrade() -> None:
    # Re-creating the GLOBAL index can fail if two owners now hold the same
    # active name -- which this migration exists to permit. Deliberately not
    # papered over: a downgrade must be a decision about that data, not a
    # silent failure at 3am.
    op.drop_index("uq_profiles_active_owner_name", table_name="profiles")
    op.create_index(
        "uq_profiles_active_name",
        "profiles",
        ["name"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )
