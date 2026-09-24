"""Spec D4, D6 (enqueue half), D7. Gates G1-G4, G6, G7, G11, G13, G15, G22, G24, G27, G28a-d."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from grant_lab.db.models import Call, CallRanking, RankingJob, RankingJobStatus
from grant_lab.profiles.fingerprint import call_rank_fingerprint, rank_fingerprint
from grant_lab.ranking import freshness as F
from grant_lab.ranking.freshness import enqueue_stale_pairs
from tests.freshness_helpers import ctx_for, make_call, make_profile

FUTURE = datetime(2099, 1, 1, tzinfo=UTC)


@pytest.fixture()
def session(pg_session_factory):
    with pg_session_factory() as s:
        yield s


def _stamp(session, call, profile, ctx, **fields) -> CallRanking:
    """A ranking whose three stamps are CURRENT for (call, profile, ctx).
    `fields` override any column, stamps included (e.g. call_fingerprint=None)."""
    values = dict(
        call_id=call.id,
        profile_id=profile.id,
        aggregate_score=60,
        rationale="",
        model="m",
        prompt_version="v5.0",
        raw_response={},
        profile_fingerprint=rank_fingerprint(profile),
        call_fingerprint=call_rank_fingerprint(call),
        scoring_fingerprint=ctx.scoring.fingerprint(),
    )
    values.update(fields)
    cr = CallRanking(**values)
    session.add(cr)
    session.flush()
    return cr


def _pending(session) -> list[tuple[int, int]]:
    return sorted(
        (j.call_id, j.profile_id)
        for j in session.execute(
            select(RankingJob).where(RankingJob.status == RankingJobStatus.PENDING)
        ).scalars()
    )


def _failed_job(session, call, profile, *, days_ago: float, kind: str | None, stamps=None) -> None:
    """`kind` is ranking_jobs.failure_kind; None only for a pre-0081-shaped row."""
    job = RankingJob(
        call_id=call.id,
        profile_id=profile.id,
        status=RankingJobStatus.FAILED,
        attempts=5,
        last_error="dead-lettered",
        failure_kind=kind,
    )
    if stamps is not None:
        job.profile_fingerprint, job.call_fingerprint, job.scoring_fingerprint = stamps
    session.add(job)
    session.flush()
    session.execute(
        update(RankingJob)
        .where(RankingJob.id == job.id)
        .values(updated_at=datetime.now(UTC) - timedelta(days=days_ago))
    )


def _stamps(call, profile, ctx):
    return (rank_fingerprint(profile), call_rank_fingerprint(call), ctx.scoring.fingerprint())


# --- freshness -------------------------------------------------------------


def test_a_fresh_pair_is_not_enqueued_even_with_a_remote_configured(session):
    """G1 at the rule level, and the NULL trap (spec D4): escalation_state is
    NULL on a normal fresh row. `NOT (state = 'pending' AND ...)` is NULL there
    and would make every fresh pair stale."""
    ctx = ctx_for(remote_enabled=True)
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _stamp(session, c, p, ctx)
    assert enqueue_stale_pairs(session, ctx=ctx, call_ids=[c.id]) == 0
    assert enqueue_stale_pairs(session, ctx=ctx) == 0


def test_a_missing_ranking_is_enqueued(session):
    """The population control for every '== 0' above."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    assert enqueue_stale_pairs(session, ctx=ctx, call_ids=[c.id]) == 1
    assert _pending(session) == [(c.id, p.id)]


def test_a_changed_profile_is_enqueued(session):
    """The profile arm of D4's currency check (G2's counterpart on the profile
    side: no numbered gate names it, but `OR cr.profile_fingerprint =
    want.fingerprint` is exactly as load-bearing as the call-fingerprint
    compare G2 pins)."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _stamp(session, c, p, ctx)
    p.description = "Rewritten focus."
    session.flush()
    assert enqueue_stale_pairs(session, ctx=ctx, call_ids=[c.id]) == 1


def test_a_changed_summary_is_enqueued(session):
    """G2."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _stamp(session, c, p, ctx)
    c.summary = "Rewritten scope."
    session.flush()
    assert enqueue_stale_pairs(session, ctx=ctx, call_ids=[c.id]) == 1


