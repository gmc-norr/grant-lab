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


def _seed(s, *, scheme="v2"):
    u = User(email=f"u-{scheme}@ki.se")
    s.add(u)
    s.flush()
    p = Profile(
        name=f"P-{scheme}", kind=ProfileKind.GROUP, description="",
        structured={"areas": ["AI"], "hard_exclusions": []},
        created_by=u.id, ranking_scheme=scheme,
    )
    s.add(p)
    s.flush()
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id, external_id=f"E-{scheme}", url="https://x", title="AI call",
        summary="Open AI funding call.", deadline=datetime(2099, 12, 31, tzinfo=UTC),
        published_at=datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    s.add(RankingJob(
        call_id=c.id, profile_id=p.id, scheme=scheme,
        status=RankingJobStatus.PENDING,
    ))
    s.flush()
    return c.id, p.id


def _ollama_v2_clean():
    body = {
        "model": "gemma4:e4b-it-q4_K_M",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": json.dumps({
                "topic_fit": {"rating": 4, "rationale": "AI match", "quoted_evidence": "AI"},
                "eligibility": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
                "exclusions": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
            })},
            "logprobs": {"content": [
                {"token": "4", "logprob": -0.1, "top_logprobs": [
                    {"token": "4", "logprob": -0.1},
                    {"token": "3", "logprob": -2.0},
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


def test_worker_v2_writes_call_ranking_with_aggregate_and_reason(pg_session_factory):
    with pg_session_factory() as s:
        cid, pid = _seed(s, scheme="v2")
        s.commit()

    client = _ollama_v2_clean()
    drain_queue(session_factory=pg_session_factory, client=client, batch_size=10)

    with pg_session_factory() as s:
        r = s.execute(select(CallRanking).where(CallRanking.scheme == "v2")).scalar_one()
    # Topic 4 (logprob expected ≈ 4.0 with strong probability) × 20 ≈ 80
    assert 75 <= r.aggregate_score <= 85
    assert r.aggregate_reason is None
    assert r.scheme == "v2"
    assert r.prompt_version == "v2.0"


def test_worker_v2_writes_sparse_criterion_rows_clean_path(pg_session_factory):
    """Clean pass — all 3 LLM criteria + 3 gate criteria get rows."""
    with pg_session_factory() as s:
        cid, pid = _seed(s, scheme="v2")
        s.commit()

    drain_queue(session_factory=pg_session_factory, client=_ollama_v2_clean(), batch_size=10)

    with pg_session_factory() as s:
        criteria = s.execute(select(CallRankingCriterion)).scalars().all()
    by_name = {c.criterion: c for c in criteria}
    # All 3 LLM criteria + 3 gates that ran are written.
    assert "topic_fit" in by_name
    assert by_name["topic_fit"].rating == "4"
    assert by_name["topic_fit"].rating_expected is not None
    assert "eligibility" in by_name
    assert by_name["eligibility"].rating == "pass"
    assert "exclusions" in by_name
    assert by_name["exclusions"].rating == "pass"
    assert "timeline" in by_name
    assert by_name["timeline"].rating == "pass"


def test_worker_v2_short_circuit_writes_only_failing_gate_row(pg_session_factory):
    """If timeline fails AND no must_include match, only timeline criterion row
    is written. No LLM row."""
    with pg_session_factory() as s:
        u = User(email="sc@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="SC", kind=ProfileKind.GROUP, description="",
            structured={"areas": [], "hard_exclusions": []},
            created_by=u.id, ranking_scheme="v2",
        )
        s.add(p)
        s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        # Deadline tomorrow → timeline FAIL.
        from datetime import timedelta
        c = Call(
            source_id=src.id, external_id="SC", url="https://x", title="T",
            deadline=datetime.now(UTC) + timedelta(days=1),
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(RankingJob(
            call_id=c.id, profile_id=p.id, scheme="v2",
            status=RankingJobStatus.PENDING,
        ))
        s.commit()

    # Client that would fail if called.
    def boom(request):
        raise AssertionError("LLM called for gate-failed pair")
    client = OllamaClient(
        base_url="http://x", model="m",
        transport=httpx.MockTransport(boom),
    )
    drain_queue(session_factory=pg_session_factory, client=client, batch_size=10)

    with pg_session_factory() as s:
        r = s.execute(select(CallRanking)).scalar_one()
        criteria = s.execute(select(CallRankingCriterion)).scalars().all()
    assert r.aggregate_score == 0
    assert r.aggregate_reason == "timeline"
    by_name = {c.criterion: c for c in criteria}
    assert set(by_name.keys()) == {"timeline"}
    assert by_name["timeline"].rating == "fail"


def test_worker_v2_must_include_with_failing_timeline_persists_llm_score(
    pg_session_factory,
):
    """Regression for Codex H1: a must_include match with a failing timeline
    gate must (a) get an LLM call, (b) persist a non-zero CallRanking score
    based on the LLM topic_fit, NOT zero from the timeline-fail short-circuit,
    and (c) have all four criterion rows (timeline=fail + LLM topic/eligibility/
    exclusions) — NOT just the timeline row."""
    from datetime import timedelta

    with pg_session_factory() as s:
        u = User(email="mi@ki.se")
        s.add(u)
        s.flush()
        # Profile has must_include=["pathology"].
        p = Profile(
            name="MI", kind=ProfileKind.GROUP, description="",
            structured={
                "areas": ["pathology"],
                "hard_exclusions": [],
                "must_include": ["pathology"],
            },
            created_by=u.id, ranking_scheme="v2",
        )
        s.add(p)
        s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        # Title contains "pathology" → must_include match.
        # Deadline tomorrow → timeline gate FAILs.
        c = Call(
            source_id=src.id, external_id="MI", url="https://x",
            title="Pathology funding announcement",
            summary="Open call.",
            deadline=datetime.now(UTC) + timedelta(days=1),
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(RankingJob(
            call_id=c.id, profile_id=p.id, scheme="v2",
            status=RankingJobStatus.PENDING,
        ))
        s.commit()

    drain_queue(session_factory=pg_session_factory, client=_ollama_v2_clean(), batch_size=10)

    with pg_session_factory() as s:
        ranking = s.execute(select(CallRanking)).scalar_one()
        criteria = s.execute(select(CallRankingCriterion)).scalars().all()

    # The LLM ran, so the score is the LLM topic_fit ≈ 80 (4/5 × 20),
    # NOT zero (which is what would happen without the override).
    assert ranking.aggregate_score >= 70
    assert ranking.aggregate_reason is None
    by_name = {c.criterion: c for c in criteria}
    # All criteria present — the timeline=fail row exists for diagnostics
    # AND the LLM rows are written despite the gate failure.
    assert by_name["timeline"].rating == "fail"
    assert by_name["topic_fit"].rating == "4"
    assert by_name["eligibility"].rating == "pass"
    assert by_name["exclusions"].rating == "pass"


def test_worker_v2_rerank_clears_stale_criterion_rows(pg_session_factory):
    """Codex I1 regression: a re-rank that gate-fails earlier than the
    previous run must NOT leave the previous run's LLM criterion rows
    behind."""
    with pg_session_factory() as s:
        cid, pid = _seed(s, scheme="v2")
        s.commit()

    # First run: clean pass, all 6 criteria written.
    drain_queue(session_factory=pg_session_factory, client=_ollama_v2_clean(), batch_size=10)

    with pg_session_factory() as s:
        criteria_after_first = s.execute(select(CallRankingCriterion)).scalars().all()
    assert len(criteria_after_first) == 6  # 3 gates + 3 LLM rows
    by_name = {c.criterion: c for c in criteria_after_first}
    assert by_name["topic_fit"].rating == "4"

    # Mutate the call so the timeline gate will FAIL on the next run.
    from datetime import timedelta
    with pg_session_factory() as s:
        call = s.get(Call, cid)
        # Make timeline FAIL — deadline in the past.
        call.deadline = datetime.now(UTC) - timedelta(days=1)
        s.commit()
        # Re-enqueue.
        s.add(RankingJob(
            call_id=cid, profile_id=pid, scheme="v2",
            status=RankingJobStatus.PENDING,
        ))
        s.commit()

    # Second run: timeline FAILs early; LLM must NOT be called.
    def boom(request):
        raise AssertionError("LLM should not be called for gate-failed pair")
    bad_client = OllamaClient(
        base_url="http://x", model="m",
        transport=httpx.MockTransport(boom),
    )
    drain_queue(session_factory=pg_session_factory, client=bad_client, batch_size=10)

    with pg_session_factory() as s:
        criteria_after_second = s.execute(select(CallRankingCriterion)).scalars().all()
    by_name = {c.criterion: c for c in criteria_after_second}
    # CRITICAL: stale topic_fit / eligibility / exclusions / budget / trl rows are GONE.
    assert set(by_name.keys()) == {"timeline"}
    assert by_name["timeline"].rating == "fail"


def test_worker_v2_must_include_override_appears_in_summary(pg_session_factory):
    """Codex N1: when must_include bypasses the timeline gate, the rationale
    one-line summary must surface that fact so users aren't surprised by a
    high score on a deadline-imminent call."""
    from datetime import timedelta

    with pg_session_factory() as s:
        u = User(email="ovr@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="OVR", kind=ProfileKind.GROUP, description="",
            structured={
                "areas": ["pathology"], "hard_exclusions": [],
                "must_include": ["pathology"],
            },
            created_by=u.id, ranking_scheme="v2",
        )
        s.add(p)
        s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="OVR", url="https://x",
            title="Pathology funding",
            summary="Open call.",
            deadline=datetime.now(UTC) + timedelta(days=1),
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(RankingJob(
            call_id=c.id, profile_id=p.id, scheme="v2",
            status=RankingJobStatus.PENDING,
        ))
        s.commit()

    drain_queue(session_factory=pg_session_factory, client=_ollama_v2_clean(), batch_size=10)

    with pg_session_factory() as s:
        ranking = s.execute(select(CallRanking)).scalar_one()
    assert "must-include override" in ranking.rationale
    assert "timeline" in ranking.rationale
