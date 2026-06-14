from functools import partial

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Call, CallRanking, Profile, RankingJob, Source, User
from call_manager.main import create_app
from call_manager.profiles.seed import DraftProfile
from call_manager.ranking.criteria import GateResult, Gates, GateStatus
from call_manager.ranking.prompt import Parsed
from call_manager.ranking.rank_pair import RankResult
from call_manager.ranking.worker import drain_queue as _drain_queue

# drain_queue now requires embedding_threshold (no module-level default).
drain_queue = partial(_drain_queue, embedding_threshold=0.30)


def _stub_rank_pair_returning(score: int = 80):
    parsed = Parsed(
        topic_fit_rating=4,
        topic_fit_rationale="e2e",
        topic_fit_quoted=None,
        eligibility_rating="pass",
        eligibility_rationale="ok",
        eligibility_quoted=None,
        exclusions_rating="pass",
        exclusions_rationale="ok",
        exclusions_quoted=None,
    )
    stub_result = RankResult(
        score=score,
        reason=None,
        gates=Gates(
            timeline=GateResult(GateStatus.PASS),
            budget=GateResult(GateStatus.PASS),
            trl=GateResult(GateStatus.PASS),
            embedding=GateResult(GateStatus.PASS),
        ),
        parsed=parsed,
        must_include_override=False,
        model="gemma4:e4b-q8",
        prompt_version="v2.0",
        raw_response={},
        embed_similarity=None,
    )

    def _stub(**_kwargs):
        return stub_result

    return _stub


def test_e2e_create_profile_then_rank_then_browse(monkeypatch, pg_url, pg_session_factory):
    """End-to-end of the async first-run flow: create a profile (no inline
    bootstrap), let the bootstrap WORKER draft + embed + enqueue backfill, drain
    the ranking queue, then browse the ranked feed."""
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")

    # Seed a Call (published recently) so backfill finds it
    from datetime import UTC, datetime

    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add(
            Call(
                source_id=src.id,
                external_id="E2E-1",
                url="https://x",
                title="E2E call",
                published_at=datetime.now(UTC),
            )
        )
        s.commit()

    app = create_app()
    # Login as a user
    with pg_session_factory() as s:
        u = User(status="active", email="e2e@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))

    # The worker's bootstrap_profile is mocked; llm_succeeded/pubmed_status
    # default to success on DraftProfile, so the worker takes the success path.
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

    with client:
        # 1. Create the profile — async: no inline bootstrap, a queued job is enqueued.
        r = client.post(
            "/profiles/new",
            data={
                "name": "E2E Profile",
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
            profile = s.execute(select(Profile).where(Profile.name == "E2E Profile")).scalar_one()
        profile_id = profile.id

        # 2. Run the bootstrap worker → writes the description, embeds (mocked),
        #    and enqueues the ranking backfill for the seeded call.
        from call_manager.profiles import bootstrap_worker as bw

        monkeypatch.setattr(bw, "bootstrap_profile", lambda **_: fake_draft)
        monkeypatch.setattr(bw, "refresh_profile_embedding", lambda *a, **k: None)
        bw.run_profile_bootstrap_tick(
            session_factory=pg_session_factory,
            pubmed=object(),
            ollama=object(),
            embed_client=object(),
        )

        with pg_session_factory() as s:
            profile = s.get(Profile, profile_id)
            assert profile.description == "E2E test profile"
            jobs = (
                s.execute(select(RankingJob).where(RankingJob.profile_id == profile_id))
                .scalars()
                .all()
            )
        assert len(jobs) == 1

        # 3. Drain the ranking queue with a stub rank_pair.
        from call_manager.ranking import worker as worker_mod

        monkeypatch.setattr(worker_mod, "rank_pair", _stub_rank_pair_returning(score=80))
        drain_queue(
            session_factory=pg_session_factory,
            client=None,
            batch_size=10,
        )
        with pg_session_factory() as s:
            rankings = (
                s.execute(select(CallRanking).where(CallRanking.profile_id == profile_id))
                .scalars()
                .all()
            )
        assert len(rankings) == 1
        assert rankings[0].aggregate_score == 80

        # 4. Browse the dashboard filtered by this profile.
        r = client.get(f"/?profile={profile_id}")
        assert r.status_code == 200
        assert "E2E call" in r.text
        assert "80" in r.text
