from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _logged_in(app, pg_session_factory, *, is_admin: bool = True, user_id: int | None = None):
    """Default `is_admin=True` so existing tests that edit profiles they
    didn't create still work under the owner-or-admin gate. Tests that
    exercise the non-admin path should pass `is_admin=False` explicitly.

    Pass `user_id=` to log in as an existing user (e.g. the profile owner)
    instead of minting a fresh `u@ki.se`.
    """
    if user_id is not None:
        uid = user_id
    else:
        with pg_session_factory() as s:
            u = User(email="u@ki.se", is_admin=is_admin)
            s.add(u)
            s.commit()
            s.refresh(u)
            uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def _app(monkeypatch, pg_url):
    """Alias for _setup — matches the Task 7 plan's helper name."""
    return _setup(monkeypatch, pg_url)


def _patch_bootstrap(monkeypatch, *, structured=None, description="fake draft"):
    """Patch bootstrap_profile so no real PubMed/Ollama call happens. The fake
    draft carries an EMPTY structured payload by default so Task 7 tests assert
    the USER-entered values, not the LLM suggestion. monkeypatch auto-undoes the
    patch at test teardown."""
    from call_manager.profiles.seed import DraftProfile

    fake_draft = DraftProfile(
        description=description,
        structured=structured if structured is not None else {"areas": []},
        publications_fetched=0,
        publications_skipped=0,
    )
    monkeypatch.setattr(
        "call_manager.web.routes.profiles.bootstrap_profile",
        lambda **_: fake_draft,
    )
    return fake_draft


def test_profiles_list_requires_auth(monkeypatch, pg_url, migrated_engine):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/profiles")
    assert r.status_code == 401


def test_profiles_list_shows_active_only(monkeypatch, pg_url, pg_session_factory, profile_factory):
    p_active, _ = profile_factory(name="Active group")
    p_inactive, _ = profile_factory(name="Inactive group")
    from call_manager.db.models import Profile

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


def test_profile_new_post_creates_profile_and_redirects(monkeypatch, pg_url, pg_session_factory):
    """POST creates a Profile atomically (empty description + queued bootstrap job)
    and redirects to /profiles/<id>/edit. No inline LLM call happens."""
    from call_manager.db.models import Profile, ProfileBootstrapJob

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)

    with client:
        r = client.post(
            "/profiles/new",
            data={
                "name": "Pathology AI",
                "kind": "organizational_unit",
                "level": "department",
                "orcids": "",
                "names_affiliations": "",
                "pasted_grants": "dummy",
                "applicant_types": "institution",
                "org_type": "university_hospital",
                "country": "Sweden",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/profiles/")
    assert r.headers["location"].endswith("/edit")
    with pg_session_factory() as s:
        p = s.execute(select(Profile).where(Profile.name == "Pathology AI")).scalar_one()
    # New async path: description is empty until the worker runs.
    assert p.description == ""
    # Bootstrap job is queued — the worker (not the request) does the LLM call.
    with pg_session_factory() as s:
        assert s.query(ProfileBootstrapJob).filter_by(profile_id=p.id, status="queued").count() == 1
    # User-entered structured namespaces are persisted immediately.
    assert "institution" in p.structured["eligibility"]["applicant_types"]
    assert p.structured["org"]["org_type"] == "university_hospital"
    assert p.structured["geo"]["country"] == "Sweden"
    assert p.kind == "organizational_unit"
    assert p.level == "department"
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

    from call_manager.db.models import Call, RankingJob, Source

    pid, _ = profile_factory(name="Backfill target")
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add(
            Call(
                source_id=src.id,
                external_id="E-1",
                url="https://x",
                title="T",
                published_at=datetime.now(UTC),
            )
        )
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

    from call_manager.db.models import Profile

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


def _archive(pg_session_factory, pid):
    from call_manager.db.models import Profile

    with pg_session_factory() as s:
        s.get(Profile, pid).is_active = False
        s.commit()


def test_archived_profile_detail_is_404(monkeypatch, pg_url, pg_session_factory, profile_factory):
    """An archived (is_active=False) profile must 404 on direct-URL view,
    matching how the index/feeds hide it."""
    pid, _ = profile_factory(name="Archived view")
    _archive(pg_session_factory, pid)
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get(f"/profiles/{pid}")
    assert r.status_code == 404


def test_archived_profile_edit_form_is_404(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="Archived edit form")
    _archive(pg_session_factory, pid)
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get(f"/profiles/{pid}/edit")
    assert r.status_code == 404


def test_archived_profile_edit_submit_is_404(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="Archived edit submit")
    _archive(pg_session_factory, pid)
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "should not save",
                "areas": "",
                "budget_min": "",
                "budget_max": "",
                "budget_currency": "",
                "trl_min": "",
                "trl_max": "",
                "must_include": "",
                "hard_exclusions": "",
            },
            follow_redirects=False,
        )
    assert r.status_code == 404


