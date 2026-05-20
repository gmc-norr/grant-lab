from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import (
    Call,
    CallRanking,
    CallRankingCriterion,
    Source,
    User,
)
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _logged_in(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="cd@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_call_detail_renders_criterion_breakdown(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="Detail")
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id="DETAIL",
            url="https://x",
            title="DetailCall",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(
            CallRanking(
                call_id=c.id,
                profile_id=pid,
                aggregate_score=82,
                aggregate_reason=None,
                rationale="Topic 4/5 · Eligibility: pass · Exclusions: pass",
                model="m",
                prompt_version="v2.0",
                ranked_at=datetime.now(UTC),
            )
        )
        s.add_all(
            [
                CallRankingCriterion(
                    call_id=c.id,
                    profile_id=pid,
                    criterion="topic_fit",
                    rating="4",
                    rationale="strong AI match",
                    quoted_evidence="AI funding call",
                    ranked_at=datetime.now(UTC),
                ),
                CallRankingCriterion(
                    call_id=c.id,
                    profile_id=pid,
                    criterion="eligibility",
                    rating="pass",
                    rationale="open to research groups",
                    quoted_evidence=None,
                    ranked_at=datetime.now(UTC),
                ),
                CallRankingCriterion(
                    call_id=c.id,
                    profile_id=pid,
                    criterion="exclusions",
                    rating="pass",
                    rationale="no exclusions hit",
                    quoted_evidence=None,
                    ranked_at=datetime.now(UTC),
                ),
            ]
        )
        s.commit()
        cid = c.id

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get(f"/calls/{cid}?profile={pid}")
    assert r.status_code == 200
    # Aggregate is still primary
    assert "82" in r.text
    # Each criterion rating is rendered (look for rationales we seeded)
    assert "strong AI match" in r.text
    assert "AI funding call" in r.text  # quoted evidence


def test_call_detail_renders_embed_similarity_when_present(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """When the ranking has embed_similarity=0.55, the call detail page
    surfaces it as 'Cosine similarity: 0.550' so the operator can spot
    LLM/embedding disagreements (the documented miss-detection signal)."""
    pid, _ = profile_factory(name="Cosine detail")
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id="COSDETAIL",
            url="https://x/cosdetail",
            title="CosineDetailCall",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(
            CallRanking(
                call_id=c.id,
                profile_id=pid,
                aggregate_score=82,
                embed_similarity=0.55,
                rationale="Topic 4/5 · Eligibility: pass · Exclusions: pass",
                model="m",
                prompt_version="v2.0",
                ranked_at=datetime.now(UTC),
            )
        )
        s.commit()
        cid = c.id

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get(f"/calls/{cid}?profile={pid}")
    assert r.status_code == 200
    assert "Cosine similarity" in r.text
    # 3 dp formatting: 0.55 → "0.550"
    assert "0.550" in r.text


def test_call_detail_omits_cosine_line_when_embed_similarity_is_none(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """A ranking with embed_similarity=None must not render the
    'Cosine similarity:' line — avoids confusing 'None' or em-dash
    when the value genuinely wasn't computed."""
    pid, _ = profile_factory(name="No cosine")
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id="NOCOS",
            url="https://x/nocos",
            title="NoCosineCall",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(
            CallRanking(
                call_id=c.id,
                profile_id=pid,
                aggregate_score=70,  # embed_similarity intentionally omitted
                rationale="ranking without cosine",
                model="m",
                prompt_version="v2.0",
                ranked_at=datetime.now(UTC),
            )
        )
        s.commit()
        cid = c.id

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get(f"/calls/{cid}?profile={pid}")
    assert r.status_code == 200
    assert "Cosine similarity" not in r.text
