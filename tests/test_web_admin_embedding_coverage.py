"""Embedding coverage badge on /admin (Plan E follow-up).

Operationally, the embedding pre-gate degrades silently to UNKNOWN when
embeddings are missing or stale (model mismatch). The /admin page now
surfaces calls/profiles coverage so this is visible without log-diving.
"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Call, Profile, ProfileKind, Source, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _logged_in(app, pg_session_factory, *, is_admin: bool):
    with pg_session_factory() as s:
        u = User(email="root@ki.se" if is_admin else "alice@ki.se", is_admin=is_admin)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_admin_shows_embedding_coverage_counts(monkeypatch, pg_url, pg_session_factory):
    """Seed 3 calls (1 current-model, 1 old-model, 1 NULL embedding) and
    2 profiles (1 current-model, 1 NULL). Expect:
      Calls: 1/3 embedded (2 stale, model bge-m3)
      Profiles: 1/2 embedded (1 stale, model bge-m3)
    """
    app = _setup(monkeypatch, pg_url)
    vec_zero = [0.0] * 1024  # placeholder; only the column being non-NULL matters
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        # Call 1: current model
        s.add(
            Call(
                source_id=src.id,
                external_id="c1",
                url="https://x/1",
                title="C1",
                embedding=vec_zero,
                embedding_model="bge-m3",
                published_at=datetime.now(UTC),
            )
        )
        # Call 2: old model
        s.add(
            Call(
                source_id=src.id,
                external_id="c2",
                url="https://x/2",
                title="C2",
                embedding=vec_zero,
                embedding_model="bge-old",
                published_at=datetime.now(UTC),
            )
        )
        # Call 3: NULL embedding
        s.add(
            Call(
                source_id=src.id,
                external_id="c3",
                url="https://x/3",
                title="C3",
                embedding=None,
                embedding_model=None,
                published_at=datetime.now(UTC),
            )
        )

        owner = User(email="owner@ki.se")
        s.add(owner)
        s.flush()
        # Profile 1: current model. Non-empty description so it counts toward
        # coverage (blank-description profiles are excluded by the tick filter
        # — see _embedding_coverage_context for the rationale).
        s.add(
            Profile(
                name="P1",
                kind=ProfileKind.GROUP,
                description="P1 desc",
                structured={},
                created_by=owner.id,
                embedding=vec_zero,
                embedding_model="bge-m3",
            )
        )
        # Profile 2: NULL embedding (also non-empty description so it appears
        # as legitimately stale rather than getting filtered out).
        s.add(
            Profile(
                name="P2",
                kind=ProfileKind.GROUP,
                description="P2 desc",
                structured={},
                created_by=owner.id,
                embedding=None,
                embedding_model=None,
            )
        )
        s.commit()

    client = _logged_in(app, pg_session_factory, is_admin=True)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    # Calls section: 1 of 3 embedded, 2 stale
    assert "1/3 embedded" in r.text
    assert "(2 stale, model" in r.text
    # Profiles section: 1 of 2 embedded, 1 stale
    assert "1/2 embedded" in r.text
    assert "(1 stale, model" in r.text
    # Current model name appears
    assert "bge-m3" in r.text


def test_admin_embedding_coverage_excludes_blank_description_profiles(
    monkeypatch, pg_url, pg_session_factory
):
    """Regression: blank-description profiles can never be embedded because
    the refresh-missing tick filters them out by design (Plan D Task 9 —
    they would otherwise infinite-loop the tick: select NULL → no-op clear
    → still NULL → re-select). The /admin coverage badge must mirror that
    filter so operators see a stable 'fully covered' state instead of
    permanent false-stale."""
    app = _setup(monkeypatch, pg_url)
    vec_zero = [0.0] * 1024
    with pg_session_factory() as s:
        owner = User(email="owner-blank@ki.se")
        s.add(owner)
        s.flush()
        # Profile A: non-empty description, embedded current model → counts
        s.add(
            Profile(
                name="PA",
                kind=ProfileKind.GROUP,
                description="actual desc",
                structured={},
                created_by=owner.id,
                embedding=vec_zero,
                embedding_model="bge-m3",
            )
        )
        # Profile B: blank description → MUST be excluded from both stale
        # and total (the tick will never embed it, so it's not "stale").
        s.add(
            Profile(
                name="PB",
                kind=ProfileKind.GROUP,
                description="",
                structured={},
                created_by=owner.id,
                embedding=None,
                embedding_model=None,
            )
        )
        # Profile C: whitespace-only description → also excluded (the tick
        # uses func.length(func.trim(...)) > 0 which strips whitespace).
        s.add(
            Profile(
                name="PC",
                kind=ProfileKind.GROUP,
                description="   ",
                structured={},
                created_by=owner.id,
                embedding=None,
                embedding_model=None,
            )
        )
        s.commit()

    client = _logged_in(app, pg_session_factory, is_admin=True)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    # 1 of 1 embedded: blank/whitespace profiles excluded from total
    assert "1/1 embedded" in r.text
    # And NOT 1/2 or 1/3 — guard against re-introduction of the bug
    assert "1/2 embedded" not in r.text
    assert "1/3 embedded" not in r.text
    # 0 stale (the only counted profile is current-model)
    assert "(0 stale, model" in r.text