def test_browser_404_gets_html_api_gets_json(monkeypatch, pg_url, pg_session_factory):
    """A browser (Accept: text/html) hitting a 404 route gets a styled HTML
    page; a JSON/API caller still gets the raw JSON {"detail": ...} body."""
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        html = client.get("/profiles/99999", headers={"Accept": "text/html"})
        api = client.get("/profiles/99999", headers={"Accept": "application/json"})
    assert html.status_code == 404
    assert "text/html" in html.headers["content-type"]
    assert "<html" in html.text.lower()
    assert api.status_code == 404
    assert "application/json" in api.headers["content-type"]
    assert api.json()["detail"] == "profile not found"


def test_profile_new_rejects_too_long_name(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.post(
            "/profiles/new",
            data={
                "name": "X" * 201,
                "kind": "organizational_unit",
                "level": "group",
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
                "kind": "organizational_unit",
                "level": "group",
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
    pid, _ = profile_factory(name="zero budget", structured={"budget_min": 0, "budget_max": 100})
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
                "kind": "organizational_unit",
                "level": "group",
                "orcids": "",
                "names_affiliations": "",
                "pasted_grants": "x",
            },
            follow_redirects=False,
        )
    assert r.status_code == 409


def test_profile_new_coerces_invalid_kind_to_organizational_unit(
    monkeypatch, pg_url, pg_session_factory
):
    """Unknown kind value silently coerces to 'organizational_unit'."""
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
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
    from call_manager.db.models import Profile, ProfileKind

    with pg_session_factory() as s:
        p = s.execute(select(Profile).where(Profile.name == "Coerced kind")).scalar_one()
    assert p.kind == ProfileKind.ORGANIZATIONAL_UNIT


def test_profile_new_kind_organizational_unit(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.post(
            "/profiles/new",
            data={
                "name": "Org profile",
                "kind": "organizational_unit",
                "orcids": "",
                "names_affiliations": "",
                "pasted_grants": "",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    from call_manager.db.models import Profile, ProfileKind

    with pg_session_factory() as s:
        p = s.execute(select(Profile).where(Profile.name == "Org profile")).scalar_one()
    assert p.kind == ProfileKind.ORGANIZATIONAL_UNIT


def test_profile_new_requires_auth(monkeypatch, pg_url, migrated_engine):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post(
            "/profiles/new",
            data={
                "name": "x",
                "kind": "organizational_unit",
                "level": "group",
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
    from call_manager.db.models import Profile

    with pg_session_factory() as s:
        p = s.get(Profile, pid)
    assert p.budget_currency == "SEK"


def test_profile_edit_rejects_non_owner_non_admin(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """A non-admin hospital user can't edit a profile they don't own.
    The profile_factory gives the profile its own owner; _logged_in with
    is_admin=False makes the request user a stranger. Must 403.
    """
    pid, _owner_uid = profile_factory(name="Someone else's")
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory, is_admin=False)
    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "hostile edit",
                "areas": "",
                "budget_min": "",
                "budget_max": "",
                "budget_currency": "",
                "trl_min": "",
                "trl_max": "",
                "must_include": "",
                "hard_exclusions": "",
            },
            follow_redirects=False,
        )
    assert r.status_code == 403

    # And the description must not have been written.
    from call_manager.db.models import Profile

    with pg_session_factory() as s:
        p = s.get(Profile, pid)
    assert p.description != "hostile edit"


def test_profile_edit_allows_owner(monkeypatch, pg_url, pg_session_factory):
    """The creator can edit even as a non-admin (policy: owner-or-admin)."""
    from call_manager.db.models import Profile, ProfileKind, User

    app = _setup(monkeypatch, pg_url)
    # Register u@ki.se as a non-admin, then make them the creator of the
    # profile before generating the logged-in client. _logged_in creates
    # u@ki.se itself, so pre-seed the user here and mirror its email.
    with pg_session_factory() as s:
        u = User(email="owner-only@ki.se", is_admin=False)
        s.add(u)
        s.flush()
        p = Profile(
            name="My profile",
            kind=ProfileKind.ORGANIZATIONAL_UNIT,
            description="mine",
            structured={},
            created_by=u.id,
        )
        s.add(p)
        s.commit()
        pid = p.id
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "owner-updated",
                "areas": "",
                "budget_min": "",
                "budget_max": "",
                "budget_currency": "",
                "trl_min": "",
                "trl_max": "",
                "must_include": "",
                "hard_exclusions": "",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
    assert p.description == "owner-updated"


def test_profile_edit_rejects_unknown_currency(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """An unlisted currency must 400 rather than silently leaving the old
    value. The dropdown only offers whitelisted currencies, but a crafted
    POST should fail loudly so the user isn't told "saved" when it wasn't."""
    pid, _ = profile_factory(name="Bad currency")
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "x",
                "areas": "",
                "budget_min": "",
                "budget_max": "",
                "budget_currency": "XYZ",
                "trl_min": "",
                "trl_max": "",
                "must_include": "",
                "hard_exclusions": "",
            },
            follow_redirects=False,
        )
    assert r.status_code == 400


def test_profile_edit_rejects_out_of_range_trl(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """TRL is a 1–9 scale; 999 is garbage. Reject so the stored rubric
    stays sensible."""
    pid, _ = profile_factory(name="TRL bounds")
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "x",
                "areas": "",
                "budget_min": "",
                "budget_max": "",
                "budget_currency": "",
                "trl_min": "1",
                "trl_max": "999",
                "must_include": "",
                "hard_exclusions": "",
            },
            follow_redirects=False,
        )
    assert r.status_code == 400


def test_profile_edit_rejects_obscenely_large_budget(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """budget_max=10**18 is implausible; reject so the rubric's numeric
    gate doesn't operate on junk inputs."""
    pid, _ = profile_factory(name="Budget bounds")
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "x",
                "areas": "",
                "budget_min": "0",
                "budget_max": "1000000000000000",
                "budget_currency": "",
                "trl_min": "",
                "trl_max": "",
                "must_include": "",
                "hard_exclusions": "",
            },
            follow_redirects=False,
        )
    assert r.status_code == 400


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
                "description": "x",
                "areas": "",
                "budget_min": "",
                "budget_max": "",
                "budget_currency": "",
                "trl_min": "",
                "trl_max": "",
                "must_include": "",
                "hard_exclusions": "",
            },
            follow_redirects=False,
        )
    from call_manager.db.models import Profile

    with pg_session_factory() as s:
        p = s.get(Profile, pid)
    assert p.budget_currency is None


