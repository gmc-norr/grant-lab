from contextlib import contextmanager

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from call_monitor.auth.dependencies import get_current_user, require_admin
from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import User
from call_monitor.db.session import make_engine, make_session_factory


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
            u = User(email="alice@ki.se")
            s.add(u)
            s.commit()
            s.refresh(u)
            uid = u.id

        with TestClient(app) as client:
            client.cookies.set("session", signer.make(user_id=uid))
            r = client.get("/whoami")
    assert r.status_code == 200
    assert r.json()["email"] == "alice@ki.se"


def test_require_admin_rejects_non_admin(pg_url, pg_session_factory):
    with _mk_app(pg_url) as (app, signer, _):
        with pg_session_factory() as s:
            u = User(email="alice@ki.se", is_admin=False)
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
            u = User(email="root@ki.se", is_admin=True)
            s.add(u)
            s.commit()
            s.refresh(u)
            uid = u.id

        with TestClient(app) as client:
            client.cookies.set("session", signer.make(user_id=uid))
            r = client.get("/admin-only")
    assert r.status_code == 200
