# tests/test_digest_status_filter.py
from datetime import UTC, datetime

from call_manager.db.models import Profile, ProfileKind, Subscription, User
from call_manager.digest.assemble import assemble_for_user
from call_manager.digest.send import candidate_user_ids


def _profile_owned_by(s, owner_id) -> int:
    # Name is per-owner unique: profiles has a partial UNIQUE index on active
    # names (uq_profiles_active_name), so a fixed "P" would collide.
    p = Profile(
        name=f"P-{owner_id}",
        kind=ProfileKind.ORGANIZATIONAL_UNIT,
        level="group",
        description="d",
        structured={},
        created_by=owner_id,
    )
    s.add(p)
    s.flush()
    return p.id


def test_candidate_query_excludes_non_active(pg_session_factory):
    with pg_session_factory() as s:
        active = User(email="active@ki.se", status="active")
        disabled = User(email="disabled@ki.se", status="disabled")
        pending = User(email="pending@ki.se", status="pending")
        s.add_all([active, disabled, pending])
        s.flush()
        for u in (active, disabled, pending):
            s.add(Subscription(user_id=u.id, profile_id=_profile_owned_by(s, u.id)))
        s.commit()
        ids = set(candidate_user_ids(s))
        emails = {s.get(User, i).email for i in ids}
    assert "active@ki.se" in emails
    assert "disabled@ki.se" not in emails and "pending@ki.se" not in emails


def test_assemble_returns_none_for_non_active(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="d@ki.se", status="disabled")
        s.add(u)
        s.flush()
        s.add(Subscription(user_id=u.id, profile_id=_profile_owned_by(s, u.id)))
        s.commit()
        assert assemble_for_user(s, user_id=u.id, now=datetime.now(UTC)) is None
