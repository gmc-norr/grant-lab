"""Codex H1 ordering tests: the orchestrator must embed touched calls
BEFORE enqueuing ranking jobs.

Why this matters: the v2 ranking worker drains the queue every 60 s. If the
embedding for a call were still NULL when its job got picked up, the
embedding pre-gate would silently fall through to the LLM with
`embed_similarity = NULL` and never recover (no daily back-pressure
re-ranks DONE jobs). So the orchestrator runs three serialized steps:

  1. upsert calls + advance source timestamp (one transaction)
  2. embed touched calls (per-row transaction, best-effort)
  3. enqueue ranking jobs (one transaction)

These tests pin that ordering down by inspecting the DB at strategic points.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from sqlalchemy import select

from call_manager.auth.email import InMemoryEmailSender
from call_manager.db.models import Call, RankingJob
from call_manager.ingestion.alerts import ConsecutiveFailureTracker
from call_manager.ingestion.orchestrator import run_ingestion
from call_manager.ranking.ollama_client import LLMError
from call_manager.sources.types import NormalizedCall


def _alerter():
    return ConsecutiveFailureTracker(threshold=3, sender=InMemoryEmailSender(), admin_email=None)


class _StaticAdapter:
    slug = "vinnova"

    def __init__(self, calls):
        self._calls = list(calls)

    def fetch_new_calls(self, since):
        yield from self._calls


def _embed_client(*, vec=None, raises=None):
    """Build a MagicMock embed client. `vec` controls what `.embed()` returns;
    `raises` (an Exception instance) makes `.embed()` raise instead."""
    c = MagicMock()
    c.model = "bge-m3"
    if raises is not None:
        c.embed.side_effect = raises
    else:
        c.embed.return_value = vec or [0.1] * 1024
    return c


def test_orchestrator_embeds_touched_calls_before_enqueueing_ranking_jobs(
    pg_session_factory,
    profile_factory,
):
    """Order is critical (Codex H1): when the v2 worker picks up a job,
    the call embedding must already be in place. We assert both the
    embedding and the ranking job exist after the orchestrator returns,
    AND that the embedding was written BEFORE the job was enqueued by
    using a side-effect that snapshots state at embed-call time."""
    profile_factory(name="Group X")
    adapter = _StaticAdapter(
        [
            NormalizedCall(
                external_id="EM-1",
                url="https://x/1",
                title="Alpha",
                published_at=datetime.now(UTC),
            ),
            NormalizedCall(
                external_id="EM-2",
                url="https://x/2",
                title="Beta",
                published_at=datetime.now(UTC),
            ),
        ]
    )
    embed_client = _embed_client(vec=[0.42] * 1024)

    # Snapshot RankingJob count at the moment each embed is performed —
    # MUST be 0 each time (jobs are step 3, embeds are step 2).
    job_counts_at_embed_time: list[int] = []
    real_embed = embed_client.embed.return_value

    def _capture_then_embed(text):
        with pg_session_factory() as s:
            n = s.execute(select(RankingJob)).scalars().all()
        job_counts_at_embed_time.append(len(n))
        return real_embed

    embed_client.embed.side_effect = _capture_then_embed

    run_ingestion(
        session_factory=pg_session_factory,
        adapter=adapter,
        alerter=_alerter(),
        embed_client=embed_client,
    )

    # Two calls → two embed calls → two snapshots. Each snapshot must show
    # zero ranking jobs (we're still in step 2, not step 3).
    assert job_counts_at_embed_time == [0, 0]

    # After the run: both calls have an embedding AND there are 2 ranking jobs.
    with pg_session_factory() as s:
        calls = s.execute(select(Call).order_by(Call.external_id)).scalars().all()
        jobs = s.execute(select(RankingJob)).scalars().all()
    assert len(calls) == 2
    for c in calls:
        assert c.embedding is not None
        assert c.embedding_model == "bge-m3"
    assert len(jobs) == 2  # 2 calls × 1 active profile


def test_orchestrator_re_embeds_touched_calls_even_when_already_present(
    pg_session_factory,
    profile_factory,
):
    """Codex Open-Q2: upsert always UPDATEs on conflict, so we always re-embed.
    No 'unchanged call' optimization until we have a content-hash mechanism."""
    profile_factory(name="Group Y")
    call = NormalizedCall(
        external_id="EM-RE",
        url="https://x",
        title="Same call",
        published_at=datetime.now(UTC),
    )
    embed_client = _embed_client()

    # Run twice with the same NormalizedCall payload.
    run_ingestion(
        session_factory=pg_session_factory,
        adapter=_StaticAdapter([call]),
        alerter=_alerter(),
        embed_client=embed_client,
    )
    run_ingestion(
        session_factory=pg_session_factory,
        adapter=_StaticAdapter([call]),
        alerter=_alerter(),
        embed_client=embed_client,
    )

    # Two ingest runs → two embeds. No skip-unchanged.
    assert embed_client.embed.call_count == 2


def test_orchestrator_continues_enqueue_when_embed_fails(
    pg_session_factory,
    profile_factory,
):
    """Embedding service down → embed fails → ranking job still enqueued
    (gate UNKNOWN, LLM runs as the safe fallback). Best-effort embed step
    must not block the enqueue."""
    profile_factory(name="Group Z")
    adapter = _StaticAdapter(
        [
            NormalizedCall(
                external_id="EM-FAIL",
                url="https://x",
                title="Fails to embed",
                published_at=datetime.now(UTC),
            ),
        ]
    )
    embed_client = _embed_client(raises=LLMError("ollama is down"))

    run_ingestion(
        session_factory=pg_session_factory,
        adapter=adapter,
        alerter=_alerter(),
        embed_client=embed_client,
    )

    # Call exists; embedding is NULL (refresh helper returned False);
    # ranking job is still enqueued so the worker can drain it.
    with pg_session_factory() as s:
        c = s.execute(select(Call).where(Call.external_id == "EM-FAIL")).scalar_one()
        jobs = s.execute(select(RankingJob)).scalars().all()
    assert c.embedding is None
    assert len(jobs) == 1


def test_orchestrator_continues_when_embed_raises_unexpected_exception(
    pg_session_factory,
    profile_factory,
):
    """A non-LLMError exception inside the embed step (e.g. a transient
    DB hiccup on the embed-only transaction) must also not block the
    enqueue step. The orchestrator catches broadly with logging.exception."""
    profile_factory(name="Group W")
    adapter = _StaticAdapter(
        [
            NormalizedCall(
                external_id="EM-BOOM",
                url="https://x",
                title="Boom embed",
                published_at=datetime.now(UTC),
            ),
        ]
    )
    embed_client = _embed_client(raises=RuntimeError("unexpected"))

    run_ingestion(
        session_factory=pg_session_factory,
        adapter=adapter,
        alerter=_alerter(),
        embed_client=embed_client,
    )

    # Same contract as the LLMError case: enqueue still happens.
    with pg_session_factory() as s:
        jobs = s.execute(select(RankingJob)).scalars().all()
    assert len(jobs) == 1


def test_orchestrator_step1_failure_skips_embed_and_enqueue(
    pg_session_factory,
    profile_factory,
):
    """If step 1 (upsert + timestamp advance) fails, neither step 2 nor
    step 3 runs. We assert via a non-zero embed call count check —
    the embed client should never be invoked."""
    from call_manager.sources.types import AdapterError

    profile_factory(name="Group V")

    class _FailingAdapter:
        slug = "vinnova"

        def fetch_new_calls(self, since):
            yield NormalizedCall(
                external_id="EM-PARTIAL",
                url="https://x",
                title="One good",
                published_at=datetime.now(UTC),
            )
            raise AdapterError("network blip mid-stream")

    embed_client = _embed_client()

    run_ingestion(
        session_factory=pg_session_factory,
        adapter=_FailingAdapter(),
        alerter=_alerter(),
        embed_client=embed_client,
    )

    # Step 1 rolled back → no calls, no embed calls, no jobs.
    assert embed_client.embed.call_count == 0
    with pg_session_factory() as s:
        calls = s.execute(select(Call)).scalars().all()
        jobs = s.execute(select(RankingJob)).scalars().all()
    assert calls == []
    assert jobs == []


def test_orchestrator_step3_failure_invokes_alerter(
    pg_session_factory,
    profile_factory,
    monkeypatch,
):
    """Codex regression-guard: if step 3 (enqueue ranking jobs) fails
    (e.g. DB constraint violation, OperationalError), the orchestrator
    must call alerter.record_failure with a clear error message. Without
    the wrapping try/except added in the bug-review fix, the exception
    would propagate out of run_ingestion, bypassing the alerter entirely
    and crashing the scheduler tick."""
    profile_factory(name="Group Q")
    adapter = _StaticAdapter(
        [
            NormalizedCall(
                external_id="EM-Q",
                url="https://x",
                title="Step3 boom",
                published_at=datetime.now(UTC),
            ),
        ]
    )
    embed_client = _embed_client()

    # Force step 3's enqueue_for_pair to raise a DB-shaped error. We patch
    # the binding *inside* the orchestrator module (where it's looked up at
    # call time) — patching call_manager.ranking.queue.enqueue_for_pair
    # would not affect the already-imported reference in orchestrator.
    def _boom(*_a, **_kw):
        raise RuntimeError("simulated step-3 DB failure")

    import call_manager.ingestion.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "enqueue_for_pair", _boom)

    alerter = MagicMock()

    run_ingestion(
        session_factory=pg_session_factory,
        adapter=adapter,
        alerter=alerter,
        embed_client=embed_client,
    )

    # Alerter must have been invoked — exactly once, with the source slug
    # and an error message that mentions the step-3 failure context so
    # operators can tell this apart from a step-1 adapter failure.
    alerter.record_failure.assert_called_once()
    args, kwargs = alerter.record_failure.call_args
    assert "vinnova" in args or kwargs.get("source") == "vinnova" or args[0] == "vinnova"
    err = kwargs.get("error", "")
    assert "step 3" in err and "simulated step-3 DB failure" in err
    # And record_success must NOT have been called.
    alerter.record_success.assert_not_called()

    # Step 1 + 2 still happened: call exists with embedding; no jobs queued.
    with pg_session_factory() as s:
        c = s.execute(select(Call).where(Call.external_id == "EM-Q")).scalar_one()
        jobs = s.execute(select(RankingJob)).scalars().all()
    assert c.embedding is not None
    assert jobs == []
