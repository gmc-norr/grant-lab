"""Migration 0073: `subscriptions.board_alerts_enabled`.

A separate migration from 0072 (Global Constraints, "Two migrations, not
one"): 0072 creates two tables Plan C owns end to end, 0073 alters a table
three other subsystems read, so reverting the alert flag must not also drop
the boards.

The load-bearing assertion is the `server_default`, not merely that the
column exists: `scripts/seed_dev_data.py:328` inserts a Subscription with
only `user_id` and `profile_id`, so without a server-side default every
seeded profile would silently opt out of board alerts (PLAN-DEFECTS.md N2).
That is why gate 2 inserts a row through raw SQL that OMITS the column
entirely, rather than reading the ORM model's Python-side `default=True`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError


def _mk_user(s, email: str) -> int:
    return s.scalar(
        text("INSERT INTO users (email, status) VALUES (:e, 'active') RETURNING id"),
        {"e": email},
    )


# ---------------------------------------------------------------------------
# Gate 1 - the column exists and is NOT NULL.
# ---------------------------------------------------------------------------
def test_column_exists_and_is_not_null(pg_session_factory):
    with pg_session_factory() as s:
        insp = inspect(s.bind)
        cols = {c["name"]: c for c in insp.get_columns("subscriptions")}
    assert "board_alerts_enabled" in cols
    assert cols["board_alerts_enabled"]["nullable"] is False


# ---------------------------------------------------------------------------
# Gate 2 - an INSERT that OMITS the column gets True from the server default.
# This is the case scripts/seed_dev_data.py:328 depends on.
# ---------------------------------------------------------------------------
def test_insert_omitting_the_column_defaults_to_true(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="Alert Default Profile")
    with pg_session_factory() as s:
        s.execute(
            text("INSERT INTO subscriptions (user_id, profile_id) VALUES (:u, :p)"),
            {"u": uid, "p": pid},
        )
        s.commit()
        val = s.scalar(
            text(
                "SELECT board_alerts_enabled FROM subscriptions "
                "WHERE user_id = :u AND profile_id = :p"
            ),
            {"u": uid, "p": pid},
        )
    assert val is True, (
        f"an INSERT that omits board_alerts_enabled must default to True via the "
        f"server_default, exactly what scripts/seed_dev_data.py:328's bare INSERT "
        f"relies on; got {val!r}"
    )


# ---------------------------------------------------------------------------
# Gate 3 - digest_enabled is untouched. The whole point of a SECOND boolean
# is that muting the digest must not also mute the board alert, and vice
# versa (Open-1). Both flags must be independently settable.
# ---------------------------------------------------------------------------
def test_digest_enabled_and_board_alerts_enabled_are_independent(
    pg_session_factory, profile_factory
):
    pid, uid = profile_factory(name="Independent Flags Profile")
    with pg_session_factory() as s:
        s.execute(
            text(
                "INSERT INTO subscriptions (user_id, profile_id, digest_enabled, "
                "board_alerts_enabled) VALUES (:u, :p, false, true)"
            ),
            {"u": uid, "p": pid},
        )
        s.commit()
        row = s.execute(
            text(
                "SELECT digest_enabled, board_alerts_enabled FROM subscriptions "
                "WHERE user_id = :u AND profile_id = :p"
            ),
            {"u": uid, "p": pid},
        ).one()
    assert row.digest_enabled is False
    assert row.board_alerts_enabled is True


# ---------------------------------------------------------------------------
# Gate 4 - NOT NULL is enforced against an explicit NULL, not just implied by
# the default. compare_metadata sees a missing NOT NULL, but this observes
# the live database directly rather than trusting the model.
# ---------------------------------------------------------------------------
def test_explicit_null_is_rejected(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="Null Rejection Profile")
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        s.execute(
            text(
                "INSERT INTO subscriptions (user_id, profile_id, board_alerts_enabled) "
                "VALUES (:u, :p, NULL)"
            ),
            {"u": uid, "p": pid},
        )
        s.commit()
