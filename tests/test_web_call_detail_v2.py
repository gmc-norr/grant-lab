from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import (
    Call,
    CallRanking,
    CallRankingCriterion,
    Profile,
    Source,
    User,
)
from call_monitor.main import create_app


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


def test_call_detail_renders_v2_criterion_breakdown(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="V2 detail")
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        p.ranking_scheme = "v2"
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="DETAIL", url="https://x", title="DetailCall",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(CallRanking(
            call_id=c.id, profile_id=pid, scheme="v2",
            aggregate_score=82, aggregate_reason=None,
            rationale="Topic 4/5 · Eligibility: pass · Exclusions: pass",
            model="m", prompt_version="v2.0", ranked_at=datetime.now(UTC),
        ))
        s.add_all([
            CallRankingCriterion(
                call_id=c.id, profile_id=pid, scheme="v2",
                criterion="topic_fit", rating="4", rating_expected=4.1,
                rationale="strong AI match", quoted_evidence="AI funding call",
                ranked_at=datetime.now(UTC),
            ),
            CallRankingCriterion(
                call_id=c.id, profile_id=pid, scheme="v2",
                criterion="eligibility", rating="pass",
                rationale="open to research groups", quoted_evidence=None,
                ranked_at=datetime.now(UTC),
            ),
            CallRankingCriterion(
                call_id=c.id, profile_id=pid, scheme="v2",
                criterion="exclusions", rating="pass",
                rationale="no exclusions hit", quoted_evidence=None,
                ranked_at=datetime.now(UTC),
            ),
        ])
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


def test_call_detail_renders_display_score_with_setwise_offset(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """C1 regression: call detail page must show display_score (= aggregate
    + setwise_offset, clamped) in the Score line, not the bare aggregate.
    Otherwise a v2 borderline call lifted from 55 → 60 by setwise would
    still render 55 here while showing 60 in the calls index — confusing
    the operator about which value is canonical."""
    pid, _ = profile_factory(name="V2 score offset")
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        p.ranking_scheme = "v2"
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="DETAIL-OFFSET",
            url="https://x/detail-offset", title="DetailOffsetCall",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(CallRanking(
            call_id=c.id, profile_id=pid, scheme="v2",
            aggregate_score=55, setwise_offset=5,  # display = 60
            aggregate_reason=None,
            rationale="borderline lifted by setwise",
            model="m", prompt_version="v2.0", ranked_at=datetime.now(UTC),
        ))
        s.commit()
        cid = c.id

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get(f"/calls/{cid}?profile={pid}")
    assert r.status_code == 200
    # Score line shows display_score (60), not bare aggregate (55).
    assert "<strong>Score:</strong> 60" in r.text, (
        "call detail Score line must render display_score=60 "
        "(=aggregate 55 + setwise +5), not the bare aggregate 55"
    )
    # Bare aggregate must NOT appear in the Score line specifically. It
    # may appear elsewhere (e.g. inside other text), so we anchor on the
    # exact "Score: 55" form.
    assert "<strong>Score:</strong> 55" not in r.text


def test_call_detail_no_breakdown_for_v1_ranking(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """v1-scheme profiles render the v1 view; no criterion breakdown section."""
    pid, _ = profile_factory(name="V1 detail")  # defaults ranking_scheme='v1'
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="V1DETAIL", url="https://x", title="V1Call",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(CallRanking(
            call_id=c.id, profile_id=pid, scheme="v1",
            aggregate_score=70, rationale="v1 rationale text",
            model="m", prompt_version="v1", ranked_at=datetime.now(UTC),
        ))
        s.commit()
        cid = c.id

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get(f"/calls/{cid}?profile={pid}")
    assert r.status_code == 200
    assert "v1 rationale text" in r.text
    # No v2 breakdown markup — look for the heading/section identifier
    # the template uses for the v2 block (e.g. "criterion breakdown" or
    # similar). Adjust the assertion to match what your template emits.
    # If the template says "v2 criterion breakdown" or "Criterion breakdown":
    assert "criterion breakdown" not in r.text.lower()
