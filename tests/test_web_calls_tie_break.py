from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import (
    Call,
    CallRanking,
    Profile,
    ProfileKind,
    Source,
    User,
)
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


# NOTE: the dashboard ranked-list cosine-tiebreak test (`GET /?profile=`) was
# removed in Plan 3 Task 9 (Jinja calls_index retired); the same tiebreak is now
# proven on JSON `GET /api/calls` in tests/test_api_calls_feed_behavior.py
# (test_ranked_ties_broken_by_cosine). The call-detail best-profile tiebreak
# below uses the still-server-rendered `/calls/{id}` page and stays.


def test_call_detail_best_profile_breaks_ties_by_cosine(monkeypatch, pg_url, pg_session_factory):
    """Two profiles both score a call 80; the one with the higher cosine is
    chosen as best_profile_id."""
    app = _app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="root@ki.se", is_admin=True)
        s.add(u)
        s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id="c1",
            url="https://x/1",
            title="C1",
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        low = Profile(
            name="Low",
            kind=ProfileKind.ORGANIZATIONAL_UNIT,
            description="d",
            structured={},
            created_by=u.id,
        )
        high = Profile(
            name="High",
            kind=ProfileKind.ORGANIZATIONAL_UNIT,
            description="d",
            structured={},
            created_by=u.id,
        )
        s.add_all([low, high])
        s.flush()
        for prof, cos in ((low, 0.30), (high, 0.95)):
            s.add(
                CallRanking(
                    call_id=c.id,
                    profile_id=prof.id,
                    aggregate_score=80,
                    aggregate_reason=None,
                    rationale="r",
                    model="m",
                    prompt_version="v4.1",
                    raw_response={},
                    embed_similarity=cos,
                    ranked_at=datetime.now(UTC),
                )
            )
        s.commit()
        cid, high_id, uid = c.id, high.id, u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.get(f"/calls/{cid}")
    assert r.status_code == 200
    # best_profile_id is rendered as an integer in the workspace link;
    # the profile name is never output to the page.
    assert f"profile_id={high_id}" in r.text
