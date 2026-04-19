import json
from datetime import UTC, datetime
from functools import partial

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
from call_monitor.ranking.worker import drain_queue as _drain_queue

# Mirrors Settings.embedding_pregate_threshold default. drain_queue now requires
# the threshold (no module-level constant); tests get the same default via
# this wrapper.
drain_queue = partial(_drain_queue, embedding_threshold=0.30)


def _seed(s):
    u = User(email="u@ki.se")
    s.add(u)
    s.flush()
    p = Profile(
        name="P", kind=ProfileKind.GROUP, description="",
        structured={"areas": ["AI"], "hard_exclusions": []},
        created_by=u.id,
    )
    s.add(p)
    s.flush()
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id, external_id="E", url="https://x", title="AI call",
        summary="Open AI funding call.", deadline=datetime(2099, 12, 31, tzinfo=UTC),
        published_at=datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    s.add(RankingJob(
        call_id=c.id, profile_id=p.id,
        status=RankingJobStatus.PENDING,
    ))
    s.flush()
    return c.id, p.id


def _ollama_clean():
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


def test_worker_writes_call_ranking_with_aggregate_and_reason(pg_session_factory):
    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.commit()

    client = _ollama_clean()
    drain_queue(session_factory=pg_session_factory, client=client, batch_size=10)

    with pg_session_factory() as s:
        r = s.execute(select(CallRanking)).scalar_one()
    # Topic 4 (logprob expected ≈ 4.0 with strong probability) × 20 ≈ 80
    assert 75 <= r.aggregate_score <= 85
    assert r.aggregate_reason is None
    assert r.prompt_version == "v3.0"


def test_worker_writes_sparse_criterion_rows_clean_path(pg_session_factory):
    """Clean pass — all 3 LLM criteria + 4 gate criteria get rows."""
    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.commit()

    drain_queue(session_factory=pg_session_factory, client=_ollama_clean(), batch_size=10)

    with pg_session_factory() as s:
        criteria = s.execute(select(CallRankingCriterion)).scalars().all()
    by_name = {c.criterion: c for c in criteria}
    # All 3 LLM criteria + 4 gates that ran are written.
    assert "topic_fit" in by_name
    assert by_name["topic_fit"].rating == "4"
    assert "eligibility" in by_name
    assert by_name["eligibility"].rating == "pass"
    assert "exclusions" in by_name
    assert by_name["exclusions"].rating == "pass"
    assert "timeline" in by_name
    assert by_name["timeline"].rating == "pass"


def test_worker_short_circuit_writes_only_failing_gate_row(pg_session_factory):
    """If timeline fails AND no must_include match, only timeline criterion row
    is written. No LLM row."""
    with pg_session_factory() as s:
        u = User(email="sc@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="SC", kind=ProfileKind.GROUP, description="",
            structured={"areas": [], "hard_exclusions": []},
            created_by=u.id,
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
            call_id=c.id, profile_id=p.id,
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


def test_worker_must_include_with_failing_timeline_persists_llm_score(
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
            created_by=u.id,
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
            call_id=c.id, profile_id=p.id,
            status=RankingJobStatus.PENDING,
        ))
        s.commit()

    drain_queue(session_factory=pg_session_factory, client=_ollama_clean(), batch_size=10)

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


def test_worker_rerank_clears_stale_criterion_rows(pg_session_factory):
    """Codex I1 regression: a re-rank that gate-fails earlier than the
    previous run must NOT leave the previous run's LLM criterion rows
    behind."""
    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.commit()

    # First run: clean pass, all 7 criteria written.
    drain_queue(session_factory=pg_session_factory, client=_ollama_clean(), batch_size=10)

    with pg_session_factory() as s:
        criteria_after_first = s.execute(select(CallRankingCriterion)).scalars().all()
    assert len(criteria_after_first) == 7  # 4 gates (timeline/budget/trl/embedding) + 3 LLM rows
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
            call_id=cid, profile_id=pid,
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


