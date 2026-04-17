from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.email import InMemoryEmailSender
from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import CallRanking, RankingJob, RankingJobStatus, User
from call_monitor.ingestion.alerts import ConsecutiveFailureTracker
from call_monitor.ingestion.orchestrator import run_ingestion
from call_monitor.main import create_app
from call_monitor.ranking.rank_pair import RankResult
from call_monitor.ranking.worker import drain_queue
from call_monitor.sources.types import NormalizedCall


class _FakeAdapter:
    slug = "vinnova"

    def fetch_new_calls(self, since):
        yield NormalizedCall(
            external_id="E2E-A",
            url="https://example.org/a",
            title="Highly relevant",
            published_at=datetime(2026, 4, 10, tzinfo=UTC),
        )
        yield NormalizedCall(
            external_id="E2E-B",
            url="https://example.org/b",
            title="Marginally relevant",
            published_at=datetime(2026, 4, 11, tzinfo=UTC),
        )


class _ScoreByExternalId:
    def __call__(self, *, client, profile, call):
        # Map external_id → score so we can predict the ordering.
        score = {"E2E-A": 88, "E2E-B": 22}.get(call.external_id, 50)
        return RankResult(
            score=score,
            rationale=f"Static {score}",
            model="gemma4:e4b-q8",
            prompt_version="v1",
            raw_response={"score": score},
        )


def test_end_to_end_ingest_rank_browse(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    profile_id, _ = profile_factory(name="Test profile")

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()

    # 1. Ingest — auto-enqueues ranking jobs
    sender = InMemoryEmailSender()
    alerter = ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email=None)
    result = run_ingestion(
        session_factory=pg_session_factory, adapter=_FakeAdapter(), alerter=alerter
    )
    assert result.error is None
    with pg_session_factory() as s:
        jobs = s.execute(select(RankingJob)).scalars().all()
    assert len(jobs) == 2
    assert all(j.status == RankingJobStatus.PENDING for j in jobs)

    # 2. Drain — workers convert pending jobs into call_rankings rows
    drain_queue(
        session_factory=pg_session_factory,
        client=None,
        ranker=_ScoreByExternalId(),
        batch_size=10,
    )
    with pg_session_factory() as s:
        rankings = s.execute(select(CallRanking)).scalars().all()
        jobs = s.execute(select(RankingJob)).scalars().all()
    assert len(rankings) == 2
    assert all(j.status == RankingJobStatus.DONE for j in jobs)

    # 3. Browse as logged-in user — score column + score-desc order
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.get(f"/?profile={profile_id}")
        assert r.status_code == 200
        assert "Highly relevant" in r.text
        assert "Marginally relevant" in r.text
        assert r.text.index("Highly relevant") < r.text.index("Marginally relevant")
        assert "88" in r.text
        assert "22" in r.text