def test_a_raw_change_outside_the_ranked_keys_is_not_enqueued(session):
    """G3."""
    ctx = ctx_for()
    p = make_profile(session)
    c = make_call(session, deadline=FUTURE, raw={"trl_min": 3, "metadata": {"t": 1}})
    _stamp(session, c, p, ctx)
    c.raw = {"trl_min": 3, "metadata": {"t": 2}}
    session.flush()
    assert enqueue_stale_pairs(session, ctx=ctx, call_ids=[c.id]) == 0


def test_a_trl_change_is_enqueued(session):
    """G4."""
    ctx = ctx_for()
    p = make_profile(session)
    c = make_call(session, deadline=FUTURE, raw={"trl_min": 3})
    _stamp(session, c, p, ctx)
    c.raw = {"trl_min": 4}
    session.flush()
    assert enqueue_stale_pairs(session, ctx=ctx, call_ids=[c.id]) == 1


def test_an_open_call_published_long_ago_is_enqueued(session):
    """G6 (F3): openness, not publication date."""
    ctx = ctx_for()
    make_profile(session)
    make_call(session, deadline=FUTURE, published_at=datetime.now(UTC) - timedelta(days=120))
    assert enqueue_stale_pairs(session, ctx=ctx) == 1


def test_a_closed_call_is_not_enqueued(session):
    """G7."""
    ctx = ctx_for()
    make_profile(session)
    make_call(session, deadline=datetime.now(UTC) - timedelta(days=1))
    assert enqueue_stale_pairs(session, ctx=ctx) == 0


@pytest.mark.parametrize("field", ["call_fingerprint", "scoring_fingerprint"])
def test_null_stamps_are_stale(session, field):
    """G13: every pre-0081 row has NULL call/scoring stamps. Parametrized over
    both stamps: a NULL `scoring_fingerprint` alone must be stale too, not
    just a NULL `call_fingerprint` -- `cr.scoring_fingerprint = :scoring_fp`
    is a separate AND clause and needs its own case."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _stamp(session, c, p, ctx, **{field: None})
    assert enqueue_stale_pairs(session, ctx=ctx, call_ids=[c.id]) == 1


def test_a_changed_call_vector_is_stale(session):
    """G15."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _stamp(session, c, p, ctx)
    c.embedding_fingerprint = "v2:" + "c" * 64
    session.flush()
    assert enqueue_stale_pairs(session, ctx=ctx, call_ids=[c.id]) == 1


def test_a_changed_scoring_setting_is_stale(session):
    """G24 at the rule level."""
    old, new = (
        ctx_for(embedding_hard_fail_threshold=0.20),
        ctx_for(embedding_hard_fail_threshold=0.10),
    )
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _stamp(session, c, p, old)
    assert enqueue_stale_pairs(session, ctx=old) == 0
    assert enqueue_stale_pairs(session, ctx=new) == 1


def test_a_prompt_version_bump_is_stale(session, monkeypatch):
    """G8 at the rule level."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _stamp(session, c, p, ctx)
    monkeypatch.setattr(F, "PROMPT_VERSION", "v999")
    assert enqueue_stale_pairs(session, ctx=ctx) == 1


# --- escalation state (spec D6, enqueue half) --------------------------------


@pytest.mark.parametrize(
    "state, remote_enabled, expected",
    [
        ("pending", True, 1),  # G17: owed and possible -> due
        ("pending", False, 0),  # no key -> nothing to do (G21)
        ("gave_up", True, 0),  # G19: given up for this revision
        (None, True, 0),  # the NULL trap again, explicitly
    ],
)
def test_escalation_state_decides_whether_a_fresh_pair_is_due(
    session, state, remote_enabled, expected
):
    ctx = ctx_for(remote_enabled=remote_enabled)
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _stamp(session, c, p, ctx, escalation_state=state)
    assert enqueue_stale_pairs(session, ctx=ctx) == expected


# --- dead-letter suppression by input (spec D7) ------------------------------


def test_a_recent_failure_on_the_same_inputs_suppresses(session):
    """G11, and G28b: a PARSE failure on the same inputs. Deterministic at
    temperature 0, so re-running it would fail the same way."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _failed_job(session, c, p, days_ago=2, kind="parse", stamps=_stamps(c, p, ctx))
    assert enqueue_stale_pairs(session, ctx=ctx) == 0