def test_worker_writes_embed_similarity_on_clean_path(pg_session_factory, monkeypatch):
    """When rank_pair returns a clean result with embed_similarity=0.45,
    the worker persists that value to call_rankings.embed_similarity."""
    from call_monitor.ranking import worker as worker_mod
    from call_monitor.ranking.criteria import GateResult, Gates, GateStatus
    from call_monitor.ranking.prompt import Parsed
    from call_monitor.ranking.rank_pair import RankResult

    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.commit()

    parsed = Parsed(
        topic_fit_rating=4,
        topic_fit_rationale="ok", topic_fit_quoted=None,
        eligibility_rating="pass", eligibility_rationale="ok", eligibility_quoted=None,
        exclusions_rating="pass", exclusions_rationale="ok", exclusions_quoted=None,
    )
    stub_result = RankResult(
        score=80, reason=None,
        gates=Gates(
            timeline=GateResult(GateStatus.PASS),
            budget=GateResult(GateStatus.PASS),
            trl=GateResult(GateStatus.PASS),
            embedding=GateResult(GateStatus.PASS),
        ),
        parsed=parsed,
        must_include_override=False,
        model="m", prompt_version="v3.0", raw_response={},
        embed_similarity=0.45,
    )

    def _stub_rank_pair(**_kwargs):
        return stub_result

    monkeypatch.setattr(worker_mod, "rank_pair", _stub_rank_pair)

    drain_queue(session_factory=pg_session_factory, client=None, batch_size=10)

    with pg_session_factory() as s:
        r = s.execute(select(CallRanking)).scalar_one()
    assert r.embed_similarity == 0.45
    assert r.aggregate_score == 80


def test_worker_writes_embed_similarity_on_gate_fail_path(pg_session_factory, monkeypatch):
    """Even on gate-FAIL short-circuits (e.g. embedding gate FAILed), the
    cosine should still be persisted — useful for tuning the threshold later."""
    from call_monitor.ranking import worker as worker_mod
    from call_monitor.ranking.criteria import GateResult, Gates, GateStatus
    from call_monitor.ranking.rank_pair import RankResult

    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.commit()

    stub_result = RankResult(
        score=0, reason="embedding",
        gates=Gates(
            timeline=GateResult(GateStatus.PASS),
            budget=GateResult(GateStatus.PASS),
            trl=GateResult(GateStatus.PASS),
            embedding=GateResult(GateStatus.FAIL, reason="topic_dissimilar"),
        ),
        parsed=None,
        must_include_override=False,
        model="", prompt_version="v3.0", raw_response={},
        embed_similarity=0.18,
    )

    def _stub_rank_pair(**_kwargs):
        return stub_result

    monkeypatch.setattr(worker_mod, "rank_pair", _stub_rank_pair)

    drain_queue(session_factory=pg_session_factory, client=None, batch_size=10)

    with pg_session_factory() as s:
        r = s.execute(select(CallRanking)).scalar_one()
    assert r.embed_similarity == 0.18
    assert r.aggregate_score == 0
    assert r.aggregate_reason == "embedding"


def test_worker_summary_renders_embedding_fail_reason():
    """When aggregate_reason == 'embedding', the one-line summary mentions
    'topic_dissimilar' and the cosine value to 3 decimals."""
    from call_monitor.ranking.criteria import GateResult, Gates, GateStatus
    from call_monitor.ranking.rank_pair import RankResult
    from call_monitor.ranking.worker import _rank_summary

    r = RankResult(
        score=0, reason="embedding",
        gates=Gates(
            timeline=GateResult(GateStatus.PASS),
            budget=GateResult(GateStatus.PASS),
            trl=GateResult(GateStatus.PASS),
            embedding=GateResult(GateStatus.FAIL, reason="topic_dissimilar"),
        ),
        parsed=None,
        must_include_override=False,
        model="m", prompt_version="v3.0", raw_response={},
        embed_similarity=0.18,
    )
    summary = _rank_summary(r)
    assert "topic_dissimilar" in summary
    assert "0.180" in summary


def test_worker_must_include_override_appears_in_summary(pg_session_factory):
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
            created_by=u.id,
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
            call_id=c.id, profile_id=p.id,
            status=RankingJobStatus.PENDING,
        ))
        s.commit()

    drain_queue(session_factory=pg_session_factory, client=_ollama_clean(), batch_size=10)

    with pg_session_factory() as s:
        ranking = s.execute(select(CallRanking)).scalar_one()
    assert "must-include override" in ranking.rationale
    assert "timeline" in ranking.rationale


