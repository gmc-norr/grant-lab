from contextlib import contextmanager
from types import SimpleNamespace

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from grant_lab.auth.dependencies import get_current_user, require_admin
from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import User
from grant_lab.db.session import make_engine, make_session_factory


@contextmanager
def _mk_app(pg_url):
    """Build a throwaway FastAPI app + engine pair. The engine must be
    disposed at test end so pooled psycopg connections don't leak into
    the next test's ResourceWarning count."""
    signer = SessionSigner(secret="x" * 32, max_age_seconds=3600)
    engine = make_engine(pg_url)
    session_factory = make_session_factory(engine)

    app = FastAPI()
    app.state.session_signer = signer
    app.state.session_factory = session_factory
    # get_current_user applies the domain rule (security review 2026-10-31 S3).
    app.state.settings = SimpleNamespace(allowed_email_domains=["umu.se"])

    @app.get("/whoami")
    def whoami(user: User = Depends(get_current_user)):  # noqa: B008
        return {"id": user.id, "email": user.email, "is_admin": user.is_admin}

    @app.get("/admin-only")
    def admin_only(user: User = Depends(require_admin)):  # noqa: B008
        return {"id": user.id}

    try:
        yield app, signer, session_factory
    finally:
        engine.dispose()


def test_whoami_returns_401_without_cookie(pg_url):
    with _mk_app(pg_url) as (app, _, _), TestClient(app) as client:
        r = client.get("/whoami")
    assert r.status_code == 401


def test_whoami_works_with_valid_cookie(pg_url, pg_session_factory):
    with _mk_app(pg_url) as (app, signer, _):
        with pg_session_factory() as s:
            u = User(email="alice@umu.se", status="active")
            s.add(u)
            s.commit()
            s.refresh(u)
            uid = u.id

        with TestClient(app) as client:
            client.cookies.set("session", signer.make(user_id=uid))
            r = client.get("/whoami")
    assert r.status_code == 200
    assert r.json()["email"] == "alice@umu.se"


def test_stale_epoch_cookie_rejected(pg_url, pg_session_factory):
    """A cookie minted at epoch N is rejected once the user's session_epoch
    advances past N (logout / admin revocation)."""
    with _mk_app(pg_url) as (app, signer, _):
        with pg_session_factory() as s:
            u = User(email="alice@umu.se", status="active")
            s.add(u)
            s.commit()
            s.refresh(u)
            uid = u.id

        # Cookie carries epoch 0 (default).
        stale_cookie = signer.make(user_id=uid, session_epoch=0)

        # Simulate a logout/revocation bumping the epoch.
        with pg_session_factory() as s:
            s.get(User, uid).session_epoch = 1
            s.commit()

        with TestClient(app) as client:
            client.cookies.set("session", stale_cookie)
            r = client.get("/whoami")
    assert r.status_code == 401


def test_require_admin_rejects_non_admin(pg_url, pg_session_factory):
    with _mk_app(pg_url) as (app, signer, _):
        with pg_session_factory() as s:
            u = User(email="alice@umu.se", is_admin=False, status="active")
            s.add(u)
            s.commit()
            s.refresh(u)
            uid = u.id

        with TestClient(app) as client:
            client.cookies.set("session", signer.make(user_id=uid))
            r = client.get("/admin-only")
    assert r.status_code == 403


def test_require_admin_allows_admin(pg_url, pg_session_factory):
    with _mk_app(pg_url) as (app, signer, _):
        with pg_session_factory() as s:
            u = User(email="root@umu.se", is_admin=True, status="active")
            s.add(u)
            s.commit()
            s.refresh(u)
            uid = u.id

        with TestClient(app) as client:
            client.cookies.set("session", signer.make(user_id=uid))
            r = client.get("/admin-only")
    assert r.status_code == 200
