"""Spec D6 at the worker, and D7's stamps and failure kinds. Gates G17-G21, G28e."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from grant_lab.db.models import Call, CallRanking, Profile, RankingJob, RankingJobStatus
from grant_lab.profiles.fingerprint import call_rank_fingerprint, rank_fingerprint
from grant_lab.ranking import worker as worker_mod
from grant_lab.ranking.criteria import GateResult, Gates, GateStatus
from grant_lab.ranking.freshness import enqueue_stale_pairs
from grant_lab.ranking.ollama_client import LLMError, OllamaClient
from grant_lab.ranking.prompt import Parsed, ParseError
from grant_lab.ranking.queue import MAX_ATTEMPTS, enqueue_for_pair, mark_failed
from grant_lab.ranking.rank_pair import RankResult
from grant_lab.ranking.remote_attempts import record_attempt
from grant_lab.ranking.worker import drain_queue
from tests.freshness_helpers import (
    ctx_for,
    enqueue_job,
    make_call,
    make_profile,
    ollama_clean,
    scoring_for,
)


class _Remote:
    model = "claude-sonnet-5"


def _stub(*, wanted=True, escalated=False, attempted=False, outcome=None, score=60):
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
    return RankResult(
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
        model="m",
        prompt_version="v5.0",
        raw_response={},
        embed_similarity=None,
        escalated=escalated,
        escalated_from_score=score if escalated else None,
        escalation_attempted=attempted,
        escalation_outcome=outcome,
        escalation_wanted=wanted,
    )


@pytest.fixture()
def pair(pg_session_factory):
    with pg_session_factory() as s:
        p, c = make_profile(s), make_call(s)
        enqueue_for_pair(s, call_id=c.id, profile_id=p.id)
        s.commit()
        return c.id, p.id


def _drain(sf, monkeypatch, stub, *, remote=_Remote, cap=150, expect_remote=None):
    """`expect_remote`: None = no check; True/False = rank_pair must (not) be
    handed a remote client. The stub ignores the client otherwise, so without
    this a test about the cap cannot tell whether the cap bound at all."""

    def _rank_pair(**kw):
        if expect_remote is not None and (kw["remote_client"] is not None) != expect_remote:
            pytest.fail(
                f"rank_pair got remote_client={kw['remote_client']!r}; the daily cap "
                f"did not act as this test assumes (expected a client: {expect_remote})"
            )
        return stub

    monkeypatch.setattr(worker_mod, "rank_pair", _rank_pair)
    drain_queue(
        session_factory=sf,
        client=None,
        embedding_threshold=0.0,
        remote_client=remote() if remote else None,
        remote_daily_cap=cap,
        batch_size=10,
    )


def _row(sf, pair) -> CallRanking:
    with sf() as s:
        return s.get(CallRanking, pair)


def _requeue(sf, pair):
    with sf() as s:
        enqueue_for_pair(s, call_id=pair[0], profile_id=pair[1])
        s.commit()


def test_a_cap_withheld_escalation_is_pending_then_completes(pg_session_factory, monkeypatch, pair):
    """G17."""
    with pg_session_factory() as s:
        record_attempt(s, call_id=pair[0], profile_id=pair[1], model="m", outcome="success")
        s.commit()
    # The cap (1) is reached: rank_pair must run WITHOUT the remote client.
    _drain(pg_session_factory, monkeypatch, _stub(), cap=1, expect_remote=False)
    assert (
        _row(pg_session_factory, pair).escalation_state,
        _row(pg_session_factory, pair).escalation_failures,
    ) == ("pending", 0)

    _requeue(pg_session_factory, pair)
    # The next day, with cap left: the remote client is handed over again.
    _drain(
        pg_session_factory,
        monkeypatch,
        _stub(escalated=True, attempted=True, outcome="success"),
        cap=150,
        expect_remote=True,
    )
    assert _row(pg_session_factory, pair).escalation_state is None


def test_llm_errors_never_exhaust_the_pair(pg_session_factory, monkeypatch, pair):
    """G18."""
    for _ in range(5):
        _drain(pg_session_factory, monkeypatch, _stub(attempted=True, outcome="llm_error"))
        _requeue(pg_session_factory, pair)
    r = _row(pg_session_factory, pair)
    assert (r.escalation_state, r.escalation_failures) == ("pending", 0)


def test_three_parse_errors_in_one_revision_give_up(pg_session_factory, monkeypatch, pair):
    """G19."""
    seen = []
    for _ in range(3):
        _drain(pg_session_factory, monkeypatch, _stub(attempted=True, outcome="parse_error"))
        r = _row(pg_session_factory, pair)
        seen.append((r.escalation_state, r.escalation_failures))
        _requeue(pg_session_factory, pair)
    assert seen == [("pending", 1), ("pending", 2), ("gave_up", 3)]


def test_a_new_revision_resets_the_count(pg_session_factory, monkeypatch, pair):
    """G20."""
    for _ in range(3):
        _drain(pg_session_factory, monkeypatch, _stub(attempted=True, outcome="parse_error"))
        _requeue(pg_session_factory, pair)
    assert _row(pg_session_factory, pair).escalation_state == "gave_up"
    with pg_session_factory() as s:
        s.get(Call, pair[0]).summary = "A corrected call text."
        s.commit()
    _drain(pg_session_factory, monkeypatch, _stub(attempted=True, outcome="parse_error"))
    r = _row(pg_session_factory, pair)
    assert (r.escalation_state, r.escalation_failures) == ("pending", 1)


def test_no_remote_configured_owes_nothing(pg_session_factory, monkeypatch, pair):
    """G21. Starts from a PENDING row with a failure counted, so the column
    defaults (NULL, 0) cannot satisfy it: the worker must clear both."""
    _drain(pg_session_factory, monkeypatch, _stub(attempted=True, outcome="parse_error"))
    r = _row(pg_session_factory, pair)
    assert (r.escalation_state, r.escalation_failures) == ("pending", 1)

    _requeue(pg_session_factory, pair)
    _drain(pg_session_factory, monkeypatch, _stub(), remote=None)
    r = _row(pg_session_factory, pair)
    assert (r.escalation_state, r.escalation_failures) == (None, 0)


def test_the_worker_stamps_all_three_fingerprints(pg_session_factory, pair):
    drain_queue(
        session_factory=pg_session_factory,
        client=ollama_clean(),
        embedding_threshold=0.0,
        batch_size=10,
    )
    with pg_session_factory() as s:
        cr = s.get(CallRanking, pair)
        call = s.get(Call, pair[0])
        assert cr.call_fingerprint == call_rank_fingerprint(call)
        assert cr.scoring_fingerprint == scoring_for(ollama_clean()).fingerprint()
        assert cr.profile_fingerprint is not None


def test_a_terminal_failure_is_stamped_with_its_inputs(pg_session_factory, monkeypatch, pair):
    """Spec D7: suppression needs to know WHAT failed."""
    with pg_session_factory() as s:
        job = s.execute(select(RankingJob)).scalar_one()
        job.attempts = 4  # MAX_ATTEMPTS is 5: the next failure is terminal
        s.commit()

    def _raise(**kw):
        raise ParseError("exclusions missing or not an object")

    monkeypatch.setattr(worker_mod, "rank_pair", _raise)
    drain_queue(
        session_factory=pg_session_factory, client=None, embedding_threshold=0.0, batch_size=10
    )
    with pg_session_factory() as s:
        job = s.execute(select(RankingJob)).scalar_one()
        call = s.get(Call, pair[0])
        prof = s.get(Profile, pair[1])
        assert job.status == RankingJobStatus.FAILED
        assert (job.profile_fingerprint, job.call_fingerprint, job.scoring_fingerprint) == (
            rank_fingerprint(prof),
            call_rank_fingerprint(call),
            scoring_for(None).fingerprint(),
        )


def _set_attempts(sf, job_id: int, attempts: int) -> None:
    with sf() as s:
        s.get(RankingJob, job_id).attempts = attempts
        s.commit()


def _raise_unexpected(**kw):
    raise RuntimeError("an unexpected bug inside rank_pair")


@pytest.mark.parametrize("handler", ["unexpected", "llm_error"])
def test_a_failure_after_capture_is_stamped_in_the_outer_handlers(
    pg_session_factory, monkeypatch, pair, handler
):
    """Spec D7 for failures the inner `except (LLMError, ParseError)` does not
    see. `unexpected`: rank_pair raises a non-LLM error (the `except Exception`
    handler). `llm_error`: an LLMError after rank_pair returned (the outer
    `except LLMError` handler). Both happen after the stamps are captured, so
    the dead letter is keyed on its inputs, not pair-wide."""
    with pg_session_factory() as s:
        job_id = s.execute(select(RankingJob.id)).scalar_one()
    _set_attempts(pg_session_factory, job_id, 4)  # MAX_ATTEMPTS is 5: terminal

    if handler == "unexpected":
        monkeypatch.setattr(worker_mod, "rank_pair", _raise_unexpected)
    else:
        monkeypatch.setattr(worker_mod, "rank_pair", lambda **kw: _stub(wanted=False))

        def _llm_error(*a, **kw):
            raise LLMError("ollama dropped the connection")

        monkeypatch.setattr(worker_mod, "_write_criteria_rows", _llm_error)

    drain_queue(
        session_factory=pg_session_factory, client=None, embedding_threshold=0.0, batch_size=10
    )
    with pg_session_factory() as s:
        job = s.get(RankingJob, job_id)
        assert job.status == RankingJobStatus.FAILED
        expected_error = "RuntimeError" if handler == "unexpected" else "ollama dropped"
        assert expected_error in job.last_error, "the failure took a different path"
        # Spec D7 amendment (I1): neither outer handler's failure suppresses.
        assert job.failure_kind == ("error" if handler == "unexpected" else "service")
        assert _job_stamps(job) == (
            rank_fingerprint(s.get(Profile, pair[1])),
            call_rank_fingerprint(s.get(Call, pair[0])),
            scoring_for(None).fingerprint(),
        )


def test_a_failure_before_capture_is_never_stamped_with_another_jobs_inputs(
    pg_session_factory, monkeypatch, pair
):
    """The per-job reset. Job A ranks (its stamps are captured); job B then
    fails before capture. B must stay NULL (nothing was captured for it),
    never carry A's inputs -- which describe a different pair."""
    with pg_session_factory() as s:
        job_a = s.execute(select(RankingJob.id)).scalar_one()
        s.get(RankingJob, job_a).updated_at = datetime.now(UTC) - timedelta(minutes=5)
        job_b = enqueue_job(s, call_id=make_call(s).id, profile_id=make_profile(s).id)
        s.get(RankingJob, job_b).attempts = 4  # terminal on this failure
        s.commit()

    real_touch = worker_mod.touch_running_job

    def _touch(session, job_id):
        if job_id == job_b:
            raise RuntimeError("the database went away before capture")
        return real_touch(session, job_id)

    monkeypatch.setattr(worker_mod, "touch_running_job", _touch)
    monkeypatch.setattr(worker_mod, "rank_pair", lambda **kw: _stub(wanted=False))
    drain_queue(
        session_factory=pg_session_factory, client=None, embedding_threshold=0.0, batch_size=10
    )
    with pg_session_factory() as s:
        a, b = s.get(RankingJob, job_a), s.get(RankingJob, job_b)
        assert a.status == RankingJobStatus.DONE, "job A must rank first, capturing stamps"
        assert b.status == RankingJobStatus.FAILED
        assert _job_stamps(b) == (None, None, None)


