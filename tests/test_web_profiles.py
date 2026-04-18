from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import User
from call_monitor.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _logged_in(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="u@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_profiles_list_requires_auth(monkeypatch, pg_url):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/profiles")
    assert r.status_code == 401


def test_profiles_list_shows_active_only(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    p_active, _ = profile_factory(name="Active group")
    p_inactive, _ = profile_factory(name="Inactive group")
    from call_monitor.db.models import Profile
    with pg_session_factory() as s:
        s.get(Profile, p_inactive).is_active = False
        s.commit()

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/profiles")
    assert r.status_code == 200
    assert "Active group" in r.text
    assert "Inactive group" not in r.text


def test_profile_new_form_renders(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/profiles/new")
    assert r.status_code == 200
    assert "name" in r.text.lower()
    assert "orcid" in r.text.lower()


def test_profile_new_post_creates_profile_and_redirects(
    monkeypatch, pg_url, pg_session_factory
):
    """POST creates a Profile synchronously (bootstrap is mocked via
    app.state override) and redirects to /profiles/<id>/edit."""
    from unittest.mock import patch

    from call_monitor.db.models import Profile
    from call_monitor.profiles.seed import DraftProfile

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)

    fake_draft = DraftProfile(
        description="fake draft description",
        structured={
            "areas": ["digital pathology"],
            "budget_min": None,
            "budget_max": None,
            "trl_min": None,
            "trl_max": None,
            "must_include": [],
            "hard_exclusions": [],
        },
        publications_fetched=0,
        publications_skipped=0,
    )
    with (
        patch("call_monitor.web.routes.profiles.bootstrap_profile", return_value=fake_draft),
        client,
    ):
        r = client.post(
            "/profiles/new",
            data={
                "name": "Pathology AI",
                "kind": "group",
                "orcids": "",
                "names_affiliations": "",
                "pasted_grants": "dummy",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/profiles/")
    assert r.headers["location"].endswith("/edit")
    with pg_session_factory() as s:
        p = s.execute(select(Profile).where(Profile.name == "Pathology AI")).scalar_one()
    assert p.description == "fake draft description"
    assert p.structured["areas"] == ["digital pathology"]
    assert p.is_active is True


def test_profile_edit_form_renders_existing_values(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(
        name="Edit me",
        description="initial desc",
        structured={"areas": ["x"], "must_include": ["kw"], "hard_exclusions": []},
    )
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get(f"/profiles/{pid}/edit")
    assert r.status_code == 200
    assert "initial desc" in r.text
    assert "Edit me" in r.text


def test_profile_edit_post_saves_and_triggers_backfill(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Saving an edit enqueues backfill jobs for the last 90 days of
    calls. We seed one recent Call and assert a RankingJob appears
    for (call, profile) after the save."""
    from datetime import UTC, datetime

    from call_monitor.db.models import Call, RankingJob, Source

    pid, _ = profile_factory(name="Backfill target")
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add(Call(
            source_id=src.id, external_id="E-1", url="https://x", title="T",
            published_at=datetime.now(UTC),
        ))
        s.commit()

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "updated description",
                "areas": "digital pathology\nfederated learning",
                "budget_min": "100000",
                "budget_max": "5000000",
                "trl_min": "4",
                "trl_max": "8",
                "must_include": "pathology",
                "hard_exclusions": "animal studies",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303

    from call_monitor.db.models import Profile
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.description == "updated description"
        assert p.structured["areas"] == ["digital pathology", "federated learning"]
        assert p.structured["budget_min"] == 100000
        assert p.structured["must_include"] == ["pathology"]
        assert p.structured["hard_exclusions"] == ["animal studies"]

        jobs = s.execute(select(RankingJob).where(RankingJob.profile_id == pid)).scalars().all()
    assert len(jobs) == 1  # our one recent Call got enqueued


def test_profile_view_renders_fields(monkeypatch, pg_url, pg_session_factory, profile_factory):
    pid, _ = profile_factory(
        name="Viewable",
        description="nice description",
        structured={"areas": ["digital pathology"], "must_include": ["kw1"], "hard_exclusions": []},
    )
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get(f"/profiles/{pid}")
    assert r.status_code == 200
    assert "Viewable" in r.text
    assert "nice description" in r.text
    assert "digital pathology" in r.text


def test_profile_view_404_for_unknown(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/profiles/99999")
    assert r.status_code == 404


def test_profile_new_rejects_too_long_name(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.post(
            "/profiles/new",
            data={
                "name": "X" * 201,
                "kind": "group",
                "orcids": "",
                "names_affiliations": "",
                "pasted_grants": "",
            },
            follow_redirects=False,
        )
    assert r.status_code == 400


def test_profile_new_rejects_oversized_grants(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.post(
            "/profiles/new",
            data={
                "name": "X",
                "kind": "group",
                "orcids": "",
                "names_affiliations": "",
                "pasted_grants": "a" * 100_001,
            },
            follow_redirects=False,
        )
    assert r.status_code == 413


def test_profile_edit_rejects_oversized_description(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="desc test")
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "d" * 50_001,
                "areas": "",
                "budget_min": "",
                "budget_max": "",
                "trl_min": "",
                "trl_max": "",
                "must_include": "",
                "hard_exclusions": "",
            },
            follow_redirects=False,
        )
    assert r.status_code == 413


def test_profile_edit_preserves_zero_budget_min(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Regression: stored budget_min=0 must round-trip through the edit form."""
    pid, _ = profile_factory(
        name="zero budget", structured={"budget_min": 0, "budget_max": 100}
    )
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get(f"/profiles/{pid}/edit")
    assert r.status_code == 200
    assert 'value="0"' in r.text  # budget_min=0 rendered as "0", not ""


def test_profile_new_rejects_duplicate_active_name(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="Duplicate me")
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.post(
            "/profiles/new",
            data={
                "name": "Duplicate me",
                "kind": "group",
                "orcids": "",
                "names_affiliations": "",
                "pasted_grants": "x",
            },
            follow_redirects=False,
        )
    assert r.status_code == 409


def test_profile_new_coerces_invalid_kind_to_group(
    monkeypatch, pg_url, pg_session_factory
):
    """Unknown kind value silently coerces to 'group'."""
    from unittest.mock import patch

    from call_monitor.profiles.seed import DraftProfile

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    fake_draft = DraftProfile(
        description="x",
        structured={
            "areas": [],
            "budget_min": None,
            "budget_max": None,
            "trl_min": None,
            "trl_max": None,
            "must_include": [],
            "hard_exclusions": [],
        },
        publications_fetched=0,
        publications_skipped=0,
    )
    with (
        patch("call_monitor.web.routes.profiles.bootstrap_profile", return_value=fake_draft),
        client,
    ):
        r = client.post(
            "/profiles/new",
            data={
                "name": "Coerced kind",
                "kind": "not-a-real-kind",
                "orcids": "",
                "names_affiliations": "",
                "pasted_grants": "",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    from call_monitor.db.models import Profile, ProfileKind
    with pg_session_factory() as s:
        p = s.execute(select(Profile).where(Profile.name == "Coerced kind")).scalar_one()
    assert p.kind == ProfileKind.GROUP


def test_profile_new_kind_org(monkeypatch, pg_url, pg_session_factory):
    from unittest.mock import patch

    from call_monitor.profiles.seed import DraftProfile

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    fake_draft = DraftProfile(
        description="x",
        structured={
            "areas": [],
            "budget_min": None,
            "budget_max": None,
            "trl_min": None,
            "trl_max": None,
            "must_include": [],
            "hard_exclusions": [],
        },
        publications_fetched=0,
        publications_skipped=0,
    )
    with (
        patch("call_monitor.web.routes.profiles.bootstrap_profile", return_value=fake_draft),
        client,
    ):
        r = client.post(
            "/profiles/new",
            data={
                "name": "Org profile",
                "kind": "org",
                "orcids": "",
                "names_affiliations": "",
                "pasted_grants": "",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    from call_monitor.db.models import Profile, ProfileKind
    with pg_session_factory() as s:
        p = s.execute(select(Profile).where(Profile.name == "Org profile")).scalar_one()
    assert p.kind == ProfileKind.ORG


def test_profile_new_requires_auth(monkeypatch, pg_url):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post(
            "/profiles/new",
            data={
                "name": "x",
                "kind": "group",
                "orcids": "",
                "names_affiliations": "",
                "pasted_grants": "",
            },
        )
    assert r.status_code == 401


def test_profile_edit_requires_auth(monkeypatch, pg_url, profile_factory):
    pid, _ = profile_factory(name="needs-auth")
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get(f"/profiles/{pid}/edit")
    assert r.status_code == 401


def test_profile_detail_requires_auth(monkeypatch, pg_url, profile_factory):
    pid, _ = profile_factory(name="detail-needs-auth")
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get(f"/profiles/{pid}")
    assert r.status_code == 401


def test_profile_edit_round_trips_budget_currency(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="Currency test")
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "x",
                "areas": "",
                "budget_min": "100000",
                "budget_max": "5000000",
                "budget_currency": "SEK",
                "trl_min": "",
                "trl_max": "",
                "must_include": "",
                "hard_exclusions": "",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    from call_monitor.db.models import Profile
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
    assert p.budget_currency == "SEK"


def test_profile_edit_accepts_empty_currency_as_null(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="No currency")
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "x", "areas": "", "budget_min": "", "budget_max": "",
                "budget_currency": "",
                "trl_min": "", "trl_max": "",
                "must_include": "", "hard_exclusions": "",
            },
            follow_redirects=False,
        )
    from call_monitor.db.models import Profile
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
    assert p.budget_currency is None


def test_profile_detail_renders_currency_when_set(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    from call_monitor.db.models import Profile
    pid, _ = profile_factory(
        name="Display currency",
        structured={"budget_min": 100000, "budget_max": 500000},
    )
    with pg_session_factory() as s:
        s.get(Profile, pid).budget_currency = "EUR"
        s.commit()
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get(f"/profiles/{pid}")
    assert r.status_code == 200
    assert "EUR" in r.text