def test_worker_writes_embedding_criterion_row_on_gate_fail(
    pg_session_factory, monkeypatch,
):
    """When the embedding gate FAILs (cosine below threshold, no must_include
    override), the CallRankingCriterion table must include a row for
    'embedding' with rating='fail'. Without this row, the call-detail page
    would render three "pass" rows for timeline/budget/trl and leave the
    operator wondering why the headline score is 0 — Codex finding from
    Plan D bug review."""
    from call_monitor.ranking import worker as worker_mod
    from call_monitor.ranking.criteria import GateResult, Gates, GateStatus
    from call_monitor.ranking.rank_pair import RankResult

    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.commit()

    stub_result = RankResult(
        score=0, reason="embedding",
        gates=Gates(
            timeline=GateResult(GateStatus.PASS),
            budget=GateResult(GateStatus.PASS),
            trl=GateResult(GateStatus.PASS),
            embedding=GateResult(GateStatus.FAIL, reason="topic_dissimilar"),
        ),
        parsed=None,
        must_include_override=False,
        model="", prompt_version="v3.0", raw_response={},
        embed_similarity=0.18,
    )

    def _stub_rank_pair(**_kwargs):
        return stub_result

    monkeypatch.setattr(worker_mod, "rank_pair", _stub_rank_pair)

    drain_queue(session_factory=pg_session_factory, client=None, batch_size=10)

    with pg_session_factory() as s:
        criteria = s.execute(select(CallRankingCriterion)).scalars().all()
    by_name = {c.criterion: c for c in criteria}
    # Exactly four gate rows — the three preceding gates that PASSed plus
    # the failing embedding row. No LLM rows because parsed is None.
    assert set(by_name.keys()) == {"timeline", "budget", "trl", "embedding"}
    assert by_name["timeline"].rating == "pass"
    assert by_name["budget"].rating == "pass"
    assert by_name["trl"].rating == "pass"
    assert by_name["embedding"].rating == "fail"
    assert by_name["embedding"].rationale == "topic_dissimilar"


def test_worker_preserves_embed_similarity_when_new_rank_returns_none(
    pg_session_factory, monkeypatch,
):
    """Codex regression: a degraded re-rank (e.g., embedding model swap →
    profile/call vectors disagree → _safe_cosine returns None → UNKNOWN gate
    → LLM still runs) yields RankResult with embed_similarity=None.

    The worker must NOT clobber the previously-recorded value with NULL —
    operators tuning the cosine pre-gate threshold rely on the historical
    diagnostic signal."""
    from call_monitor.ranking import worker as worker_mod
    from call_monitor.ranking.criteria import GateResult, Gates, GateStatus
    from call_monitor.ranking.prompt import Parsed
    from call_monitor.ranking.rank_pair import RankResult

    with pg_session_factory() as s:
        cid, pid = _seed(s)
        # Pre-seed a CallRanking row with embed_similarity=0.83 — the value
        # we want to verify is preserved across the degraded re-rank.
        s.add(CallRanking(
            call_id=cid, profile_id=pid,
            aggregate_score=72, aggregate_reason=None,
            rationale="prior run", model="m", prompt_version="v2.0",
            raw_response={}, embed_similarity=0.83,
            ranked_at=datetime.now(UTC),
        ))
        s.commit()

    parsed = Parsed(
        topic_fit_rating=4,
        topic_fit_rationale="ok", topic_fit_quoted=None,
        eligibility_rating="pass", eligibility_rationale="ok", eligibility_quoted=None,
        exclusions_rating="pass", exclusions_rationale="ok", exclusions_quoted=None,
    )
    # Simulate degraded re-rank: embedding gate UNKNOWN (cosine returned
    # None due to model mismatch), LLM ran, non-zero score, but
    # embed_similarity=None on the new result.
    stub_result = RankResult(
        score=80, reason=None,
        gates=Gates(
            timeline=GateResult(GateStatus.PASS),
            budget=GateResult(GateStatus.PASS),
            trl=GateResult(GateStatus.PASS),
            embedding=GateResult(GateStatus.UNKNOWN),
        ),
        parsed=parsed,
        must_include_override=False,
        model="m", prompt_version="v3.0", raw_response={},
        embed_similarity=None,
    )

    def _stub_rank_pair(**_kwargs):
        return stub_result

    monkeypatch.setattr(worker_mod, "rank_pair", _stub_rank_pair)

    drain_queue(session_factory=pg_session_factory, client=None, batch_size=10)

    with pg_session_factory() as s:
        r = s.execute(select(CallRanking)).scalar_one()
    # The prior diagnostic value MUST be preserved. The new score, model,
    # rationale etc. all updated normally (verified by the score change).
    assert r.embed_similarity == 0.83
    assert r.aggregate_score == 80
