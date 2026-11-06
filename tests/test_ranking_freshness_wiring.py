"""The rule, wired in. Gates G1 (end to end), G6, G23, G26."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

import grant_lab.scheduler as scheduler_mod
from grant_lab.auth.email import InMemoryEmailSender
from grant_lab.config import Settings
from grant_lab.db.models import Call, CallRanking, RankingJob, RankingJobStatus
from grant_lab.embeddings.client import EmbedResult
from grant_lab.ingestion.alerts import ConsecutiveFailureTracker
from grant_lab.ingestion.orchestrator import run_ingestion
from grant_lab.ranking.freshness import ScoringConfig, enqueue_stale_pairs
from grant_lab.ranking.queue import MAX_ATTEMPTS
from grant_lab.ranking.worker import drain_queue
from grant_lab.sources.types import NormalizedCall
from tests.freshness_helpers import (
    ctx_for,
    enqueue_job,
    make_call,
    make_profile,
    ollama_clean,
    ollama_context_overflow,
)

FUTURE = datetime(2099, 1, 1, tzinfo=UTC)


def _embed_client():
    c = MagicMock()
    c.model = "bge-m3"
    c.embed.return_value = EmbedResult(vector=[0.1] * 1024, embedded_chars=1)
    return c


class _StaticAdapter:
    slug = "vinnova"

    def __init__(self, calls):
        self._calls = list(calls)

    def fetch_new_calls(self, since):
        yield from self._calls


def _pending(sf) -> int:
    with sf() as s:
        return len(
            s.execute(select(RankingJob).where(RankingJob.status == RankingJobStatus.PENDING))
            .scalars()
            .all()
        )


def _ingest(sf, summary, ctx):
    run_ingestion(
        session_factory=sf,
        adapter=_StaticAdapter(
            [
                NormalizedCall(
                    external_id="U-1",
                    url="https://x/u1",
                    title="Unchanged call",
                    summary=summary,
                    deadline=FUTURE,
                )
            ]
        ),
        alerter=ConsecutiveFailureTracker(
            threshold=3, sender=InMemoryEmailSender(), admin_email=None
        ),
        embed_client=_embed_client(),
        freshness=ctx,
    )


def test_an_unchanged_call_reingested_is_not_re_ranked(pg_session_factory, profile_factory):
    """G1 end to end (F1). The drain uses the same scoring values as `ctx`, with
    a remote configured on the enqueue side (the NULL trap)."""
    profile_factory()
    ctx = ctx_for(ollama_clean(), remote_enabled=True)
    _ingest(pg_session_factory, "Same text.", ctx)
    assert _pending(pg_session_factory) == 1, "population control: the first ingest enqueues"
    drain_queue(
        session_factory=pg_session_factory,
        client=ollama_clean(),
        embedding_threshold=0.0,
        batch_size=10,
    )
    assert _pending(pg_session_factory) == 0
    # The drain RANKED the pair, stamped. `_pending == 0` alone also holds for a
    # dead-lettered job, and suppression (same inputs) would then keep the
    # unchanged re-ingest below quiet too -- a pass with nothing ranked.
    with pg_session_factory() as s:
        rk = s.execute(select(CallRanking)).scalar_one_or_none()
        assert rk is not None, "the drain did not rank the pair"
        assert rk.call_fingerprint is not None
        assert rk.scoring_fingerprint == ctx.scoring.fingerprint()

    _ingest(pg_session_factory, "Same text.", ctx)
    assert _pending(pg_session_factory) == 0, "an unchanged call was re-enqueued"

    _ingest(pg_session_factory, "The funder changed the scope.", ctx)
    assert _pending(pg_session_factory) == 1, "a changed call was NOT re-enqueued"


# --- G26: the real scheduler hands ingest and reconcile the drain's own values ---


class _NullAlerter:
    def record_failure(self, *a, **kw):
        return None

    def record_success(self, *a, **kw):
        return None


class _FakeEmbedder:
    model = "fake-embed"

    def embed(self, text: str):
        return [0.1] * 1024


@pytest.fixture()
def _settings_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")
    # Not the default (False): a FreshnessContext that hard-coded
    # logprobs_enabled=False would otherwise agree with the drain by accident.
    monkeypatch.setenv("RANKING_LOGPROBS_ENABLED", "true")


SCORING_PARAMS = (
    "client",
    "embedding_threshold",
    "embedding_hard_fail_threshold",
    "escalation_min_score",
    "logprobs_enabled",
)


@pytest.mark.parametrize("api_key", [None, "sk-ant-test-not-a-real-key"])
def test_the_scheduler_hands_enqueue_the_drain_values(
    monkeypatch, pg_session_factory, _settings_env, api_key
):
    """G26. No network: every job body below is monkeypatched before it runs,
    and AnthropicClient() makes no request on construction."""
    seen: dict = {}
    monkeypatch.setattr(
        scheduler_mod, "drain_queue", lambda **kw: seen.setdefault("drain", kw) and 0
    )
    monkeypatch.setattr(scheduler_mod, "run_ingestion", lambda **kw: seen.setdefault("ingest", kw))
    monkeypatch.setattr(
        scheduler_mod,
        "reconcile_missing_rankings",
        lambda session, **kw: seen.setdefault("reconcile", kw) and 0,
    )
    sched = scheduler_mod.build_scheduler(
        session_factory=pg_session_factory,
        settings=Settings(),
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.27,
        embedding_hard_fail_threshold=0.18,
        anthropic_api_key=api_key,
        ranking_escalation_min_score=50,
        embed_client=_FakeEmbedder(),
        start=False,
    )
    try:
        for job_id in ("rank:drain", "ingest:vinnova", "ranking:reconcile"):
            job = sched.get_job(job_id)
            assert job is not None, job_id
            job.func()
    finally:
        sched.shutdown(wait=False)

    assert {"drain", "ingest", "reconcile"} <= set(seen), seen.keys()
    d = seen["drain"]
    want = ScoringConfig.for_drain(**{k: d[k] for k in SCORING_PARAMS})
    for ctx in (seen["ingest"]["freshness"], seen["reconcile"]["ctx"]):
        assert ctx.scoring == want
        assert ctx.remote_enabled is (d["remote_client"] is not None)
    assert want.local_model == "gemma4:e4b-q8" and want.embedding_threshold == 0.27


def test_the_scheduler_wires_tick_batch_and_daily_cap_to_the_drain_job(
    monkeypatch, pg_session_factory, _settings_env
):
    """The drain job's interval, `batch_size` and `remote_daily_cap` each come
    from their own value. Three distinct numbers, so a swap between any two of
    them (or with another count) shows up. Nothing else reads the trigger
    interval or those two drain keyword arguments."""
    seen: dict = {}
    monkeypatch.setattr(
        scheduler_mod, "drain_queue", lambda **kw: seen.setdefault("drain", kw) and 0
    )
    sched = scheduler_mod.build_scheduler(
        session_factory=pg_session_factory,
        settings=Settings(),
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=37,
        ranking_queue_batch=11,
        embedding_pregate_threshold=0.27,
        ranking_remote_daily_cap=223,
        embed_client=_FakeEmbedder(),
        start=False,
    )
    try:
        job = sched.get_job("rank:drain")
        assert job is not None
        job.func()
        assert job.trigger.interval.total_seconds() == 37
    finally:
        sched.shutdown(wait=False)

    assert seen["drain"]["batch_size"] == 11
    assert seen["drain"]["remote_daily_cap"] == 223


# --- Review 2026-10-26 H2: a context-window overflow must stop retrying ---


@pytest.mark.parametrize(
    ("finish_reason", "content"),
    [
        ("length", ""),  # thinking filled the window; content empty
        ("stop", ""),  # same reply, server reported a clean stop
        ("length", '{"topic_fit": {"rat'),  # the 205-of-541 truncation shape
    ],
)
def test_a_context_window_overflow_is_a_parse_failure_and_stops_being_re_enqueued(
    pg_session_factory, finish_reason, content
):
    """Red 4, end to end. A reply Gemma cut off is deterministic for these
    inputs, so the job must dead-letter as `parse` -- which enqueue_stale_pairs
    suppresses -- and not as `service`, which it never suppresses.

    Before the fix, 541 jobs and 2705 Gemma calls were burned re-ranking the
    same 15 pairs every night (review 2026-10-26 H2)."""
    with pg_session_factory() as s:
        p = make_profile(s, description="Liver disease", structured={"areas": ["AI"]})
        c = make_call(s)
        enqueue_job(s, call_id=c.id, profile_id=p.id)
        s.commit()
        call_id, profile_id = c.id, p.id

    client = ollama_context_overflow(finish_reason=finish_reason, content=content)
    for _ in range(MAX_ATTEMPTS):
        drain_queue(
            session_factory=pg_session_factory,
            client=client,
            embedding_threshold=0.0,
            batch_size=10,
        )

    ctx = ctx_for(client)
    with pg_session_factory() as s:
        job = s.execute(
            select(RankingJob).where(
                RankingJob.call_id == call_id, RankingJob.profile_id == profile_id
            )
        ).scalar_one()
        assert job.status == RankingJobStatus.FAILED, job.status
        assert job.attempts == MAX_ATTEMPTS, job.attempts
        assert job.failure_kind == "parse", (
            f"failure_kind={job.failure_kind!r}: a 'service' row is re-enqueued "
            "by every ingest and every 04:00 reconcile, forever"
        )
        # Suppression reads all three stamps; a partial set never matches.
        assert job.profile_fingerprint and job.call_fingerprint and job.scoring_fingerprint
        # An operator reading last_error must learn which knob to raise.
        assert "OLLAMA_CONTEXT_LENGTH" in (job.last_error or ""), job.last_error

        # The pair is not re-enqueued while the inputs are unchanged...
        assert enqueue_stale_pairs(s, ctx=ctx) == 0, "the same inputs were re-enqueued"
        # ...but suppression is keyed on the INPUTS, not the pair: without this
        # control, a pair that can never be enqueued at all passes the line
        # above. (And nothing ranked it, so no CallRanking makes it fresh.)
        assert s.execute(select(CallRanking)).scalar_one_or_none() is None
        s.get(Call, call_id).summary = "The funder rewrote the scope."
        s.flush()
        assert enqueue_stale_pairs(s, ctx=ctx) == 1, "a CHANGED call must still be re-enqueued"
        s.rollback()
