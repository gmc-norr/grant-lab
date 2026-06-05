import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import Profile, ProfileKind, ProfileLevel, User


def _user(s):
    u = User(email="owner@ki.se")
    s.add(u)
    s.flush()
    return u.id


def test_kind_enum_has_two_members():
    assert {k.value for k in ProfileKind} == {"organizational_unit", "individual"}


def test_create_unit_with_level_and_parent(pg_session_factory):
    with pg_session_factory() as s:
        uid = _user(s)
        parent = Profile(
            name="Hospital",
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level=ProfileLevel.INSTITUTION.value,
            created_by=uid,
        )
        s.add(parent)
        s.flush()
        child = Profile(
            name="Path Lab",
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level=ProfileLevel.LAB.value,
            parent_profile_id=parent.id,
            created_by=uid,
        )
        s.add(child)
        s.commit()
        got = s.execute(select(Profile).where(Profile.name == "Path Lab")).scalar_one()
        assert got.parent_profile_id == parent.id
        assert got.level == "lab"


def test_individual_allows_null_level(pg_session_factory):
    with pg_session_factory() as s:
        uid = _user(s)
        s.add(Profile(name="Dr A", kind="individual", level=None, created_by=uid))
        s.commit()  # no cross-field NOT NULL constraint


def test_bad_kind_rejected_by_check(pg_session_factory):
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        uid = _user(s)
        s.add(Profile(name="X", kind="banana", created_by=uid))
        s.commit()


def test_bad_level_rejected_by_check(pg_session_factory):
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        uid = _user(s)
        s.add(Profile(name="X", kind="organizational_unit", level="galaxy", created_by=uid))
        s.commit()