def _job_stamps(job: RankingJob) -> tuple[str | None, str | None, str | None]:
    return (job.profile_fingerprint, job.call_fingerprint, job.scoring_fingerprint)


@pytest.mark.parametrize(
    "stamps",
    [
        (None, "c", "s"),
        ("p", None, "s"),
        ("p", "c", None),
        ("p", "", "s"),
        ("p", "c"),
    ],
    ids=["no-profile", "no-call", "no-scoring", "empty-call", "two-of-three"],
)
def test_mark_failed_refuses_a_partial_set_of_stamps(pg_session_factory, pair, stamps):
    """All three stamps or none (carry-over from the Task 4 review).

    enqueue_stale_pairs treats a failed job as pre-0081 (pair-wide) only when
    `profile_fingerprint IS NULL`, and as the same inputs only when all three
    stamps equal the current ones. A partial set matches neither arm, so the
    pair would be retried every day. mark_failed refuses one before writing.
    """
    with pg_session_factory() as s:
        job_id = s.execute(select(RankingJob.id)).scalar_one()
        with pytest.raises(ValueError, match="all three"):
            mark_failed(s, job_id, error="boom", terminal=True, stamps=stamps, failure_kind="parse")
        s.commit()
    with pg_session_factory() as s:
        job = s.get(RankingJob, job_id)
        assert job.status == RankingJobStatus.PENDING
        assert _job_stamps(job) == (None, None, None)


