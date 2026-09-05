"""The four arms of readable_profile_ids(), one test per arm, against a real DB.

The architecture gate (tests/architecture/test_profile_access_single_source.py)
proves there is exactly ONE definition of the read rule and that every surface
calls it. It cannot prove the rule is CORRECT -- an AST walk sees a call, not a
row. This file is the other half: it runs the predicate against Postgres and
checks which profile ids come back for an owner, a shared-with colleague, a
stranger and an admin.

Why each arm needs its own test, concretely:

  created_by  -- the owner NEVER has a profile_viewers row (models.py:338-339).
                 An implementation that checks only the viewer table locks
                 owners out of their own private profiles, and every other test
                 here would still pass because they all use visibility='org'.
  is_admin    -- the same override profiles.py uses for editing.
  'org'       -- the day-one default; migration 0070 sets every existing row to
                 it, so this is the arm that keeps Monday working.
  viewers     -- the only arm that makes sharing mean anything.

And one negative: the predicate does NOT filter is_active. Callers already do,
and folding it in here would silently narrow the set for the admin/archive
surfaces that legitimately want inactive profiles.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from grant_lab.db.models import Profile, ProfileKind, ProfileViewer, User
from grant_lab.web.profile_access import can_read_profile, readable_profile_ids


@pytest.fixture()
def world(pg_session_factory):
    """Owner A, colleague B (viewer of P_priv), stranger C, admin D.

    P_org    -- visibility 'org'      (the default)
    P_priv   -- visibility 'private', B has a profile_viewers row
    P_secret -- visibility 'private', nobody has a viewer row

    All three are owned by A, so the owner arm and the viewer arm are
    distinguishable: A has no viewer row anywhere.
    """
    with pg_session_factory() as s:
        a = User(email="a-owner@umu.se", status="active")
        b = User(email="b-colleague@umu.se", status="active")
        c = User(email="c-stranger@umu.se", status="active")
        d = User(email="d-admin@umu.se", status="active", is_admin=True)
        s.add_all([a, b, c, d])
        s.flush()

        def _profile(name: str, visibility: str) -> Profile:
            p = Profile(
                name=name,
                kind=ProfileKind.ORGANIZATIONAL_UNIT,
                level="group",
                description=f"{name} description",
                structured={},
                created_by=a.id,
                visibility=visibility,
            )
            s.add(p)
            return p

        p_org = _profile("P_org", "org")
        p_priv = _profile("P_priv", "private")
        p_secret = _profile("P_secret", "private")
        s.flush()
        s.add(ProfileViewer(profile_id=p_priv.id, user_id=b.id))
        s.commit()

        ids = {
            "A": a.id,
            "B": b.id,
            "C": c.id,
            "D": d.id,
            "P_org": p_org.id,
            "P_priv": p_priv.id,
            "P_secret": p_secret.id,
        }

    # Read visibility back from the DB: Profile.visibility has no Python-side
    # default, so an unflushed instance reads None. Asserting on the persisted
    # value is the only way to know the seed is what the test claims.
    with pg_session_factory() as s:
        persisted = dict(s.execute(select(Profile.name, Profile.visibility)).all())
    assert persisted == {"P_org": "org", "P_priv": "private", "P_secret": "private"}, persisted
    return ids


def _readable(session, user) -> set[int]:
    """Ids the predicate admits, resolved the way every caller will use it:
    as a subquery inside `.in_()`, never materialised into a list."""
    return set(
        session.scalars(select(Profile.id).where(Profile.id.in_(readable_profile_ids(user)))).all()
    )


def _user(session, user_id: int) -> User:
    u = session.get(User, user_id)
    assert u is not None
    return u


@pytest.mark.parametrize(
    ("actor", "expected_names"),
    [
        # Owner arm: A owns all three and has NO viewer row anywhere.
        ("A", {"P_org", "P_priv", "P_secret"}),
        # Viewer arm: B is shared P_priv; P_secret stays invisible.
        ("B", {"P_org", "P_priv"}),
        # Org arm: C is a plain colleague -- the default-visibility profile only.
        ("C", {"P_org"}),
        # Admin arm: D sees everything, private or not.
        ("D", {"P_org", "P_priv", "P_secret"}),
    ],
)
def test_readable_profile_ids_per_arm(pg_session_factory, world, actor, expected_names):
    expected = {world[name] for name in expected_names}
    with pg_session_factory() as s:
        assert _readable(s, _user(s, world[actor])) == expected


@pytest.mark.parametrize(
    ("actor", "expected_names"),
    [
        ("A", {"P_org", "P_priv", "P_secret"}),
        ("B", {"P_org", "P_priv"}),
        ("C", {"P_org"}),
        ("D", {"P_org", "P_priv", "P_secret"}),
    ],
)
def test_can_read_profile_agrees_with_the_subquery(
    pg_session_factory, world, actor, expected_names
):
    """can_read_profile is composed FROM readable_profile_ids, so the two can
    never disagree -- this pins that they don't, per profile, per actor."""
    with pg_session_factory() as s:
        user = _user(s, world[actor])
        for name in ("P_org", "P_priv", "P_secret"):
            assert can_read_profile(s, user, world[name]) is (name in expected_names), (
                f"{actor} vs {name}"
            )


def test_can_read_profile_is_false_for_a_missing_profile(pg_session_factory, world):
    with pg_session_factory() as s:
        assert can_read_profile(s, _user(s, world["D"]), 10_000_000) is False


def test_predicate_ignores_is_active(pg_session_factory, world):
    """Archiving a profile must not change who MAY read it.

    is_active is the caller's filter -- list_profiles hides archived rows, the
    admin surfaces show them. If this predicate folded is_active in, every
    caller that wants archived rows would have to route around the single
    source, which is the exact duplication the gate exists to prevent.
    """
    with pg_session_factory() as s:
        s.execute(
            select(Profile).where(Profile.id == world["P_org"])
        ).scalar_one().is_active = False
        s.commit()

    with pg_session_factory() as s:
        c = _user(s, world["C"])
        assert world["P_org"] in _readable(s, c)
        assert can_read_profile(s, c, world["P_org"]) is True
