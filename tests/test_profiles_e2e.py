from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import Call, CallRanking, Profile, RankingJob, Source, User
from call_monitor.main import create_app
from call_monitor.profiles.seed import DraftProfile
from call_monitor.ranking.rank_pair import RankResult
from call_monitor.ranking.worker import drain_queue


def _static_ranker(score: int = 80):
    def _rank(*, client, profile, call):
        return RankResult(
            score=score,
            rationale="e2e",
            model="gemma4:e4b-q8",
            prompt_version="v1",
            raw_response={"score": score},
        )
    return _rank


def test_e2e_create_profile_then_rank_then_browse(
    monkeypatch, pg_url, pg_session_factory
):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")

    # Seed a Call (published recently) so backfill finds it
    from datetime import UTC, datetime
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add(Call(
            source_id=src.id, external_id="E2E-1", url="https://x", title="E2E call",
            published_at=datetime.now(UTC),
        ))
        s.commit()

    app = create_app()
    # Login as a user
    with pg_session_factory() as s:
        u = User(email="e2e@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))

    # 1. Create a profile (bootstrap mocked)
    fake_draft = DraftProfile(
        description="E2E test profile",
        structured={
            "areas": ["ai"],
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
                "name": "E2E Profile",
                "kind": "group",
                "orcids": "",
                "names_affiliations": "",
                "pasted_grants": "",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

        with pg_session_factory() as s:
            profile = s.execute(select(Profile).where(Profile.name == "E2E Profile")).scalar_one()
        profile_id = profile.id

        # 2. Save the edit → backfill triggers
        r = client.post(
            f"/profiles/{profile_id}/edit",
            data={
                "description": "edited desc",
                "areas": "ai",
                "budget_min": "",
                "budget_max": "",
                "trl_min": "",
                "trl_max": "",
                "must_include": "",
                "hard_exclusions": "",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

        # 3. Ranking jobs were enqueued for our seeded call
        with pg_session_factory() as s:
            jobs = (
                s.execute(select(RankingJob).where(RankingJob.profile_id == profile_id))
                .scalars()
                .all()
            )
        assert len(jobs) == 1

        # 4. Drain the queue with a static ranker
        drain_queue(
            session_factory=pg_session_factory,
            client=None,
            ranker=_static_ranker(score=80),
            batch_size=10,
        )
        with pg_session_factory() as s:
            rankings = s.execute(
                select(CallRanking).where(CallRanking.profile_id == profile_id)
            ).scalars().all()
        assert len(rankings) == 1
        assert rankings[0].score == 80

        # 5. Browse the dashboard filtered by this profile
        r = client.get(f"/?profile={profile_id}")
        assert r.status_code == 200
        assert "E2E call" in r.text
        assert "80" in r.text
