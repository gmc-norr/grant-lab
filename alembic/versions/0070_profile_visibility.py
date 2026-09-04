"""profile_viewers + profiles.visibility — day one changes nothing

Revision ID: 0070
Revises: 0069
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Shape copied verbatim from 0015_workspace_viewers.py: composite PK, both
    # FKs ON DELETE CASCADE, server-defaulted added_at. BigInteger on both
    # columns because profiles.id and users.id are both BigInteger.
    op.create_table(
        "profile_viewers",
        sa.Column("profile_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["profiles.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("profile_id", "user_id"),
    )

    # server_default 'org' so EVERY existing row is 'org' after this runs. That
    # is the whole no-op property: nobody's access narrows on the day of the
    # upgrade. Task 2's read predicate then treats 'org' exactly as today's
    # (absent) rule does.
    op.add_column(
        "profiles",
        sa.Column(
            "visibility",
            sa.String(length=10),
            nullable=False,
            server_default=sa.text("'org'"),
        ),
    )
    op.create_check_constraint(
        "ck_profiles_visibility",
        "profiles",
        "visibility IN ('private','org')",
    )

    # Backfill: every CURRENT subscriber becomes an explicit viewer.
    #
    # Without this the first owner who flips a profile to 'private' silently
    # cancels their colleagues' Monday digest -- no email, no warning, no audit
    # row. Copying today's access forward is the conservative direction; the
    # owner can then remove people deliberately, which is a decision with a
    # person behind it.
    #
    # The owner is excluded: readable_profile_ids() grants them access via
    # created_by, and a row would make "remove this viewer" able to lock an
    # owner out of their own profile.
    #
    # A disclosure in the OPPOSITE direction, which this backfill creates and no
    # part of the plan analysed (PD-10): every current non-owner subscriber
    # becomes VISIBLE to the profile's owner and to admins, by name and email,
    # through GET /api/profiles/{id}/viewers — before this migration nobody could
    # see who had subscribed to a profile.
    #
    # ON CONFLICT DO NOTHING: subscriptions is keyed (user_id, profile_id) and
    # profile_viewers on (profile_id, user_id), so the SELECT itself cannot
    # duplicate a pair -- the clause exists only for a RE-RUN. Measured, not
    # assumed: with the clause deleted, running this INSERT a second time
    # against an already-upgraded schema raises
    # `psycopg.errors.UniqueViolation: duplicate key value violates unique
    # constraint "profile_viewers_pkey"`. It is load-bearing.
    op.execute("""
        INSERT INTO profile_viewers (profile_id, user_id)
        SELECT s.profile_id, s.user_id
          FROM subscriptions s
          JOIN profiles p ON p.id = s.profile_id
         WHERE p.created_by <> s.user_id
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    """Drop the column and the table.

    This is NOT a round trip. Dropping profiles.visibility destroys the record
    of which profiles their owners had made private; re-running upgrade() puts
    every one of them back to 'org'. Downgrading is a way to get the schema back,
    not the decisions.
    """
    op.drop_constraint("ck_profiles_visibility", "profiles", type_="check")
    op.drop_column("profiles", "visibility")
    op.drop_table("profile_viewers")
