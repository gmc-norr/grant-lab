from fastapi.testclient import TestClient

from call_manager.db.models import Profile, ProfileKind, User
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def _client(app, pg_session_factory, set_default=False):
    with pg_session_factory() as s:
        u = User(status="active", email="u@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="Grp",
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level="group",
            description="d",
            structured={},
            created_by=u.id,
        )
        s.add(p)
        s.flush()
        if set_default:
            u.default_profile_id = p.id
        s.commit()
        uid, pid = u.id, p.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid, pid


def test_valid_selection_persists_default(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _client(app, pg_session_factory)
    c.get(f"/?profile={pid}")
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id == pid


def test_bare_root_uses_stored_default(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _client(app, pg_session_factory, set_default=True)
    assert str(pid) in c.get("/").text


def test_explicit_empty_clears_default(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _client(app, pg_session_factory, set_default=True)
    c.get("/?profile=")
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id is None


def test_invalid_id_does_not_destroy_default(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _client(app, pg_session_factory, set_default=True)
    c.get("/?profile=99999")
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id == pid  # unchanged


def test_whitespace_profile_does_not_clear_default(monkeypatch, pg_url, pg_session_factory):
    # Legacy parity: a whitespace-only ?profile preserved the stored default
    # (it never matched the exact-"" clear branch, fell through to the id branch,
    # parsed to None, and never wrote). The refactor must not clear it.
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _client(app, pg_session_factory, set_default=True)
    c.get("/?profile=%20%20")
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id == pid  # unchanged
