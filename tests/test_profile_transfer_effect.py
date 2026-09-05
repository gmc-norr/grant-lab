"""What a transfer costs the OUTGOING owner, and it is not one sentence.

`readable_profile_ids` short-circuits on `if user.is_admin` (profile_access.py:61)
and returns every profile, so an admin previous owner loses nothing. The
decisions note records that an earlier revision stated the revocation
unconditionally and was wrong. This pins the difference.

FIXTURES -- READ THIS BEFORE COPYING A NAME. This repo has NO `db_session`.
The session fixture is `pg_session_factory` (tests/conftest.py:151) and it is a
FACTORY, used as `with pg_session_factory() as s:`. `profile_factory` (:184)
returns a `(profile_id, user_id)` TUPLE and accepts no `visibility` or
`created_by`, so these cases build their own rows. `ProfileKind` is a StrEnum
whose only values are `organizational_unit` and `individual`
(db/models/profiles.py:31-33). Measured against tests/conftest.py at 05bd151c.
"""

from grant_lab.db.models import Profile, ProfileKind, ProfileViewer, User
from grant_lab.web.api.profiles import transfer_read_effect


def _owner_and_profile(session, email, visibility, *, is_admin=False):
    u = User(email=email, name=email, status="active", is_admin=is_admin)
    session.add(u)
    session.flush()
    p = Profile(
        name=f"p-{email}",
        kind=ProfileKind.ORGANIZATIONAL_UNIT,
        level="group",
        description="",
        structured={},
        created_by=u.id,
        visibility=visibility,
    )
    session.add(p)
    session.flush()
    return u, p


def test_a_non_admin_owner_of_a_private_profile_loses_read(pg_session_factory):
    with pg_session_factory() as s:
        u, p = _owner_and_profile(s, "owner@umu.se", "private")
        assert transfer_read_effect(s, profile=p, outgoing=u) is True


def test_an_admin_previous_owner_loses_nothing(pg_session_factory):
    with pg_session_factory() as s:
        u, p = _owner_and_profile(s, "adminowner@umu.se", "private", is_admin=True)
        assert transfer_read_effect(s, profile=p, outgoing=u) is False


def test_an_org_profile_stays_readable_by_everyone(pg_session_factory):
    with pg_session_factory() as s:
        u, p = _owner_and_profile(s, "orgowner@umu.se", "org")
        assert transfer_read_effect(s, profile=p, outgoing=u) is False


def test_a_viewer_row_keeps_read_even_on_a_private_profile(pg_session_factory):
    with pg_session_factory() as s:
        u, p = _owner_and_profile(s, "vowner@umu.se", "private")
        s.add(ProfileViewer(profile_id=p.id, user_id=u.id))
        s.flush()
        assert transfer_read_effect(s, profile=p, outgoing=u) is False
