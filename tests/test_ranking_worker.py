import json
from datetime import UTC, datetime
from functools import partial

import httpx
from sqlalchemy import select

from grant_lab.db.models import (
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
from grant_lab.ranking.ollama_client import OllamaClient
from grant_lab.ranking.worker import drain_queue as _drain_queue

# Mirrors Settings.embedding_pregate_threshold default. drain_queue now requires
# the threshold (no module-level constant); tests get the same default via
# this wrapper.
drain_queue = partial(_drain_queue, embedding_threshold=0.30)


def _seed(s):
    u = User(email="u@umu.se")
    s.add(u)
    s.flush()
    p = Profile(
        name="P",
        kind=ProfileKind.ORGANIZATIONAL_UNIT,
        description="",
        structured={"areas": ["AI"], "hard_exclusions": []},
        created_by=u.id,
    )
    s.add(p)
    s.flush()
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id,
        external_id="E",
        url="https://x",
        title="AI call",
        summary="Open AI funding call.",
        deadline=datetime(2099, 12, 31, tzinfo=UTC),
        published_at=datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    s.add(
        RankingJob(
            call_id=c.id,
            profile_id=p.id,
            status=RankingJobStatus.PENDING,
        )
    )
    s.flush()
    return c.id, p.id


def _ollama_clean():
    body = {
        "model": "gemma4:e4b-it-q4_K_M",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "topic_fit": {
                                "rating": 4,
                                "rationale": "AI match",
                                "quoted_evidence": "AI",
                            },
                            "eligibility": {
                                "rating": "pass",
                                "rationale": "ok",
                                "quoted_evidence": None,
                            },
                            "exclusions": {
                                "rating": "pass",
                                "rationale": "ok",
                                "quoted_evidence": None,
                            },
                        }
                    ),
                },
                "logprobs": {
                    "content": [
                        {
                            "token": "4",
                            "logprob": -0.1,
                            "top_logprobs": [
                                {"token": "4", "logprob": -0.1},
                                {"token": "3", "logprob": -2.0},
                            ],
                        },
                    ]
                },
            }
        ],
    }

    def handler(request):
        return httpx.Response(200, json=body)

    return OllamaClient(
        base_url="http://x",
        model="gemma4:e4b-it-q4_K_M",
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
    # The fixture's degenerate single-token logprobs block can't offset-bind
    # to the structural rating literal, so EV falls back to argmax: 4 × 20 = 80.
    assert 75 <= r.aggregate_score <= 85
    assert r.aggregate_reason is None
    assert r.prompt_version == "v5.0"


def test_worker_writes_sparse_criterion_rows_clean_path(pg_session_factory):
    """Clean pass — all 3 LLM criteria + 3 gate criteria get rows."""
    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.commit()

    drain_queue(session_factory=pg_session_factory, client=_ollama_clean(), batch_size=10)

    with pg_session_factory() as s:
        criteria = s.execute(select(CallRankingCriterion)).scalars().all()
    by_name = {c.criterion: c for c in criteria}
    # All 3 LLM criteria + 3 gates that ran are written (no timeline row, spec D8).
    assert "topic_fit" in by_name
    assert by_name["topic_fit"].rating == "4"
    assert "eligibility" in by_name
    assert by_name["eligibility"].rating == "pass"
    assert "exclusions" in by_name
    assert by_name["exclusions"].rating == "pass"


def test_worker_short_circuit_writes_up_to_failing_gate_row(pg_session_factory):
    """If timeline fails (the call has expired), rows are written for the gates
    that precede timeline in the short-circuit precedence (budget, trl, both
    PASS here). The timeline FAIL itself writes no row (spec D8; the headline
    aggregate_reason names it). No LLM row, no embedding row (timeline
    short-circuits before it)."""
    with pg_session_factory() as s:
        u = User(email="sc@umu.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="SC",
            kind=ProfileKind.ORGANIZATIONAL_UNIT,
            description="",
            structured={"areas": [], "hard_exclusions": []},
            created_by=u.id,
        )
        s.add(p)
        s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        # Deadline yesterday → timeline FAIL (expired; a deadline tomorrow now PASSes).
        from datetime import timedelta

        c = Call(
            source_id=src.id,
            external_id="SC",
            url="https://x",
            title="T",
            deadline=datetime.now(UTC) - timedelta(days=1),
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(
            RankingJob(
                call_id=c.id,
                profile_id=p.id,
                status=RankingJobStatus.PENDING,
            )
        )
        s.commit()

    # Client that would fail if called.
    def boom(request):
        raise AssertionError("LLM called for gate-failed pair")

    client = OllamaClient(
        base_url="http://x",
        model="m",
        transport=httpx.MockTransport(boom),
    )
    drain_queue(session_factory=pg_session_factory, client=client, batch_size=10)

    with pg_session_factory() as s:
        r = s.execute(select(CallRanking)).scalar_one()
        criteria = s.execute(select(CallRankingCriterion)).scalars().all()
    assert r.aggregate_score == 0
    assert r.aggregate_reason == "timeline"
    by_name = {c.criterion: c for c in criteria}
    # budget + trl precede timeline in precedence (neither FAILs here), then
    # the timeline FAIL stops the sparse write without a row of its own; the
    # headline aggregate_reason names it. No embedding/LLM rows past the stop.
    assert set(by_name.keys()) == {"budget", "trl"}


def test_worker_scores_a_call_closing_tomorrow_and_writes_no_timeline_row(pg_session_factory):
    """G12 at the worker: a real score, and no timeline criterion row (spec D8)."""
    from datetime import timedelta

    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.get(Call, cid).deadline = datetime.now(UTC) + timedelta(days=1)
        s.commit()

    drain_queue(session_factory=pg_session_factory, client=_ollama_clean(), batch_size=10)

    with pg_session_factory() as s:
        ranking = s.execute(select(CallRanking)).scalar_one()
        names = {c.criterion for c in s.execute(select(CallRankingCriterion)).scalars()}
    assert ranking.aggregate_score >= 70
    assert "timeline" not in names
    assert {"budget", "trl", "embedding", "topic_fit", "eligibility", "exclusions"} <= names


def test_worker_expired_call_scores_zero_even_with_must_include(pg_session_factory):
    """G12: must_include no longer rescues a closed call."""
    from datetime import timedelta

    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.get(Profile, pid).structured = {
            "areas": ["AI"],
            "hard_exclusions": [],
            "must_include": ["funding"],
        }
        s.get(Call, cid).deadline = datetime.now(UTC) - timedelta(days=1)
        s.commit()

    drain_queue(session_factory=pg_session_factory, client=_ollama_clean(), batch_size=10)

    with pg_session_factory() as s:
        ranking = s.execute(select(CallRanking)).scalar_one()
    assert (ranking.aggregate_score, ranking.aggregate_reason) == (0, "timeline")


def test_worker_rerank_clears_stale_criterion_rows(pg_session_factory):
    """Codex I1 regression: a re-rank that gate-fails earlier than the
    previous run must NOT leave the previous run's LLM criterion rows
    behind."""
    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.commit()

    # First run: clean pass, all 6 criteria written.
    drain_queue(session_factory=pg_session_factory, client=_ollama_clean(), batch_size=10)

    with pg_session_factory() as s:
        criteria_after_first = s.execute(select(CallRankingCriterion)).scalars().all()
    assert len(criteria_after_first) == 6  # 3 gates (budget/trl/embedding) + 3 LLM rows
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
        s.add(
            RankingJob(
                call_id=cid,
                profile_id=pid,
                status=RankingJobStatus.PENDING,
            )
        )
        s.commit()

    # Second run: timeline FAILs early; LLM must NOT be called.
    def boom(request):
        raise AssertionError("LLM should not be called for gate-failed pair")

    bad_client = OllamaClient(
        base_url="http://x",
        model="m",
        transport=httpx.MockTransport(boom),
    )
    drain_queue(session_factory=pg_session_factory, client=bad_client, batch_size=10)

    with pg_session_factory() as s:
        criteria_after_second = s.execute(select(CallRankingCriterion)).scalars().all()
    by_name = {c.criterion: c for c in criteria_after_second}
    # CRITICAL: stale topic_fit / eligibility / exclusions (LLM rows) and the
    # embedding row are GONE. The timeline short-circuit writes the gates that
    # precede it in precedence (budget, trl — both PASS), and no row of its own.
    assert set(by_name.keys()) == {"budget", "trl"}
    assert "topic_fit" not in by_name
    assert "embedding" not in by_name


def test_worker_writes_embed_similarity_on_clean_path(pg_session_factory, monkeypatch):
    """When rank_pair returns a clean result with embed_similarity=0.45,
    the worker persists that value to call_rankings.embed_similarity."""
    from grant_lab.ranking import worker as worker_mod
    from grant_lab.ranking.criteria import GateResult, Gates, GateStatus
    from grant_lab.ranking.prompt import Parsed
    from grant_lab.ranking.rank_pair import RankResult

    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.commit()

    parsed = Parsed(
        topic_fit_rating=4,
        topic_fit_rationale="ok",
        topic_fit_quoted=None,
        eligibility_rating="pass",
        eligibility_rationale="ok",
        eligibility_quoted=None,
        exclusions_rating="pass",
        exclusions_rationale="ok",
        exclusions_quoted=None,
    )
    stub_result = RankResult(
        score=80,
        reason=None,
        gates=Gates(
            timeline=GateResult(GateStatus.PASS),
            budget=GateResult(GateStatus.PASS),
            trl=GateResult(GateStatus.PASS),
            embedding=GateResult(GateStatus.PASS),
        ),
        parsed=parsed,
        must_include_override=False,
        model="m",
        prompt_version="v3.0",
        raw_response={},
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
    from grant_lab.ranking import worker as worker_mod
    from grant_lab.ranking.criteria import GateResult, Gates, GateStatus
    from grant_lab.ranking.rank_pair import RankResult

    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.commit()

    stub_result = RankResult(
        score=0,
        reason="embedding",
        gates=Gates(
            timeline=GateResult(GateStatus.PASS),
            budget=GateResult(GateStatus.PASS),
            trl=GateResult(GateStatus.PASS),
            embedding=GateResult(GateStatus.FAIL, reason="topic_dissimilar"),
        ),
        parsed=None,
        must_include_override=False,
        model="",
        prompt_version="v3.0",
        raw_response={},
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
    from grant_lab.ranking.criteria import GateResult, Gates, GateStatus
    from grant_lab.ranking.rank_pair import RankResult
    from grant_lab.ranking.worker import _rank_summary

    r = RankResult(
        score=0,
        reason="embedding",
        gates=Gates(
            timeline=GateResult(GateStatus.PASS),
            budget=GateResult(GateStatus.PASS),
            trl=GateResult(GateStatus.PASS),
            embedding=GateResult(GateStatus.FAIL, reason="topic_dissimilar"),
        ),
        parsed=None,
        must_include_override=False,
        model="m",
        prompt_version="v3.0",
        raw_response={},
        embed_similarity=0.18,
    )
    summary = _rank_summary(r)
    assert "topic_dissimilar" in summary
    assert "0.180" in summary


def test_worker_writes_embedding_criterion_row_on_gate_fail(
    pg_session_factory,
    monkeypatch,
):
    """When the embedding gate FAILs (cosine below threshold, no must_include
    override), the CallRankingCriterion table must include a row for
    'embedding' with rating='fail'. Without this row, the call-detail page
    would render two "pass" rows for budget/trl and leave the operator
    wondering why the headline score is 0 — Codex finding from Plan D bug
    review."""
    from grant_lab.ranking import worker as worker_mod
    from grant_lab.ranking.criteria import GateResult, Gates, GateStatus
    from grant_lab.ranking.rank_pair import RankResult

    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.commit()

    stub_result = RankResult(
        score=0,
        reason="embedding",
        gates=Gates(
            timeline=GateResult(GateStatus.PASS),
            budget=GateResult(GateStatus.PASS),
            trl=GateResult(GateStatus.PASS),
            embedding=GateResult(GateStatus.FAIL, reason="topic_dissimilar"),
        ),
        parsed=None,
        must_include_override=False,
        model="",
        prompt_version="v3.0",
        raw_response={},
        embed_similarity=0.18,
    )

    def _stub_rank_pair(**_kwargs):
        return stub_result

    monkeypatch.setattr(worker_mod, "rank_pair", _stub_rank_pair)

    drain_queue(session_factory=pg_session_factory, client=None, batch_size=10)

    with pg_session_factory() as s:
        criteria = s.execute(select(CallRankingCriterion)).scalars().all()
    by_name = {c.criterion: c for c in criteria}
    # Exactly three gate rows — the two preceding gates that PASSed plus
    # the failing embedding row. No LLM rows because parsed is None.
    assert set(by_name.keys()) == {"budget", "trl", "embedding"}
    assert by_name["budget"].rating == "pass"
    assert by_name["trl"].rating == "pass"
    assert by_name["embedding"].rating == "fail"
    assert by_name["embedding"].rationale == "topic_dissimilar"


def test_worker_criteria_terminal_fail_row_matches_aggregate_reason(
    pg_session_factory,
    monkeypatch,
):
    """When multiple gates FAIL, the terminal FAIL row written to the
    breakdown must name the SAME gate as the headline aggregate_reason.

    rank_pair returns the first failing gate in its short-circuit precedence
    (budget → trl → timeline → embedding) as the headline, before aggregate()
    runs, so with budget and timeline both failing the headline is 'budget'.
    _write_criteria_rows must iterate in that same precedence and stop at the
    budget FAIL row. Timeline has no row since ranking spec D8, but it keeps
    its place in the order: a loop that honoured the expired stop before the
    budget row would write no FAIL row at all, and the breakdown would not
    name the 'budget' headline. Regression for that divergence."""
    from grant_lab.ranking import worker as worker_mod
    from grant_lab.ranking.criteria import GateResult, Gates, GateStatus
    from grant_lab.ranking.rank_pair import RankResult

    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.commit()

    # Both timeline AND budget FAIL. rank_pair's precedence makes budget the
    # headline reason (this stub stands in for it); the breakdown must stop at
    # budget.
    stub_result = RankResult(
        score=0,
        reason="budget",
        gates=Gates(
            timeline=GateResult(GateStatus.FAIL, reason="expired"),
            budget=GateResult(GateStatus.FAIL, reason="currency_mismatch"),
            trl=GateResult(GateStatus.PASS),
            embedding=GateResult(GateStatus.PASS),
        ),
        parsed=None,
        must_include_override=False,
        model="",
        prompt_version="v3.0",
        raw_response={},
        embed_similarity=None,
    )

    monkeypatch.setattr(worker_mod, "rank_pair", lambda **_k: stub_result)
    drain_queue(session_factory=pg_session_factory, client=None, batch_size=10)

    with pg_session_factory() as s:
        ranking = s.execute(select(CallRanking)).scalar_one()
        criteria = s.execute(select(CallRankingCriterion)).scalars().all()
    by_name = {c.criterion: c for c in criteria}
    # Only budget was reached (it's first in precedence among the FAILs).
    assert set(by_name.keys()) == {"budget"}
    assert by_name["budget"].rating == "fail"
    # The terminal FAIL row and the headline reason name the same gate.
    assert ranking.aggregate_reason == "budget"
    assert by_name["budget"].rationale == "currency_mismatch"


def test_worker_writes_current_cosine_verbatim_not_preserved(
    pg_session_factory,
    monkeypatch,
):
    """R4: once cosine affects ordering, a None current cosine must OVERWRITE
    a stale prior value (model-mismatch case), not preserve it."""
    from grant_lab.ranking import worker as worker_mod
    from grant_lab.ranking.criteria import GateResult, Gates, GateStatus
    from grant_lab.ranking.prompt import Parsed
    from grant_lab.ranking.rank_pair import RankResult

    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.add(
            CallRanking(
                call_id=cid,
                profile_id=pid,
                aggregate_score=72,
                aggregate_reason=None,
                rationale="prior",
                model="m",
                prompt_version="v4.1",
                raw_response={},
                embed_similarity=0.83,
                ranked_at=datetime.now(UTC),
            )
        )
        s.commit()

    parsed = Parsed(
        topic_fit_rating=4,
        topic_fit_rationale="ok",
        topic_fit_quoted=None,
        eligibility_rating="pass",
        eligibility_rationale="ok",
        eligibility_quoted=None,
        exclusions_rating="pass",
        exclusions_rationale="ok",
        exclusions_quoted=None,
    )
    stub = RankResult(
        score=80,
        reason=None,
        gates=Gates(
            timeline=GateResult(GateStatus.PASS),
            budget=GateResult(GateStatus.PASS),
            trl=GateResult(GateStatus.PASS),
            embedding=GateResult(GateStatus.UNKNOWN),
        ),
        parsed=parsed,
        must_include_override=False,
        model="m",
        prompt_version="v5.0",
        raw_response={},
        embed_similarity=None,
    )
    monkeypatch.setattr(worker_mod, "rank_pair", lambda **_k: stub)
    drain_queue(session_factory=pg_session_factory, client=None, batch_size=10)
    with pg_session_factory() as s:
        r = s.execute(select(CallRanking)).scalar_one()
    assert r.embed_similarity is None  # NOT 0.83 — current run wins
    assert r.aggregate_score == 80


def test_worker_persists_topic_fit_expected(pg_session_factory, monkeypatch):
    from grant_lab.ranking import worker as worker_mod
    from grant_lab.ranking.criteria import GateResult, Gates, GateStatus
    from grant_lab.ranking.prompt import Parsed
    from grant_lab.ranking.rank_pair import RankResult

    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.commit()

    parsed = Parsed(
        topic_fit_rating=4,
        topic_fit_rationale="ok",
        topic_fit_quoted=None,
        eligibility_rating="pass",
        eligibility_rationale="ok",
        eligibility_quoted=None,
        exclusions_rating="pass",
        exclusions_rationale="ok",
        exclusions_quoted=None,
    )
    stub = RankResult(
        score=72,
        reason=None,
        gates=Gates(
            timeline=GateResult(GateStatus.PASS),
            budget=GateResult(GateStatus.PASS),
            trl=GateResult(GateStatus.PASS),
            embedding=GateResult(GateStatus.PASS),
        ),
        parsed=parsed,
        must_include_override=False,
        model="m",
        prompt_version="v5.0",
        raw_response={},
        embed_similarity=0.5,
        topic_fit_expected=3.6,
    )
    monkeypatch.setattr(worker_mod, "rank_pair", lambda **_k: stub)
    drain_queue(session_factory=pg_session_factory, client=None, batch_size=10)

    with pg_session_factory() as s:
        r = s.execute(select(CallRanking)).scalar_one()
    assert r.topic_fit_expected == 3.6
    assert r.aggregate_score == 72


def test_worker_writes_ledger_row_on_escalation_success(pg_session_factory, monkeypatch):
    from grant_lab.db.models import RankingRemoteAttempt
    from grant_lab.ranking import worker as worker_mod
    from grant_lab.ranking.criteria import GateResult, Gates, GateStatus
    from grant_lab.ranking.prompt import Parsed
    from grant_lab.ranking.rank_pair import RankResult

    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.commit()
    parsed = Parsed(
        topic_fit_rating=5,
        topic_fit_rationale="ok",
        topic_fit_quoted=None,
        eligibility_rating="pass",
        eligibility_rationale="ok",
        eligibility_quoted=None,
        exclusions_rating="pass",
        exclusions_rationale="ok",
        exclusions_quoted=None,
    )
    stub = RankResult(
        score=100,
        reason=None,
        gates=Gates(
            timeline=GateResult(GateStatus.PASS),
            budget=GateResult(GateStatus.PASS),
            trl=GateResult(GateStatus.PASS),
            embedding=GateResult(GateStatus.PASS),
        ),
        parsed=parsed,
        must_include_override=False,
        model="claude-sonnet-4-6",
        prompt_version="v5.0",
        raw_response={},
        embed_similarity=0.7,
        summary="an analysis",
        applicant_notes=[{"category": "deadline", "text": "soon"}],
        analysis_model="claude-sonnet-4-6",
        escalated=True,
        escalated_from_score=80,
        escalation_attempted=True,
        escalation_outcome="success",
    )

    class _Remote:
        model = "claude-sonnet-4-6"

    seen = {}

    def _capture(**kwargs):
        seen["remote_client"] = kwargs.get("remote_client")
        seen["escalation_min_score"] = kwargs.get("escalation_min_score")
        return stub

    remote_instance = _Remote()
    monkeypatch.setattr(worker_mod, "rank_pair", _capture)
    # Provide a non-None remote_client so the worker's cap gate path runs.
    drain_queue(
        session_factory=pg_session_factory,
        client=None,
        remote_client=remote_instance,
        batch_size=10,
    )
    with pg_session_factory() as s:
        led = s.execute(select(RankingRemoteAttempt)).scalars().all()
        r = s.execute(select(CallRanking)).scalar_one()
    assert len(led) == 1
    assert led[0].outcome == "success"
    assert r.escalated is True
    assert r.escalated_from_score == 80
    assert r.analysis_model == "claude-sonnet-4-6"
    # Prove cap-gate pass-through: remote_client was forwarded (not nulled)
    # and the default escalation_min_score reached rank_pair unchanged.
    assert seen["remote_client"] is remote_instance
    assert seen["escalation_min_score"] == 45


def test_worker_tick_budget_stops_claiming_and_leaves_remainder_pending(
    pg_session_factory, monkeypatch
):
    """B11 Defect 6: a soft per-tick deadline must stop processing once exceeded,
    leaving the unprocessed claimed jobs PENDING for the next tick instead of
    letting one escalation-heavy batch monopolize the scheduler slot."""
    from grant_lab.ranking import worker as worker_mod
    from grant_lab.ranking.criteria import GateResult, Gates, GateStatus
    from grant_lab.ranking.prompt import Parsed
    from grant_lab.ranking.rank_pair import RankResult

    # Seed THREE pending jobs.
    with pg_session_factory() as s:
        u = User(email="budget@umu.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="BUD",
            kind=ProfileKind.ORGANIZATIONAL_UNIT,
            description="",
            structured={"areas": ["AI"], "hard_exclusions": []},
            created_by=u.id,
        )
        s.add(p)
        s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        for i in range(3):
            c = Call(
                source_id=src.id,
                external_id=f"BUD{i}",
                url=f"https://x/{i}",
                title="AI call",
                summary="Open AI funding call.",
                deadline=datetime(2099, 12, 31, tzinfo=UTC),
                published_at=datetime.now(UTC),
            )
            s.add(c)
            s.flush()
            s.add(RankingJob(call_id=c.id, profile_id=p.id, status=RankingJobStatus.PENDING))
        s.commit()

    parsed = Parsed(
        topic_fit_rating=4,
        topic_fit_rationale="ok",
        topic_fit_quoted=None,
        eligibility_rating="pass",
        eligibility_rationale="ok",
        eligibility_quoted=None,
        exclusions_rating="pass",
        exclusions_rationale="ok",
        exclusions_quoted=None,
    )
    stub = RankResult(
        score=80,
        reason=None,
        gates=Gates(
            timeline=GateResult(GateStatus.PASS),
            budget=GateResult(GateStatus.PASS),
            trl=GateResult(GateStatus.PASS),
            embedding=GateResult(GateStatus.PASS),
        ),
        parsed=parsed,
        must_include_override=False,
        model="m",
        prompt_version="v5.0",
        raw_response={},
        embed_similarity=0.5,
    )

    # Injected monotonic clock that jumps forward 100s on each call, so after
    # the first job the elapsed time blows past a 10s budget.
    ticks = iter([0.0, 0.0, 100.0, 200.0, 300.0])

    def _clock():
        try:
            return next(ticks)
        except StopIteration:
            return 999.0

    monkeypatch.setattr(worker_mod, "rank_pair", lambda **_k: stub)

    processed = _drain_queue(
        session_factory=pg_session_factory,
        client=None,
        embedding_threshold=0.30,
        batch_size=10,
        tick_budget_seconds=10.0,
        _clock=_clock,
    )
    # Only the first job ran; the deadline halted the loop.
    assert processed == 1
    with pg_session_factory() as s:
        statuses = [j.status for j in s.execute(select(RankingJob)).scalars().all()]
        done = [st for st in statuses if st == RankingJobStatus.DONE]
        pending = [st for st in statuses if st == RankingJobStatus.PENDING]
    assert len(done) == 1
    # The remaining two were reverted to PENDING for the next tick.
    assert len(pending) == 2


def test_worker_escalation_ledger_survives_main_commit_failure(pg_session_factory, monkeypatch):
    """B11 Defect 2: when the main merge/commit fails AFTER a billed Sonnet
    escalation, the ledger row must already be committed in its own short
    transaction so the spend is counted — and the next retry tick does NOT
    re-bill a fresh rank_pair. We assert (a) the ledger row survives the failed
    commit, and (b) rank_pair is invoked only ONCE more on the retry (i.e. the
    paid escalation is counted, not silently re-run uncounted)."""
    from grant_lab.db.models import RankingRemoteAttempt
    from grant_lab.ranking import worker as worker_mod
    from grant_lab.ranking.criteria import GateResult, Gates, GateStatus
    from grant_lab.ranking.prompt import Parsed
    from grant_lab.ranking.rank_pair import RankResult

    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.commit()

    parsed = Parsed(
        topic_fit_rating=5,
        topic_fit_rationale="ok",
        topic_fit_quoted=None,
        eligibility_rating="pass",
        eligibility_rationale="ok",
        eligibility_quoted=None,
        exclusions_rating="pass",
        exclusions_rationale="ok",
        exclusions_quoted=None,
    )
    stub = RankResult(
        score=100,
        reason=None,
        gates=Gates(
            timeline=GateResult(GateStatus.PASS),
            budget=GateResult(GateStatus.PASS),
            trl=GateResult(GateStatus.PASS),
            embedding=GateResult(GateStatus.PASS),
        ),
        parsed=parsed,
        must_include_override=False,
        model="claude-sonnet-4-6",
        prompt_version="v5.0",
        raw_response={},
        embed_similarity=0.7,
        summary="an analysis",
        applicant_notes=[{"category": "deadline", "text": "soon"}],
        analysis_model="claude-sonnet-4-6",
        escalated=True,
        escalated_from_score=80,
        escalation_attempted=True,
        escalation_outcome="success",
    )

    call_count = {"n": 0}

    def _capture(**_kwargs):
        call_count["n"] += 1
        return stub

    class _Remote:
        model = "claude-sonnet-4-6"

    monkeypatch.setattr(worker_mod, "rank_pair", _capture)

    # Make the FIRST main-transaction write (criteria rows) blow up, so the
    # main commit never lands. The ledger record was committed before this.
    original_write = worker_mod._write_criteria_rows
    fail_state = {"failed": False}

    def _flaky_write(session, call_id, profile_id, r):
        if not fail_state["failed"]:
            fail_state["failed"] = True
            raise RuntimeError("simulated transient commit failure")
        return original_write(session, call_id, profile_id, r)

    monkeypatch.setattr(worker_mod, "_write_criteria_rows", _flaky_write)

    # First tick: escalation runs (1 rank_pair call), ledger row committed, but
    # the main merge/commit is rolled back by the simulated failure → job back
    # to PENDING.
    drain_queue(
        session_factory=pg_session_factory,
        client=None,
        remote_client=_Remote(),
        batch_size=10,
    )
    with pg_session_factory() as s:
        led_after_first = s.execute(select(RankingRemoteAttempt)).scalars().all()
        job = s.execute(select(RankingJob)).scalar_one()
    # The billed attempt is counted even though the main commit failed.
    assert len(led_after_first) == 1
    assert led_after_first[0].outcome == "success"
    # Job reverted for retry (not done).
    assert job.status in (RankingJobStatus.PENDING, RankingJobStatus.RUNNING)

    # Second tick: succeeds. rank_pair is invoked exactly once more (total 2).
    drain_queue(
        session_factory=pg_session_factory,
        client=None,
        remote_client=_Remote(),
        batch_size=10,
    )
    with pg_session_factory() as s:
        led_after_second = s.execute(select(RankingRemoteAttempt)).scalars().all()
        ranking = s.execute(select(CallRanking)).scalar_one()
    # Two ledger rows total — one per actual rank_pair invocation (both billed).
    # NEITHER attempt is uncounted; this is the honest-spend invariant.
    assert len(led_after_second) == 2
    assert ranking.escalated is True
    assert call_count["n"] == 2


def test_worker_cap_disables_remote_when_exhausted(pg_session_factory, monkeypatch):
    """When today's ledger count >= cap, the worker passes remote_client=None
    into rank_pair (asserted by capturing the kwarg)."""
    from grant_lab.ranking import worker as worker_mod
    from grant_lab.ranking.criteria import GateResult, Gates, GateStatus
    from grant_lab.ranking.prompt import Parsed
    from grant_lab.ranking.rank_pair import RankResult
    from grant_lab.ranking.remote_attempts import record_attempt

    with pg_session_factory() as s:
        cid, pid = _seed(s)
        # Pre-fill the ledger to the cap.
        for _ in range(2):
            record_attempt(s, call_id=cid, profile_id=pid, model="m", outcome="success")
        s.commit()

    seen = {}
    parsed = Parsed(
        topic_fit_rating=3,
        topic_fit_rationale="ok",
        topic_fit_quoted=None,
        eligibility_rating="pass",
        eligibility_rationale="ok",
        eligibility_quoted=None,
        exclusions_rating="pass",
        exclusions_rationale="ok",
        exclusions_quoted=None,
    )
    stub = RankResult(
        score=60,
        reason=None,
        gates=Gates(
            timeline=GateResult(GateStatus.PASS),
            budget=GateResult(GateStatus.PASS),
            trl=GateResult(GateStatus.PASS),
            embedding=GateResult(GateStatus.PASS),
        ),
        parsed=parsed,
        must_include_override=False,
        model="m",
        prompt_version="v5.0",
        raw_response={},
        embed_similarity=0.5,
    )

    def _capture(**kwargs):
        seen["remote_client"] = kwargs.get("remote_client")
        return stub

    class _Remote:
        model = "claude-sonnet-4-6"

    monkeypatch.setattr(worker_mod, "rank_pair", _capture)
    drain_queue(
        session_factory=pg_session_factory,
        client=None,
        remote_client=_Remote(),
        remote_daily_cap=2,
        batch_size=10,
    )
    assert seen["remote_client"] is None  # cap reached → escalation disabled


def test_worker_cap_boundary_allows_attempt_below_cap(pg_session_factory, monkeypatch):
    """At count == cap-1 the gate condition (>= cap) is FALSE, so the worker
    passes the real remote_client through to rank_pair. Pins >= semantics."""
    from grant_lab.ranking import worker as worker_mod
    from grant_lab.ranking.criteria import GateResult, Gates, GateStatus
    from grant_lab.ranking.prompt import Parsed
    from grant_lab.ranking.rank_pair import RankResult
    from grant_lab.ranking.remote_attempts import record_attempt

    with pg_session_factory() as s:
        cid, pid = _seed(s)
        # Pre-fill ledger to cap-1 (count == 1, cap == 2).
        record_attempt(s, call_id=cid, profile_id=pid, model="m", outcome="success")
        s.commit()

    seen = {}
    parsed = Parsed(
        topic_fit_rating=3,
        topic_fit_rationale="ok",
        topic_fit_quoted=None,
        eligibility_rating="pass",
        eligibility_rationale="ok",
        eligibility_quoted=None,
        exclusions_rating="pass",
        exclusions_rationale="ok",
        exclusions_quoted=None,
    )
    stub = RankResult(
        score=60,
        reason=None,
        gates=Gates(
            timeline=GateResult(GateStatus.PASS),
            budget=GateResult(GateStatus.PASS),
            trl=GateResult(GateStatus.PASS),
            embedding=GateResult(GateStatus.PASS),
        ),
        parsed=parsed,
        must_include_override=False,
        model="m",
        prompt_version="v5.0",
        raw_response={},
        embed_similarity=0.5,
    )

    def _capture(**kwargs):
        seen["remote_client"] = kwargs.get("remote_client")
        return stub

    class _Remote:
        model = "claude-sonnet-4-6"

    remote_instance = _Remote()
    monkeypatch.setattr(worker_mod, "rank_pair", _capture)
    drain_queue(
        session_factory=pg_session_factory,
        client=None,
        remote_client=remote_instance,
        remote_daily_cap=2,
        batch_size=10,
    )
    # count(1) < cap(2) → gate does NOT fire → remote forwarded as-is
    assert seen["remote_client"] is remote_instance
