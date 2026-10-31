from fastapi.testclient import TestClient

from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import User
from grant_lab.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _login(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="viewer@umu.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_list_active_by_name(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    profile_factory(name="Beta", structured={"areas": ["a", "b"]})
    profile_factory(name="Alpha", structured={"areas": ["x"]})
    profile_factory(name="Archived")  # active by default; archive it below
    # archive the third
    from grant_lab.db.models import Profile
    with pg_session_factory() as s:
        s.execute(Profile.__table__.select().where(Profile.name == "Archived")).first()
        archived_update = (
            Profile.__table__.update()
            .where(Profile.name == "Archived")
            .values(is_active=False)
        )
        s.execute(archived_update)
        s.commit()
    client = _login(app, pg_session_factory)
    with client:
        r = client.get("/api/profiles")
    assert r.status_code == 200
    body = r.json()
    names = [p["name"] for p in body["profiles"]]
    assert names == ["Alpha", "Beta"]  # active only, by name
    alpha = next(p for p in body["profiles"] if p["name"] == "Alpha")
    assert alpha["areas_count"] == 1
    assert alpha["kind"] == "organizational_unit"


def test_list_requires_auth(monkeypatch, pg_url):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/api/profiles")
    assert r.status_code == 401


# --- ownership on the list (UX review 2026-10-31) ----------------------------
# The list offered "Edit" on every profile, including ones the viewer cannot
# edit (the page then showed a disabled form), and never said whose a profile
# was. Each row now says whether it is the viewer's own and whether they may
# edit it, by the same rule GET /api/profiles/{id} uses for `can_edit`.

from grant_lab.db.models import Profile, ProfileKind  # noqa: E402


def _user(pg_session_factory, email, *, is_admin=False):
    with pg_session_factory() as s:
        u = User(status="active", email=email, is_admin=is_admin)
        s.add(u)
        s.commit()
        return u.id


def _org_profile(pg_session_factory, *, name, owner_id):
    with pg_session_factory() as s:
        p = Profile(
            name=name,
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level="group",
            description="d",
            structured={},
            created_by=owner_id,
            visibility="org",
        )
        s.add(p)
        s.commit()
        return p.id


def _list_as(app, uid):
    signer: SessionSigner = app.state.session_signer
    c = TestClient(app)
    c.cookies.set("session", signer.make(user_id=uid))
    return {p["name"]: p for p in c.get("/api/profiles").json()["profiles"]}


def test_each_row_says_whose_it_is_and_whether_you_may_edit_it(
    monkeypatch, pg_url, pg_session_factory
):
    app = _setup(monkeypatch, pg_url)
    owner = _user(pg_session_factory, "owner@umu.se")
    other = _user(pg_session_factory, "other@umu.se")
    admin = _user(pg_session_factory, "admin@umu.se", is_admin=True)
    _org_profile(pg_session_factory, name="Owner's", owner_id=owner)

    as_owner = _list_as(app, owner)["Owner's"]
    as_other = _list_as(app, other)["Owner's"]
    as_admin = _list_as(app, admin)["Owner's"]

    assert (as_owner["is_owner"], as_owner["can_edit"]) == (True, True)
    assert (as_other["is_owner"], as_other["can_edit"]) == (False, False)
    # Admins may edit any profile (the detail route's rule), but it is not theirs.
    assert (as_admin["is_owner"], as_admin["can_edit"]) == (False, True)
