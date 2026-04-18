import json
import re
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import (
    Call,
    CallRanking,
    Profile,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from call_monitor.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", "admin@ki.se")
    return create_app()


def _admin_client(app, pg_session_factory):
    with TestClient(app):  # trigger lifespan to bootstrap admin
        pass
    with pg_session_factory() as s:
        u = s.execute(select(User).where(User.email == "admin@ki.se")).scalar_one()
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_admin_shows_per_scheme_queue_depth(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="P")
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="X", url="https://x", title="X",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(RankingJob(call_id=c.id, profile_id=pid, scheme="v1",
                         status=RankingJobStatus.PENDING))
        s.add(RankingJob(call_id=c.id, profile_id=pid, scheme="v2",
                         status=RankingJobStatus.PENDING))
        s.commit()

    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    # Per-scheme queue depth section
    m = re.search(r"v1[^<]*</dt>\s*<dd>\s*(\d+)\s*</dd>", r.text)
    assert m and int(m.group(1)) >= 1
    m = re.search(r"v2[^<]*</dt>\s*<dd>\s*(\d+)\s*</dd>", r.text)
    assert m and int(m.group(1)) >= 1


def test_admin_shows_per_profile_scheme_and_last_rank_time(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="HasRanking")
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        p.ranking_scheme = "v2"
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="LR", url="https://x", title="LR",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(CallRanking(
            call_id=c.id, profile_id=pid, scheme="v2",
            aggregate_score=80, rationale="r", model="m",
            prompt_version="v2.0", ranked_at=datetime.now(UTC),
        ))
        s.commit()

    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    assert "HasRanking" in r.text
    # Anchor on the actual table row: profile link cell directly followed by
    # the scheme cell. Plain "v2 in r.text" would also match the per-scheme
    # queue-depth <dl> above, masking a broken row render.
    assert re.search(
        r"<a [^>]*>HasRanking</a></td>\s*<td>v2</td>",
        r.text,
        re.DOTALL,
    )


def test_admin_per_profile_row_shows_em_dash_when_never_ranked(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    # Default ranking_scheme from profile_factory is "v1" (model default);
    # no CallRanking rows are seeded, so the outer-join's NULL branch must
    # render an em dash in the "Last ranked" column.
    profile_factory(name="NeverRanked")

    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    assert "NeverRanked" in r.text
    assert re.search(
        r"<a [^>]*>NeverRanked</a></td>\s*<td>v1</td>\s*<td>\s*—\s*</td>",
        r.text,
        re.DOTALL,
    )


def test_admin_renders_golden_set_report_when_persisted(
    monkeypatch, pg_url, pg_session_factory, profile_factory, tmp_path,
):
    """When data/golden_set_last.json exists, /admin shows a dl with both
    scheme taus formatted to 3 decimal places."""
    from call_monitor.ranking import eval as eval_mod
    monkeypatch.setattr(
        eval_mod, "_REPORT_PATH", tmp_path / "golden_set_last.json",
    )
    (tmp_path / "golden_set_last.json").write_text(json.dumps({
        "n_fixture": 50,
        "n_v1": 50, "tau_v1": 0.789,
        "n_v2": 48, "tau_v2": 0.812,
    }))

    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    assert "Golden-set Kendall" in r.text
    assert "0.789" in r.text
    assert "0.812" in r.text
    assert "n=50" in r.text
    assert "n=48" in r.text


def test_admin_renders_placeholder_when_no_report_persisted(
    monkeypatch, pg_url, pg_session_factory, profile_factory, tmp_path,
):
    """If no report has been persisted yet, show the placeholder text."""
    from call_monitor.ranking import eval as eval_mod
    monkeypatch.setattr(
        eval_mod, "_REPORT_PATH", tmp_path / "never-existed.json",
    )

    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    assert "golden eval not yet run" in r.text


def test_admin_tolerates_corrupt_golden_set_report(
    monkeypatch, pg_url, pg_session_factory, profile_factory, tmp_path,
):
    """If data/golden_set_last.json is corrupt, /admin still renders 200
    with the placeholder. /admin must never 500 on a bad sidecar file."""
    from call_monitor.ranking import eval as eval_mod
    bad = tmp_path / "golden_set_last.json"
    bad.write_text("{not json")
    monkeypatch.setattr(eval_mod, "_REPORT_PATH", bad)

    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    assert "golden eval not yet run" in r.text
