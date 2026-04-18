import json
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from call_monitor.db.models import (
    Call,
    CallRanking,
    CallRankingCriterion,
    Profile,
    ProfileKind,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from call_monitor.ranking.ollama_client import OllamaClient
from call_monitor.ranking.worker import drain_queue


def _ollama_v2_canned():
    body = {
        "model": "gemma4:e4b-it-q4_K_M",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": json.dumps({
                "topic_fit": {"rating": 5, "rationale": "perfect AI match",
                              "quoted_evidence": "AI for medical imaging"},
                "eligibility": {"rating": "pass", "rationale": "research groups eligible",
                                "quoted_evidence": None},
                "exclusions": {"rating": "pass", "rationale": "no exclusions",
                               "quoted_evidence": None},
            })},
            "logprobs": {"content": [
                {"token": "5", "logprob": -0.05, "top_logprobs": [
                    {"token": "5", "logprob": -0.05},
                    {"token": "4", "logprob": -3.0},
                ]},
            ]},
        }],
    }
    def handler(request):
        return httpx.Response(200, json=body)
    return OllamaClient(
        base_url="http://x", model="gemma4:e4b-it-q4_K_M",
        transport=httpx.MockTransport(handler),
    )


def test_e2e_v2_pipeline(pg_session_factory):
    """Enqueue a v2 job → drain → verify CallRanking + 4 criteria rows
    (timeline pass + topic_fit + eligibility + exclusions) + score 100."""
    with pg_session_factory() as s:
        u = User(email="e2e@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="E2E", kind=ProfileKind.GROUP, description="",
            structured={
                "areas": ["AI for medical imaging"],
                "must_include": [], "hard_exclusions": [],
                "budget_min": None, "budget_max": None,
                "trl_min": None, "trl_max": None,
            },
            created_by=u.id, ranking_scheme="v2",
        )
        s.add(p)
        s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="E2E", url="https://x",
            title="AI for medical imaging — call",
            summary="Open call for AI-driven medical imaging research.",
            deadline=datetime(2099, 12, 31, tzinfo=UTC),
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(RankingJob(
            call_id=c.id, profile_id=p.id, scheme="v2",
            status=RankingJobStatus.PENDING,
        ))
        s.commit()

    drain_queue(session_factory=pg_session_factory, client=_ollama_v2_canned(), batch_size=10)

    with pg_session_factory() as s:
        ranking = s.execute(
            select(CallRanking).where(CallRanking.scheme == "v2")
        ).scalar_one()
        criteria = s.execute(select(CallRankingCriterion)).scalars().all()

    # Topic 5 (logprob expected ~5.0) × 20 = 100, no penalty.
    assert ranking.aggregate_score >= 99
    assert ranking.aggregate_reason is None
    assert ranking.scheme == "v2"
    assert ranking.prompt_version == "v2.0"

    by_name = {c.criterion: c for c in criteria}
    assert set(by_name.keys()) >= {"topic_fit", "eligibility", "exclusions", "timeline"}
    assert by_name["topic_fit"].rating == "5"
    assert by_name["topic_fit"].quoted_evidence == "AI for medical imaging"
    assert by_name["eligibility"].rating == "pass"
    assert by_name["timeline"].rating == "pass"
