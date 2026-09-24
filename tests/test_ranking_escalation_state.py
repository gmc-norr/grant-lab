"""Spec D6 at the worker, and D7's stamps. Gates G17-G21."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from grant_lab.db.models import Call, CallRanking, RankingJob, RankingJobStatus
from grant_lab.profiles.fingerprint import call_rank_fingerprint, rank_fingerprint
from grant_lab.ranking import worker as worker_mod
from grant_lab.ranking.criteria import GateResult, Gates, GateStatus
from grant_lab.ranking.prompt import Parsed, ParseError
from grant_lab.ranking.queue import enqueue_for_pair, mark_failed
from grant_lab.ranking.rank_pair import RankResult
from grant_lab.ranking.remote_attempts import record_attempt
from grant_lab.ranking.worker import drain_queue
from tests.freshness_helpers import make_call, make_profile, ollama_clean, scoring_for


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


def _drain(sf, monkeypatch, stub, *, remote=_Remote, cap=150):
    monkeypatch.setattr(worker_mod, "rank_pair", lambda **kw: stub)
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
    _drain(pg_session_factory, monkeypatch, _stub(), cap=1)
    assert (
        _row(pg_session_factory, pair).escalation_state,
        _row(pg_session_factory, pair).escalation_failures,
    ) == ("pending", 0)

    _requeue(pg_session_factory, pair)
    _drain(
        pg_session_factory,
        monkeypatch,
        _stub(escalated=True, attempted=True, outcome="success"),
        cap=150,
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
    """G21."""
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
        from grant_lab.db.models import Profile

        prof = s.get(Profile, pair[1])
        assert job.status == RankingJobStatus.FAILED
        assert (job.profile_fingerprint, job.call_fingerprint, job.scoring_fingerprint) == (
            rank_fingerprint(prof),
            call_rank_fingerprint(call),
            scoring_for(None).fingerprint(),
        )


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
            mark_failed(s, job_id, error="boom", terminal=True, stamps=stamps)
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
        mark_failed(s, job_id, error="boom", terminal=False)
        s.commit()
    with pg_session_factory() as s:
        assert _job_stamps(s.get(RankingJob, job_id)) == (None, None, None)
        mark_failed(s, job_id, error="boom", terminal=True, stamps=("p", "c", "s"))
        s.commit()
    with pg_session_factory() as s:
        assert _job_stamps(s.get(RankingJob, job_id)) == ("p", "c", "s")
