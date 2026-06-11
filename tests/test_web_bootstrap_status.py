from fastapi.testclient import TestClient

from call_manager.db.models import Profile, ProfileBootstrapJob, ProfileKind, User
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


def _owner_profile(app, pg_session_factory, status, **jobkw):
    with pg_session_factory() as s:
        u = User(email="owner@ki.se", is_admin=False)
        s.add(u)
        s.flush()
        p = Profile(
            name="Grp",
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level="group",
            description="",
            structured={},
            created_by=u.id,
        )
        s.add(p)
        s.flush()
        s.add(ProfileBootstrapJob(profile_id=p.id, status=status, **jobkw))
        s.commit()
        uid, pid = u.id, p.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, pid


def test_edit_page_running_is_disabled_and_polls(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid = _owner_profile(app, pg_session_factory, "running")
    body = c.get(f"/profiles/{pid}/edit").text
    assert "Drafting" in body
    assert "disabled" in body  # the wrapping fieldset
    assert "/bootstrap-status" in body  # poll element mounted


def test_edit_page_done_unavailable_wording_and_editable(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid = _owner_profile(
        app, pg_session_factory, "done", pubmed_status="unavailable", publications_fetched=0
    )
    body = c.get(f"/profiles/{pid}/edit").text
    assert "PubMed was unavailable" in body
    assert "/bootstrap-status" not in body  # no poll once terminal


def test_status_route_sets_refresh_when_terminal(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid = _owner_profile(
        app, pg_session_factory, "done", pubmed_status="ok", publications_fetched=2
    )
    r = c.get(f"/profiles/{pid}/bootstrap-status")
    assert r.headers.get("HX-Refresh") == "true"


def test_status_route_keeps_polling_while_active(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid = _owner_profile(app, pg_session_factory, "running")
    r = c.get(f"/profiles/{pid}/bootstrap-status")
    assert r.status_code == 200
    assert "HX-Refresh" not in r.headers
