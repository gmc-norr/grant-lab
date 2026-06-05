"""Profile redesign: two-kind model + level + parent_profile_id

Revision ID: 0034
Revises: 0033
Create Date: 2026-06-05

Upgrade: convert profiles.kind from the profile_kind enum to Text+Check
(org/group -> organizational_unit, user -> individual); add level (group ->
'group', org -> NULL) and a reserved parent_profile_id FK. New structured.*
keys need no migration (read via .get()).

Downgrade is EXPLICITLY LOSSY: organizational_unit -> group, individual ->
user; the original org/group split is unrecoverable and level/parent_profile_id
are dropped. See the design spec.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add level first, populate from the *legacy* kind, THEN remap kind.
    op.add_column("profiles", sa.Column("level", sa.Text(), nullable=True))
    op.execute("UPDATE profiles SET level = 'group' WHERE kind = 'group'")

    op.execute("ALTER TABLE profiles ALTER COLUMN kind TYPE text USING kind::text")
    op.execute("DROP TYPE profile_kind")
    op.execute(
        "UPDATE profiles SET kind = CASE "
        "WHEN kind IN ('org','group') THEN 'organizational_unit' "
        "WHEN kind = 'user' THEN 'individual' ELSE kind END"
    )
    op.create_check_constraint(
        "ck_profiles_kind", "profiles", "kind IN ('organizational_unit','individual')"
    )
    op.create_check_constraint(
        "ck_profiles_level",
        "profiles",
        "level IS NULL OR level IN ('lab','group','department','clinic','institution','other')",
    )
    op.add_column("profiles", sa.Column("parent_profile_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_profiles_parent_profile_id",
        "profiles",
        "profiles",
        ["parent_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_profiles_parent_profile_id", "profiles", ["parent_profile_id"])


def downgrade() -> None:
    op.drop_index("ix_profiles_parent_profile_id", table_name="profiles")
    op.drop_constraint("fk_profiles_parent_profile_id", "profiles", type_="foreignkey")
    op.drop_column("profiles", "parent_profile_id")
    # IF EXISTS: tolerate a schema where a later migration (or operational fix)
    # has already replaced these check constraints — the downgrade should still
    # complete rather than error on a missing constraint.
    op.execute("ALTER TABLE profiles DROP CONSTRAINT IF EXISTS ck_profiles_level")
    op.drop_column("profiles", "level")
    # Lossy: collapse back to the legacy enum domain.
    op.execute("ALTER TABLE profiles DROP CONSTRAINT IF EXISTS ck_profiles_kind")
    op.execute(
        "UPDATE profiles SET kind = CASE "
        "WHEN kind = 'organizational_unit' THEN 'group' "
        "WHEN kind = 'individual' THEN 'user' ELSE kind END"
    )
    op.execute("CREATE TYPE profile_kind AS ENUM ('org','group','user')")
    # Guard the cast: any kind value outside the legacy enum domain (e.g. a
    # post-redesign kind that the CASE above didn't remap) would make the
    # USING kind::profile_kind cast fail. Collapse stragglers to 'group' so the
    # downgrade can never error on unexpected data.
    op.execute("UPDATE profiles SET kind = 'group' WHERE kind NOT IN ('org','group','user')")
    op.execute("ALTER TABLE profiles ALTER COLUMN kind TYPE profile_kind USING kind::profile_kind")
