"""call_decisions replaces the global user_call_dismissals

Revision ID: 0071
Revises: 0070
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create, backfill, count the losses, THEN drop -- in one migration.
    # Leaving both tables in place is how two sources of truth start, and the
    # model/migration agreement gate (tests/test_schema_drift.py) is red for
    # every commit in which they disagree.
    #
    # state is String(10) + CHECK rather than a Postgres native enum, matching
    # ck_ranking_remote_attempts_outcome and ck_profiles_visibility: adding a
    # third state later is then an INSERT-time concern, not an ALTER TYPE.
    op.create_table(
        "call_decisions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("profile_id", sa.BigInteger(), nullable=False),
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=10), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # NULLABLE + ON DELETE SET NULL, not RESTRICT: the backfill below sets
        # decided_by = user_id, so RESTRICT would make deleting an ordinary
        # account fail outright even though user_id itself CASCADEs.
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "profile_id", "call_id", name="uq_call_decisions_user_profile_call"
        ),
        sa.CheckConstraint("state IN ('saved','hidden')", name="ck_call_decisions_state"),
    )
    # Serves the Saved / Hidden lists: filter (user_id, profile_id, state),
    # order by decided_at DESC (D12). The inbox's NOT EXISTS is already covered
    # by the unique index.
    op.create_index(
        "ix_call_decisions_user_profile_state",
        "call_decisions",
        ["user_id", "profile_id", "state"],
    )
    # D20. Both FKs CASCADE; without these, deleting a Call or a Profile
    # sequentially scans what will be the largest per-user table in the schema.
    op.create_index("ix_call_decisions_call_id", "call_decisions", ["call_id"])
    op.create_index("ix_call_decisions_profile_id", "call_decisions", ["profile_id"])

    # The backfill set, named exactly (spec 4.1): every profile the user is
    # SUBSCRIBED to, plus their users.default_profile_id, restricted to
    # profiles.is_active = true.
    #
    # NOT "every active profile in the database". Profile.is_active is a GLOBAL
    # flag, not a per-user one, so that reading would write a hide onto other
    # groups' profiles.
    #
    # NOT "the user's single default profile" either: that would silently
    # UN-hide calls people had already dealt with on their other profiles.
    #
    # And NOT filtered by the profile READ rule (Phase 2 decision D7). A row on
    # a profile the user can no longer read is harmless -- it is never surfaced
    # while unreadable -- and becomes correct again the moment the share is
    # restored. Putting web/profile_access.py's predicate into SQL here would
    # be a second copy of the one rule that must have exactly one definition.
    #
    # The ON CONFLICT is DEFENSIVE, NOT LOAD-BEARING, and this is measured, not
    # assumed: with the clause deleted the migration still produces exactly the
    # same three rows on the fixture in tests/test_migration_0071_call_decisions.py.
    # The OR sits in a JOIN's ON clause, where it is a boolean test evaluated
    # once per candidate profiles row -- it decides inclusion, it does not
    # duplicate. Keep it for re-run safety, but know that if the JOIN shape ever
    # becomes a UNION of a subscribed branch and a default branch, DO NOTHING
    # turns into the thing that silently swallows a real duplicate-row bug.
    op.execute("""
        INSERT INTO call_decisions
            (user_id, profile_id, call_id, state, decided_at, decided_by)
        SELECT d.user_id, p.id, d.call_id, 'hidden', d.dismissed_at, d.user_id
        FROM user_call_dismissals d
        JOIN users u ON u.id = d.user_id
        JOIN profiles p
          ON p.is_active = true
         AND (p.id IN (SELECT s.profile_id FROM subscriptions s WHERE s.user_id = d.user_id)
              OR p.id = u.default_profile_id)
        ON CONFLICT (user_id, profile_id, call_id) DO NOTHING
    """)

    # Report the loss, do not hide it. A dismissal whose owner has no active
    # subscribed or default profile fans out to nothing and is about to be
    # destroyed by the drop below. (The human runs the equivalent count against
    # the live database BEFORE migrating real data -- plan section "Decided by
    # the human", item 2 -- so the number is known in advance, not discovered in
    # a log.)
    conn = op.get_bind()
    orphans = conn.execute(
        sa.text("""
        SELECT count(*) FROM user_call_dismissals d
        WHERE NOT EXISTS (SELECT 1 FROM call_decisions c
                          WHERE c.user_id = d.user_id AND c.call_id = d.call_id)
    """)
    ).scalar_one()
    if orphans:
        print(
            f"0071: {orphans} dismissal(s) had no active subscribed or default "
            f"profile and were NOT migrated"
        )

    # The old table goes in the SAME migration that removes its last consumer:
    # leaving both in place is how two sources of truth start. Its four
    # importers (web/api/calls.py, web/dashboard.py, digest/assemble.py,
    # ranking/golden_set.py) and its mapped class itself are deleted in this
    # commit, so nothing can read it after this point.
    op.drop_table("user_call_dismissals")


def downgrade() -> None:
    """Rebuild user_call_dismissals from the 'hidden' rows. NOT LOSSLESS.

    THE PROFILE DIMENSION CANNOT BE RESTORED. user_call_dismissals is keyed
    (user_id, call_id) with no profile column, so a call hidden on three
    profiles collapses to ONE row and re-upgrading fans it back out to whatever
    the 0071 backfill picks -- which is not necessarily the three it started
    with. DISTINCT + min(decided_at) is chosen deliberately: the oldest decision
    is the one the original dismissal actually carried.

    THE ORPHANS CANNOT BE RESURRECTED EITHER. A dismissal that upgrade() could
    not fan out to any active subscribed-or-default profile wrote no
    call_decisions row, and the drop destroyed it. Downgrading does not bring it
    back; nothing can.

    'saved' rows are DROPPED, not rebuilt. The old table has exactly one
    meaning, "the user does not want to see this", and rebuilding a save as a
    dismissal would hide the calls the user explicitly kept -- the loudest
    possible way to be wrong.
    """
    op.create_table(
        "user_call_dismissals",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "dismissed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("user_id", "call_id", name="pk_user_call_dismissals"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name="fk_user_call_dismissals_user",
        ),
        sa.ForeignKeyConstraint(
            ["call_id"],
            ["calls.id"],
            ondelete="CASCADE",
            name="fk_user_call_dismissals_call",
        ),
    )
    op.execute("""
        INSERT INTO user_call_dismissals (user_id, call_id, dismissed_at)
        SELECT user_id, call_id, min(decided_at)
        FROM call_decisions
        WHERE state = 'hidden'
        GROUP BY user_id, call_id
    """)
    op.drop_index("ix_call_decisions_profile_id", table_name="call_decisions")
    op.drop_index("ix_call_decisions_call_id", table_name="call_decisions")
    op.drop_index("ix_call_decisions_user_profile_state", table_name="call_decisions")
    op.drop_table("call_decisions")