@pytest.mark.parametrize("kind", ["service", "error"])
def test_a_recent_non_parse_failure_on_the_same_inputs_does_not_suppress(session, kind):
    """G28a (`service`) and G28c (`error`), spec D7 amendment I1. Same stamps as
    G11, other kind: an Ollama outage that exhausts MAX_ATTEMPTS is not the
    pair's fault, and a bug should stay visible. Neither silences the pair."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _failed_job(session, c, p, days_ago=2, kind=kind, stamps=_stamps(c, p, ctx))
    assert enqueue_stale_pairs(session, ctx=ctx) == 1


@pytest.mark.parametrize("kind", ["service", "error"])
def test_a_null_stamped_non_parse_failure_does_not_suppress_pair_wide(session, kind):
    """NULL stamps are pair-wide ONLY on a pre-0081 row (failure_kind NULL too).
    A `service` row with NULL stamps is what requeue_stuck_running writes (the
    process died before any capture); an `error` one is a failure before
    capture. Read as pre-0081, a worker restart mid-drain would silence the
    pair for DEAD_LETTER_SUPPRESS_DAYS (mutation M-I1c)."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _failed_job(session, c, p, days_ago=2, kind=kind, stamps=None)
    assert enqueue_stale_pairs(session, ctx=ctx) == 1


def test_a_recent_failure_on_other_inputs_does_not_suppress(session):
    """G22 (gap 2): the corrected call is retried at once. A `parse` failure,
    the only kind that suppresses at all: with any other kind this passes
    without reaching the stamp compare."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _failed_job(session, c, p, days_ago=2, kind="parse", stamps=_stamps(c, p, ctx))
    c.summary = "Corrected text."
    session.flush()
    assert enqueue_stale_pairs(session, ctx=ctx) == 1


def test_a_legacy_failure_with_null_stamps_suppresses_pair_wide(session):
    """G28d: a pre-0081-shaped row (no kind, no stamps) keeps the old pair-wide
    rule; it lapses within DEAD_LETTER_SUPPRESS_DAYS of deploy."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _failed_job(session, c, p, days_ago=2, kind=None, stamps=None)
    assert enqueue_stale_pairs(session, ctx=ctx) == 0


def test_an_old_failure_no_longer_suppresses(session):
    """A `parse` failure (the kind that would otherwise suppress) past the window."""
    ctx = ctx_for()
    p, c = make_profile(session), make_call(session, deadline=FUTURE)
    _failed_job(
        session,
        c,
        p,
        days_ago=F.DEAD_LETTER_SUPPRESS_DAYS + 1,
        kind="parse",
        stamps=_stamps(c, p, ctx),
    )
    assert enqueue_stale_pairs(session, ctx=ctx) == 1


# --- plumbing ----------------------------------------------------------------


def test_an_empty_call_list_inserts_nothing(session):
    make_profile(session)
    make_call(session, deadline=FUTURE)
    assert enqueue_stale_pairs(session, ctx=ctx_for(), call_ids=[]) == 0


def test_an_active_job_is_not_duplicated(session):
    ctx = ctx_for()
    make_profile(session)
    make_call(session, deadline=FUTURE)
    assert enqueue_stale_pairs(session, ctx=ctx) == 1
    assert enqueue_stale_pairs(session, ctx=ctx) == 0


def test_the_narrowed_call_row_and_the_loaded_call_agree(session):
    """G27. Real values on every hashed field: with NULLs, _canonical drops the
    key from both payloads and the two agree about nothing."""
    c = make_call(
        session,
        deadline=FUTURE,
        raw={"trl_min": 3, "trl_max": 6, "metadata": {"big": "x" * 1000}},
        keywords=["ai", "health"],
        budget_min=1,
        budget_max=2,
        currency="EUR",
        programme="Digital Europe",
    )
    c.embedding_fingerprint = "v2:" + "d" * 64
    c.embedding_model = "bge-m3"
    c.embedding = [0.0] * 1024  # ck: embedding and embedding_model are paired
    session.commit()
    # expire_on_commit=False (pg_session_factory) means session.get() below
    # would otherwise hand back the SAME in-memory object commit() just wrote,
    # not a real load -- expire_all() forces the next get() to hit the DB, so
    # the comparison is genuinely Row-vs-loaded-Call.
    session.expire_all()
    loaded = session.get(Call, c.id)
    (row,) = F._call_rows(session, [c.id])
    assert call_rank_fingerprint(row) == call_rank_fingerprint(loaded)
    assert "metadata" not in (row.raw or {}), "the select loaded raw whole"
    assert "embedding" not in row._fields, "the select loaded the 1024-dim vector"
