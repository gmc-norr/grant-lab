from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Call, Source, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _logged_in(app, pg_session_factory, email="alice@ki.se"):
    with pg_session_factory() as s:
        u = User(email=email)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def _seed_calls(pg_session_factory):
    with pg_session_factory() as s:
        eu = s.execute(select(Source).where(Source.slug == "eu_portal")).scalar_one()
        vn = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add_all(
            [
                # EU one: published April 1, deadline July 1 (latest deadline)
                Call(
                    source_id=eu.id,
                    external_id="E-1",
                    url="u1",
                    title="EU one",
                    published_at=datetime(2026, 4, 1, tzinfo=UTC),
                    deadline=datetime(2026, 7, 1, tzinfo=UTC),
                ),
                # Vin one: published April 5 (newest), deadline May 15 (earliest)
                Call(
                    source_id=vn.id,
                    external_id="V-1",
                    url="u2",
                    title="Vin one",
                    published_at=datetime(2026, 4, 5, tzinfo=UTC),
                    deadline=datetime(2026, 5, 15, tzinfo=UTC),
                ),
                # EU two: published March 1 (oldest), deadline June 10
                Call(
                    source_id=eu.id,
                    external_id="E-2",
                    url="u3",
                    title="EU two",
                    published_at=datetime(2026, 3, 1, tzinfo=UTC),
                    deadline=datetime(2026, 6, 10, tzinfo=UTC),
                ),
            ]
        )
        s.commit()


def test_root_requires_auth(monkeypatch, pg_url):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 401


def test_root_lists_calls_newest_first(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/")
    assert r.status_code == 200
    body = r.text
    # Newest published_at first
    assert body.index("Vin one") < body.index("EU one") < body.index("EU two")


def test_root_filter_by_source(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?source=vinnova")
    assert r.status_code == 200
    assert "Vin one" in r.text
    assert "EU one" not in r.text


def test_root_unknown_source_degrades_to_all(monkeypatch, pg_url, pg_session_factory):
    """A stale/unknown ?source= slug shows all calls (selection cleared),
    not a confusing empty feed."""
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?source=does-not-exist")
    assert r.status_code == 200
    assert "Vin one" in r.text and "EU one" in r.text


def test_root_summary_counts_full_dataset_despite_paging(monkeypatch, pg_url, pg_session_factory):
    """With limit=1 only one row renders, but the 'Open calls' badge must
    reflect all 3 seeded calls (summary is computed over the whole dataset,
    not the visible page), and a Next link is offered."""
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?limit=1")
    assert r.status_code == 200
    # Only one call row on the page...
    assert r.text.count('class="call-row"') == 1
    # ...but the summary badge reports the full total of 3.
    assert ">3</div>" in r.text
    # ...and pagination offers a next page.
    assert "page=2" in r.text


def test_root_sort_by_deadline(monkeypatch, pg_url, pg_session_factory):
    """sort=deadline orders soonest-deadline first (irrespective of
    publication date). Vin one (May 15) → EU two (June 10) → EU one (July 1)."""
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?sort=deadline")
    assert r.status_code == 200
    body = r.text
    assert body.index("Vin one") < body.index("EU two") < body.index("EU one")


def test_root_treats_empty_profile_as_unfiltered(monkeypatch, pg_url, pg_session_factory):
    """Browsers submit `?profile=` (empty string) when the profile <select>
    is set to its 'all' option. The route must treat that the same as the
    param being absent — not 422 with int-parsing error."""
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?profile=")
    assert r.status_code == 200


def test_call_detail_treats_empty_profile_as_unfiltered(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with pg_session_factory() as s:
        cid = s.execute(select(Call.id).where(Call.external_id == "V-1")).scalar_one()
    with client:
        r = client.get(f"/calls/{cid}?profile=")
    assert r.status_code == 200


def test_root_rejects_invalid_sort(monkeypatch, pg_url, pg_session_factory):
    """FastAPI Literal type rejects unknown sort values with 422."""
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?sort=junk")
    assert r.status_code == 422


def test_call_detail_shows_metadata(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with pg_session_factory() as s:
        cid = s.execute(select(Call.id).where(Call.external_id == "V-1")).scalar_one()
    with client:
        r = client.get(f"/calls/{cid}")
    assert r.status_code == 200
    assert "Vin one" in r.text


def test_call_detail_404_for_unknown_id(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/calls/99999")
    assert r.status_code == 404
