from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Subscription, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _user(pg_session_factory, *, email="u@ki.se"):
    with pg_session_factory() as s:
        u = User(email=email, status="active")
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def test_me_lists_active_profiles_as_checkboxes(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    p1, _ = profile_factory(name="Group A")
    p2, _ = profile_factory(name="Group B")
    app = _setup(monkeypatch, pg_url)
    uid = _user(pg_session_factory)
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.get("/me")
    assert r.status_code == 200
    assert "Group A" in r.text
    assert "Group B" in r.text
    assert 'name="subscribe_' in r.text


def test_me_post_subscribe_creates_rows(monkeypatch, pg_url, pg_session_factory, profile_factory):
    p1, _ = profile_factory(name="Group A")
    p2, _ = profile_factory(name="Group B")
    app = _setup(monkeypatch, pg_url)
    uid = _user(pg_session_factory)
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.post(
            "/me/subscriptions",
            data={
                f"subscribe_{p1}": "on",
                # p2 omitted → not subscribed
            },
        )
    assert r.status_code == 200
    with pg_session_factory() as s:
        subs = s.execute(select(Subscription).where(Subscription.user_id == uid)).scalars().all()
    assert len(subs) == 1
    assert subs[0].profile_id == p1


def test_me_post_unsubscribe_removes_row(monkeypatch, pg_url, pg_session_factory, profile_factory):
    p1, _ = profile_factory(name="Group A")
    app = _setup(monkeypatch, pg_url)
    uid = _user(pg_session_factory)
    with pg_session_factory() as s:
        s.add(Subscription(user_id=uid, profile_id=p1))
        s.commit()
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        # subs form, no checkboxes → unsubscribe all
        client.post("/me/subscriptions", data={})
    with pg_session_factory() as s:
        subs = s.execute(select(Subscription).where(Subscription.user_id == uid)).scalars().all()
    assert subs == []


def test_me_post_silently_drops_unknown_profile_ids(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Posting subscribe_<nonexistent_id> must not 500 the handler."""
    p1, _ = profile_factory(name="Real")
    app = _setup(monkeypatch, pg_url)
    uid = _user(pg_session_factory)
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.post(
            "/me/subscriptions",
            data={
                f"subscribe_{p1}": "on",
                "subscribe_999999": "on",  # bogus
            },
        )
    assert r.status_code == 200
    with pg_session_factory() as s:
        subs = s.execute(select(Subscription).where(Subscription.user_id == uid)).scalars().all()
    assert {sub.profile_id for sub in subs} == {p1}


def test_me_post_silently_drops_inactive_profile_subscribe(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Subscribing to an INACTIVE profile must be silently dropped."""
    p1, _ = profile_factory(name="Inactive")
    with pg_session_factory() as s:
        from call_manager.db.models import Profile

        s.get(Profile, p1).is_active = False
        s.commit()
    app = _setup(monkeypatch, pg_url)
    uid = _user(pg_session_factory)
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.post(
            "/me/subscriptions",
            data={
                f"subscribe_{p1}": "on",
            },
        )
    assert r.status_code == 200
    with pg_session_factory() as s:
        subs = s.execute(select(Subscription).where(Subscription.user_id == uid)).scalars().all()
    assert subs == []


def test_subscription_edit_preserves_digest_enabled(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Editing the subscription set must NOT reset an existing row's
    digest_enabled flag. Regression: a delete-all-then-reinsert handler
    silently flipped a user-disabled digest back to the default True.
    """
    p1, _ = profile_factory(name="Kept")
    p2, _ = profile_factory(name="Added")
    app = _setup(monkeypatch, pg_url)
    uid = _user(pg_session_factory)
    with pg_session_factory() as s:
        # User is subscribed to p1 but has DISABLED its digest.
        s.add(Subscription(user_id=uid, profile_id=p1, digest_enabled=False))
        s.commit()

    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        # Re-save with p1 still checked plus a newly-added p2.
        r = client.post(
            "/me/subscriptions",
            data={f"subscribe_{p1}": "on", f"subscribe_{p2}": "on"},
        )
    assert r.status_code == 200
    with pg_session_factory() as s:
        rows = {
            row.profile_id: row
            for row in s.execute(select(Subscription).where(Subscription.user_id == uid)).scalars()
        }
    assert set(rows) == {p1, p2}
    # The pre-existing p1 row kept its disabled digest.
    assert rows[p1].digest_enabled is False
    # The freshly-inserted p2 row defaults to enabled.
    assert rows[p2].digest_enabled is True


def test_subscriptions_post_redirects_to_me(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Post/Redirect/Get: a plain form POST returns a 303 to /me so a
    browser refresh doesn't re-submit."""
    p1, _ = profile_factory(name="Group A")
    app = _setup(monkeypatch, pg_url)
    uid = _user(pg_session_factory)
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.post(
            "/me/subscriptions",
            data={f"subscribe_{p1}": "on"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/me"


def test_name_post_redirects_to_me(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    uid = _user(pg_session_factory)
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.post("/me/name", data={"name": "Alice"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/me"


def test_name_post_does_not_touch_subscriptions(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Name endpoint is physically separate from subscriptions endpoint."""
    p1, _ = profile_factory(name="Kept")
    app = _setup(monkeypatch, pg_url)
    uid = _user(pg_session_factory)
    with pg_session_factory() as s:
        s.add(Subscription(user_id=uid, profile_id=p1))
        s.commit()
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        client.post("/me/name", data={"name": "New"})
    with pg_session_factory() as s:
        subs = s.execute(select(Subscription).where(Subscription.user_id == uid)).scalars().all()
    assert len(subs) == 1
