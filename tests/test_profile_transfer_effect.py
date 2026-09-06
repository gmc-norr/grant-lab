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

Also carries a gate for `_readable_ignoring_ownership`'s docstring claim: its
probe neutralises the owner arm AND the profile_viewers arm of
`readable_profile_ids` together, because both are keyed on `user.id`. That
claim is only true for the arm count that exists today, so
`test_readable_profile_ids_or_arm_count_is_pinned` below pins the argument
count of the `or_(...)` call directly from the source, by `ast`.
"""

import ast
from pathlib import Path

from grant_lab.db.models import Profile, ProfileKind, ProfileViewer, User
from grant_lab.web.api.profiles import transfer_read_effect

_PROFILE_ACCESS = (
    Path(__file__).resolve().parents[1] / "src" / "grant_lab" / "web" / "profile_access.py"
)


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


def test_a_stranger_who_never_owned_the_profile_loses_nothing(pg_session_factory):
    """Pins the `profile.created_by != outgoing.id` branch (profiles.py:161-162).

    Every other case in this file passes the profile's OWN owner as `outgoing`,
    so without this case the early `return False` for a non-owner is never
    exercised -- deleting it would leave the suite green while a stranger
    checked against someone else's private profile got `True` back, i.e. told
    "you will lose read access" for access they never had.
    """
    with pg_session_factory() as s:
        _owner, p = _owner_and_profile(s, "realowner@umu.se", "private")
        stranger = User(email="stranger@umu.se", name="stranger@umu.se", status="active")
        s.add(stranger)
        s.flush()
        assert transfer_read_effect(s, profile=p, outgoing=stranger) is False


def _readable_profile_ids_or_call() -> ast.Call:
    tree = ast.parse(_PROFILE_ACCESS.read_text(), filename=str(_PROFILE_ACCESS))
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "readable_profile_ids"
    )
    return next(
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "or_"
    )


def test_readable_profile_ids_or_arm_count_is_pinned():
    """Pins the argument count of the `or_(...)` inside `readable_profile_ids`.

    `_readable_ignoring_ownership` (web/api/profiles.py) neutralises the read
    predicate by probing with a synthetic `id=-1` user. That probe drops the
    owner arm on purpose, and drops the profile_viewers arm as a side effect --
    both arms compare against `user.id`, and the probe's docstring says so.
    Nothing else gates that coupling: if a fifth arm keyed on `user.id` is
    added to the `or_(...)` below, the probe would silently neutralise it too,
    and every behavioural test above would stay green because none of them
    can see the missing arm.

    Measured directly from the source, by `ast`, not asserted from memory: the
    `or_(...)` inside `readable_profile_ids` takes exactly 3 arguments today --
    `Profile.created_by == user.id`, `Profile.visibility == "org"`, and
    `Profile.id.in_(shared_ids)` (the profile_viewers membership). The
    `is_admin` arm is a short-circuit `return` before the `or_(...)` is ever
    built (profile_access.py:61-65), so it is not one of the 3 counted here.
    """
    count = len(_readable_profile_ids_or_call().args)
    assert count == 3, (
        f"readable_profile_ids' or_(...) now takes {count} argument(s), not "
        "the 3 this test measured. Before changing this number, re-check "
        "_readable_ignoring_ownership in web/api/profiles.py -- its docstring "
        "and its probe assume the synthetic id neutralises exactly the owner "
        "arm and the profile_viewers arm, and nothing else keyed on user.id."
    )