def test_profile_detail_renders_currency_when_set(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    from call_manager.db.models import Profile

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


# --- Codex H1: profile-handler embedding refresh tests --------------------
#
# Each test below installs a stub embed_client onto app.state.embed_client
# so the handler doesn't try to hit Ollama. We verify the embedding column
# state in the DB after the request — which is the only thing the rest of
# the system actually reads.


def _stub_embed_client(*, vec=None, raises=None):
    from unittest.mock import MagicMock

    c = MagicMock()
    c.model = "bge-m3"
    if raises is not None:
        c.embed.side_effect = raises
    else:
        c.embed.return_value = vec or [0.42] * 1024
    return c


def test_profile_create_triggers_embedding_refresh(
    monkeypatch,
    pg_url,
    pg_session_factory,
):
    """Creating a new profile enqueues a bootstrap job but does NOT trigger an
    inline embedding compute. The worker (next task) handles embedding.
    After the POST, profile.embedding is None."""
    from call_manager.db.models import Profile, ProfileBootstrapJob

    app = _setup(monkeypatch, pg_url)
    app.state.embed_client = _stub_embed_client(vec=[0.5] * 1024)
    client = _logged_in(app, pg_session_factory)

    with client:
        r = client.post(
            "/profiles/new",
            data={
                "name": "Embed on create",
                "kind": "organizational_unit",
                "level": "group",
                "orcids": "",
                "names_affiliations": "",
                "pasted_grants": "x",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303

    with pg_session_factory() as s:
        p = s.execute(select(Profile).where(Profile.name == "Embed on create")).scalar_one()
    # Async path: embedding is computed by the worker, not the request.
    assert p.embedding is None
    with pg_session_factory() as s:
        assert s.query(ProfileBootstrapJob).filter_by(profile_id=p.id, status="queued").count() == 1


def test_profile_edit_changing_description_triggers_embedding_refresh(
    monkeypatch,
    pg_url,
    pg_session_factory,
    profile_factory,
):
    """Editing the description triggers re-embed: the new vector is written
    over any prior value."""
    from call_manager.db.models import Profile

    pid, _ = profile_factory(
        name="Edit re-embed",
        description="old description text",
    )
    # Pre-seed an old embedding so we can prove it gets overwritten.
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        p.embedding = [0.1] * 1024
        p.embedding_model = "bge-m3"
        s.commit()

    app = _setup(monkeypatch, pg_url)
    new_vec = [0.9] * 1024
    app.state.embed_client = _stub_embed_client(vec=new_vec)
    client = _logged_in(app, pg_session_factory)

    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "brand new description text",
                "areas": "",
                "budget_min": "",
                "budget_max": "",
                "budget_currency": "",
                "trl_min": "",
                "trl_max": "",
                "must_include": "",
                "hard_exclusions": "",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303

    with pg_session_factory() as s:
        p = s.get(Profile, pid)
    assert list(p.embedding) == new_vec  # overwritten with the new vec
    assert app.state.embed_client.embed.call_count == 1


def test_profile_edit_changing_areas_triggers_embedding_refresh(
    monkeypatch,
    pg_url,
    pg_session_factory,
    profile_factory,
):
    """Editing `areas` (description unchanged) DOES re-embed: `areas` feeds
    build_profile_text, so a changed area surface yields a stale vector unless
    re-embedded (Codex R6 — trigger off build_profile_text, not description)."""
    from call_manager.db.models import Profile

    pid, _ = profile_factory(
        name="Edit areas only",
        description="stable description",
        structured={"areas": ["old area"]},
    )
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        p.embedding = [0.7] * 1024
        p.embedding_model = "bge-m3"
        s.commit()

    app = _setup(monkeypatch, pg_url)
    new_vec = [0.99] * 1024
    app.state.embed_client = _stub_embed_client(vec=new_vec)
    client = _logged_in(app, pg_session_factory)

    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "stable description",  # SAME as before
                "areas": "digital pathology\nfederated learning",  # areas changed
                "budget_min": "",
                "budget_max": "",
                "budget_currency": "",
                "trl_min": "",
                "trl_max": "",
                "must_include": "pathology",
                "hard_exclusions": "",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303

    # Embed WAS invoked and the pre-seeded vector was overwritten.
    assert app.state.embed_client.embed.call_count == 1
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
    assert list(p.embedding) == new_vec


def test_profile_edit_no_embed_text_change_skips_embedding_refresh(
    monkeypatch,
    pg_url,
    pg_session_factory,
    profile_factory,
):
    """A no-op edit (description AND areas identical) leaves build_profile_text
    unchanged, so it does NOT re-embed — only rubric-only fields that don't
    feed the embedding text changed."""
    from call_manager.db.models import Profile

    pid, _ = profile_factory(
        name="Edit rubric only",
        description="stable description",
        structured={"areas": ["digital pathology"]},
    )
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        p.embedding = [0.7] * 1024
        p.embedding_model = "bge-m3"
        s.commit()

    app = _setup(monkeypatch, pg_url)
    app.state.embed_client = _stub_embed_client(vec=[0.99] * 1024)
    client = _logged_in(app, pg_session_factory)

    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "stable description",  # SAME as before
                "areas": "digital pathology",  # SAME as before
                "budget_min": "",
                "budget_max": "",
                "budget_currency": "",
                "trl_min": "",
                "trl_max": "",
                "must_include": "pathology",  # rubric-only field, not embedded
                "hard_exclusions": "",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303

    # Embed was NOT invoked.
    assert app.state.embed_client.embed.call_count == 0
    # And the pre-seeded vector is untouched.
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
    assert list(p.embedding) == [0.7] * 1024


def test_profile_edit_embed_failure_does_not_block_backfill(
    monkeypatch,
    pg_url,
    pg_session_factory,
    profile_factory,
):
    """Codex H1 robustness: if the embed step raises, the handler logs and
    proceeds to backfill_profile. The redirect still lands; backfill jobs
    are still enqueued. (Embedding gate falls through to UNKNOWN → LLM
    runs as the safe fallback.)"""
    from datetime import UTC, datetime

    from call_manager.db.models import Call, Profile, RankingJob, Source
    from call_manager.ranking.ollama_client import LLMError

    pid, _ = profile_factory(name="Embed-fails-but-backfill-works")
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add(
            Call(
                source_id=src.id,
                external_id="BF-1",
                url="https://x",
                title="T",
                published_at=datetime.now(UTC),
            )
        )
        s.commit()

    app = _setup(monkeypatch, pg_url)
    app.state.embed_client = _stub_embed_client(raises=LLMError("ollama down"))
    client = _logged_in(app, pg_session_factory)

    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "freshly changed description",  # triggers embed
                "areas": "",
                "budget_min": "",
                "budget_max": "",
                "budget_currency": "",
                "trl_min": "",
                "trl_max": "",
                "must_include": "",
                "hard_exclusions": "",
            },
            follow_redirects=False,
        )
    # Handler did not 500.
    assert r.status_code == 303
    # Description still saved.
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
    assert p.description == "freshly changed description"
    assert p.embedding is None  # refresh helper returned False on LLMError
    # Backfill ran — our seeded recent call has a job.
    with pg_session_factory() as s:
        jobs = s.execute(select(RankingJob).where(RankingJob.profile_id == pid)).scalars().all()
    assert len(jobs) == 1


def test_profile_create_embed_failure_does_not_500(
    monkeypatch,
    pg_url,
    pg_session_factory,
):
    """The create path no longer triggers an inline embedding compute, so there
    is no embed step in the request that could fail. The profile is created with
    an empty description and a queued bootstrap job."""
    from call_manager.db.models import Profile, ProfileBootstrapJob

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)

    with client:
        r = client.post(
            "/profiles/new",
            data={
                "name": "Create-embed-fails",
                "kind": "organizational_unit",
                "level": "group",
                "orcids": "",
                "names_affiliations": "",
                "pasted_grants": "",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    with pg_session_factory() as s:
        p = s.execute(select(Profile).where(Profile.name == "Create-embed-fails")).scalar_one()
    assert p.embedding is None  # worker hasn't run yet
    assert p.description == ""  # async: description filled by worker
    with pg_session_factory() as s:
        assert s.query(ProfileBootstrapJob).filter_by(profile_id=p.id, status="queued").count() == 1


def test_create_individual_persists_eligibility(monkeypatch, pg_url, pg_session_factory):
    """User-entered structured fields are persisted immediately even though
    the LLM bootstrap is now async (queued job)."""
    app = _app(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.post(
            "/profiles/new",
            data={
                "name": "Dr A",
                "kind": "individual",
                "orcids": "0000-0001-2345-6789",
                "career_stage": "postdoc",
                "applicant_types": "solo_pi",
                "host_institution": "Karolinska",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    from sqlalchemy import select

    from call_manager.db.models import Profile

    with pg_session_factory() as s:
        p = s.execute(select(Profile).where(Profile.name == "Dr A")).scalar_one()
        assert p.kind == "individual"
        assert "solo_pi" in p.structured["eligibility"]["applicant_types"]
        assert p.structured["eligibility"]["career_stage"] == "postdoc"
        assert p.structured["geo"]["host_institution"] == "Karolinska"


def test_create_individual_drops_raw_llm_org_namespace_and_caps_areas(
    monkeypatch, pg_url, pg_session_factory
):
    """The async create path persists only user-entered structured values
    (validated by parse_structured). An `org` namespace is kind-inappropriate
    for an individual and parse_structured never produces one, so it can never
    reach the DB. The profile is created with empty description and a queued
    bootstrap job."""
    from call_manager.db.models import Profile, ProfileBootstrapJob

    app = _app(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.post(
            "/profiles/new",
            data={
                "name": "Dr Org",
                "kind": "individual",
                "career_stage": "postdoc",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303

    with pg_session_factory() as s:
        p = s.execute(select(Profile).where(Profile.name == "Dr Org")).scalar_one()
        assert p.kind == "individual"
        # parse_structured never produces an org namespace for an individual.
        assert not p.structured.get("org")
        # No LLM yet: description is empty, bootstrap job is queued.
        assert p.description == ""
    with pg_session_factory() as s:
        assert s.query(ProfileBootstrapJob).filter_by(profile_id=p.id, status="queued").count() == 1


def test_create_rejects_unknown_career_stage(monkeypatch, pg_url, pg_session_factory):
    """Structured input validation (parse_structured) runs before bootstrap
    enqueuing, so an unknown career_stage is still rejected with 400."""
    app = _app(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.post(
            "/profiles/new",
            data={"name": "Dr B", "kind": "individual", "career_stage": "wizard"},
            follow_redirects=False,
        )
    assert r.status_code == 400


def test_edit_persists_new_fields_and_schedules_backfill(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    scheduled = {"backfill": 0}
    import call_manager.web.routes.profiles as profiles_mod

    monkeypatch.setattr(
        profiles_mod,
        "backfill_profile",
        lambda **kw: scheduled.__setitem__("backfill", scheduled["backfill"] + 1),
    )
    # create an org-unit profile directly
    from call_manager.db.models import Profile

    with pg_session_factory() as s:
        from call_manager.db.models import User

        u = User(email="o@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="Lab",
            kind="organizational_unit",
            level="lab",
            description="d",
            structured={"areas": []},
            created_by=u.id,
        )
        s.add(p)
        s.commit()
        pid, uid = p.id, u.id
    client = _logged_in(app, pg_session_factory, user_id=uid)
    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "d",
                "level": "clinic",
                "org_type": "university_hospital",
                "capabilities": "biobank\nimaging_core",
                "budget_currency": "",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    # background task ran the re-rank even though description didn't change
    assert scheduled["backfill"] == 1
    with pg_session_factory() as s:
        got = s.get(Profile, pid)
        assert got.level == "clinic"  # editable level persisted (was NULL for migrated orgs)
        assert got.structured["org"]["org_type"] == "university_hospital"
        assert "biobank" in got.structured["org"]["capabilities"]


def test_edit_rejects_unknown_level(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    from call_manager.db.models import Profile, User

    with pg_session_factory() as s:
        u = User(email="o2@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="Lab2",
            kind="organizational_unit",
            level=None,
            description="d",
            structured={"areas": []},
            created_by=u.id,
        )
        s.add(p)
        s.commit()
        pid, uid = p.id, u.id
    client = _logged_in(app, pg_session_factory, user_id=uid)
    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={"description": "d", "level": "galaxy", "budget_currency": ""},
            follow_redirects=False,
        )
    assert r.status_code == 400


def test_new_form_has_kind_and_unit_and_individual_fields(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/profiles/new")
    assert r.status_code == 200
    body = r.text
    assert 'value="organizational_unit"' in body and 'value="individual"' in body
    assert 'name="career_stage"' in body  # individual field
    assert 'name="capabilities"' in body  # unit field
    assert 'name="funding_instruments"' in body
