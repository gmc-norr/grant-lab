import call_manager.auth.accounts as acc
from call_manager.auth.accounts import link_google_identity, resolve_google_account
from call_manager.db.models import User

ISS = "https://accounts.google.com"


def test_new_identity_creates_pending(pg_session_factory):
    with pg_session_factory() as s:
        res = resolve_google_account(s, iss=ISS, sub="s1", email="new@gmail.com", name="N")
        s.commit()
        assert res.outcome == "created"
        assert res.user.status == "pending" and res.user.is_admin is False


def test_returning_identity_matches(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="x@ki.se", status="active", oidc_iss=ISS, oidc_sub="s2"))
        s.commit()
    with pg_session_factory() as s:
        res = resolve_google_account(s, iss=ISS, sub="s2", email="x@ki.se", name=None)
        assert res.outcome == "matched" and res.user.status == "active"


def test_email_collision_blocked(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="admin@ki.se", status="active"))  # no identity
        s.commit()
    with pg_session_factory() as s:
        res = resolve_google_account(s, iss=ISS, sub="s3", email="admin@ki.se", name=None)
        assert res.outcome == "email_collision" and res.user is None
        assert s.query(User).filter_by(email="admin@ki.se").one().oidc_sub is None


def test_concurrent_insert_reresolves(pg_session_factory, monkeypatch):
    # Simulate the race: a competitor commits the same identity AFTER our first
    # existence check returns None. The flush hits the unique index -> we roll
    # back and re-resolve to the winner.
    with pg_session_factory() as s0:
        s0.add(User(email="pre@ki.se", status="active", oidc_iss=ISS, oidc_sub="race"))
        s0.commit()

    real = acc._by_identity
    calls = {"n": 0}

    def flaky(session, iss, sub):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # stale read: pretend not found
        return real(session, iss, sub)

    monkeypatch.setattr(acc, "_by_identity", flaky)
    with pg_session_factory() as s:
        res = resolve_google_account(s, iss=ISS, sub="race", email="newcomer@ki.se", name=None)
        assert res.outcome == "matched"
        assert res.user.email == "pre@ki.se"


def test_link_attaches_identity(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="me@ki.se", status="active")
        s.add(u)
        s.commit()
        uid = u.id
    with pg_session_factory() as s:
        assert link_google_identity(s, user_id=uid, iss=ISS, sub="s4") is True
        s.commit()
        assert s.get(User, uid).oidc_sub == "s4"


def test_link_rejects_used_identity(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="other@ki.se", status="active", oidc_iss=ISS, oidc_sub="s5"))
        u = User(email="mine@ki.se", status="active")
        s.add(u)
        s.commit()
        uid = u.id
    with pg_session_factory() as s:
        assert link_google_identity(s, user_id=uid, iss=ISS, sub="s5") is False