def test_mark_failed_writes_all_three_stamps_or_none(pg_session_factory, pair):
    """The control for the refusal above: a complete set is written whole, and
    no set leaves all three NULL (spec D7's pair-wide rule)."""
    with pg_session_factory() as s:
        job_id = s.execute(select(RankingJob.id)).scalar_one()
        mark_failed(s, job_id, error="boom", terminal=False, failure_kind="error")
        s.commit()
    with pg_session_factory() as s:
        assert _job_stamps(s.get(RankingJob, job_id)) == (None, None, None)
        mark_failed(
            s, job_id, error="boom", terminal=True, stamps=("p", "c", "s"), failure_kind="parse"
        )
        s.commit()
    with pg_session_factory() as s:
        assert _job_stamps(s.get(RankingJob, job_id)) == ("p", "c", "s")


# --- failure kinds (spec D7 amendment, final review I1) -----------------------


@pytest.mark.parametrize("kind", [None, "bogus", "PARSE"])
def test_mark_failed_refuses_a_missing_or_unknown_failure_kind(pg_session_factory, pair, kind):
    """A NULL failure_kind is reserved for rows written before 0081, which
    enqueue_stale_pairs reads (with NULL stamps) as the pair-wide legacy rule.
    A caller that did not classify its failure must fail loudly here, not
    write a row that silences the pair for DEAD_LETTER_SUPPRESS_DAYS. Refused
    before anything is written."""
    with pg_session_factory() as s:
        job_id = s.execute(select(RankingJob.id)).scalar_one()
        with pytest.raises(ValueError, match="failure_kind"):
            mark_failed(s, job_id, error="boom", terminal=True, failure_kind=kind)
        s.commit()
    with pg_session_factory() as s:
        job = s.get(RankingJob, job_id)
        assert (job.status, job.failure_kind) == (RankingJobStatus.PENDING, None)


