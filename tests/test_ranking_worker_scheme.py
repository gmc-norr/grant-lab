from datetime import UTC, datetime

from sqlalchemy import select

from call_monitor.db.models import (
    Call,
    CallRanking,
    Profile,
    ProfileKind,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from call_monitor.ranking.rank_pair import RankResult
from call_monitor.ranking.worker import drain_queue


def _seed_pair(s, *, profile_scheme="v1"):
    u = User(email=f"u-{profile_scheme}@ki.se")
    s.add(u)
    s.flush()
    p = Profile(
        name=f"P-{profile_scheme}", kind=ProfileKind.GROUP, description="",
        structured={}, created_by=u.id, ranking_scheme=profile_scheme,
    )
    s.add(p)
    s.flush()
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id, external_id=f"E-{profile_scheme}", url="https://x", title="T",
        published_at=datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    return c.id, p.id


def _enqueue(s, cid, pid, scheme):
    s.add(RankingJob(
        call_id=cid, profile_id=pid, scheme=scheme,
        status=RankingJobStatus.PENDING,
    ))


def test_worker_dispatches_v1_job_to_v1_ranker(pg_session_factory):
    """v1 jobs run rank_pair_v1 and produce a CallRanking with scheme='v1'."""
    with pg_session_factory() as s:
        cid, pid = _seed_pair(s, profile_scheme="v1")
        _enqueue(s, cid, pid, "v1")
        s.commit()

    def _fake_ranker(*, client, profile, call):
        return RankResult(
            score=77, rationale="ok", model="m", prompt_version="v1",
            raw_response={},
        )

    drain_queue(
        session_factory=pg_session_factory, client=None,
        ranker=_fake_ranker, batch_size=10,
    )

    with pg_session_factory() as s:
        job = s.execute(select(RankingJob)).scalar_one()
        ranking = s.execute(select(CallRanking)).scalar_one()
    assert job.status == RankingJobStatus.DONE
    assert ranking.scheme == "v1"
    assert ranking.aggregate_score == 77


def test_worker_runs_v2_job_and_writes_call_ranking(pg_session_factory):
    """v2 jobs now execute rank_pair_v2 (Plan B). A v2 job with a far-future
    deadline produces a DONE job and a CallRanking row with scheme='v2'."""
    import json

    import httpx

    from call_monitor.ranking.ollama_client import OllamaClient

    with pg_session_factory() as s:
        cid, pid = _seed_pair(s, profile_scheme="v2")
        _enqueue(s, cid, pid, "v2")
        s.commit()

    body = {
        "model": "gemma4:e4b-it-q4_K_M",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": json.dumps({
                "topic_fit": {"rating": 3, "rationale": "ok", "quoted_evidence": None},
                "eligibility": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
                "exclusions": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
            })},
            "logprobs": None,
        }],
    }
    client = OllamaClient(
        base_url="http://x", model="gemma4:e4b-it-q4_K_M",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)),
    )

    drain_queue(
        session_factory=pg_session_factory, client=client,
        ranker=lambda **kw: (_ for _ in ()).throw(AssertionError("v1 ranker called for v2 job")),
        batch_size=10,
    )

    with pg_session_factory() as s:
        job = s.execute(select(RankingJob)).scalar_one()
        rankings = s.execute(select(CallRanking)).scalars().all()
    assert job.status == RankingJobStatus.DONE
    assert len(rankings) == 1
    assert rankings[0].scheme == "v2"


def test_worker_processes_v1_and_v2_jobs_in_same_batch(pg_session_factory):
    """v1 and v2 jobs in the same batch are both processed and produce DONE rows."""
    import json

    import httpx

    from call_monitor.ranking.ollama_client import OllamaClient

    with pg_session_factory() as s:
        cid_a, pid_a = _seed_pair(s, profile_scheme="v1")
        cid_b, pid_b = _seed_pair(s, profile_scheme="v2")
        _enqueue(s, cid_a, pid_a, "v1")
        _enqueue(s, cid_b, pid_b, "v2")
        s.commit()

    body = {
        "model": "gemma4:e4b-it-q4_K_M",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": json.dumps({
                "topic_fit": {"rating": 3, "rationale": "ok", "quoted_evidence": None},
                "eligibility": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
                "exclusions": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
            })},
            "logprobs": None,
        }],
    }
    client = OllamaClient(
        base_url="http://x", model="gemma4:e4b-it-q4_K_M",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)),
    )

    drain_queue(
        session_factory=pg_session_factory, client=client,
        ranker=lambda *, client, profile, call: RankResult(
            score=50, rationale="r", model="m", prompt_version="v1", raw_response={},
        ),
        batch_size=10,
    )

    with pg_session_factory() as s:
        jobs = s.execute(select(RankingJob)).scalars().all()
        rows = {(j.profile_id, j.scheme): j.status for j in jobs}
    assert rows[(pid_a, "v1")] == RankingJobStatus.DONE
    assert rows[(pid_b, "v2")] == RankingJobStatus.DONE