def test_mark_failed_writes_the_kind_of_every_failure(pg_session_factory, pair):
    """The last failure's kind is what a FAILED row carries: a service blip on
    an early attempt must not stick to a job whose final attempt failed to
    parse."""
    with pg_session_factory() as s:
        job_id = s.execute(select(RankingJob.id)).scalar_one()
        mark_failed(s, job_id, error="refused", terminal=False, failure_kind="service")
        s.commit()
    with pg_session_factory() as s:
        assert s.get(RankingJob, job_id).failure_kind == "service"
        mark_failed(
            s,
            job_id,
            error="exclusions missing",
            terminal=True,
            stamps=("p", "c", "s"),
            failure_kind="parse",
        )
        s.commit()
    with pg_session_factory() as s:
        job = s.get(RankingJob, job_id)
        assert (job.status, job.failure_kind) == (RankingJobStatus.FAILED, "parse")


_GEMMA = "gemma4:e4b-it-q4_K_M"


def _refusing_ollama(seen: list) -> OllamaClient:
    """Local Ollama is down: every request is refused (the final review's probe)."""

    def handler(request):
        seen.append(request)
        raise httpx.ConnectError("[Errno 111] Connection refused", request=request)

    return OllamaClient(base_url="http://x", model=_GEMMA, transport=httpx.MockTransport(handler))


def _unparseable_ollama(seen: list) -> OllamaClient:
    """Gemma answers, but without `exclusions`: the spec §1.6 parse failure."""
    content = json.dumps(
        {
            "topic_fit": {"rating": 4, "rationale": "AI match", "quoted_evidence": None},
            "eligibility": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
        }
    )
    body = {
        "model": _GEMMA,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=body)

    return OllamaClient(base_url="http://x", model=_GEMMA, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "make_client, kind, error_text, enqueued",
    [
        (_refusing_ollama, "service", "Connection refused", 1),
        (_unparseable_ollama, "parse", "exclusions missing", 0),
    ],
    ids=["service", "parse"],
)
def test_a_dead_letter_suppresses_its_pair_only_after_a_parse_failure(
    pg_session_factory, pair, make_client, kind, error_text, enqueued
):
    """G28e, end to end: the real worker, a real OllamaClient, the real rule.

    `service`: Ollama refuses every connection until the job exhausts
    MAX_ATTEMPTS and dead-letters, as a ~15-minute outage does. The FAILED row
    is stamped with its inputs, and the next ingest/reconcile must still
    enqueue the pair: before fix I1 it was silenced for 7 days. `parse`: Gemma
    answers without `exclusions` on every attempt; the same inputs fail the
    same way at temperature 0, so the pair stays suppressed. Each leg is the
    other's population control (same fixture, same context)."""
    seen: list = []
    client = make_client(seen)
    for _ in range(MAX_ATTEMPTS):
        drain_queue(
            session_factory=pg_session_factory,
            client=client,
            embedding_threshold=0.0,
            batch_size=10,
        )
    assert len(seen) == MAX_ATTEMPTS, "every attempt must reach the model"
    with pg_session_factory() as s:
        job = s.execute(select(RankingJob)).scalar_one()
        assert (job.status, job.attempts) == (RankingJobStatus.FAILED, MAX_ATTEMPTS)
        assert error_text in job.last_error, "the failure took a different path"
        assert job.failure_kind == kind
        assert _job_stamps(job) == (
            rank_fingerprint(s.get(Profile, pair[1])),
            call_rank_fingerprint(s.get(Call, pair[0])),
            scoring_for(client).fingerprint(),
        )
        assert enqueue_stale_pairs(s, ctx=ctx_for(client)) == enqueued
