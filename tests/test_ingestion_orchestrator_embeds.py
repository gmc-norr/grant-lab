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

import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock

from sqlalchemy import select

from grant_lab.auth.email import InMemoryEmailSender
from grant_lab.db.models import Call, RankingJob
from grant_lab.embeddings.client import EmbedResult
from grant_lab.ingestion.alerts import ConsecutiveFailureTracker
from grant_lab.ingestion.orchestrator import run_ingestion
from grant_lab.profiles.fingerprint import call_embedding_fingerprint
from grant_lab.ranking.ollama_client import LLMError
from grant_lab.sources.types import NormalizedCall
from tests.freshness_helpers import ctx_for


def _embed_result(vec: list[float]) -> EmbedResult:
    return EmbedResult(vector=vec, embedded_chars=1)


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
        c.embed.return_value = _embed_result(vec or [0.1] * 1024)
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

    def _capture_then_embed(text, **_kw):
        with pg_session_factory() as s:
            n = s.execute(select(RankingJob)).scalars().all()
        job_counts_at_embed_time.append(len(n))
        return real_embed

    embed_client.embed.side_effect = _capture_then_embed

    run_ingestion(
        freshness=ctx_for(),
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


def test_orchestrator_skips_re_embedding_a_call_whose_fingerprint_is_current(
    pg_session_factory,
    profile_factory,
):
    """Flipped from test_orchestrator_re_embeds_touched_calls_even_when_already_present,
    whose docstring read "No 'unchanged call' optimization until we have a content-hash
    mechanism." The mechanism is `calls.embedding_fingerprint` (migration 0080): it
    hashes build_call_text's OUTPUT and refresh_call_embedding stamps it alongside the
    vector, so re-ingesting a byte-identical call is now a no-op for the embedder.
    Live evidence for why: 419 `POST /api/embed` for 0 inserts in one night's log.

    Pins three things, not just the count:

      - the FIRST run really embeds (count reaches 1, vector + model + fingerprint
        land on the row). Without this, "1 embed for 3 runs" would also be satisfied
        by an orchestrator that never embeds at all.
      - runs 2 and 3 add no embed call.
      - the skip writes NOTHING. `call_rank_fingerprint` hashes a call's
        `embedding_fingerprint` and `embedding_model`, so the skip path must leave
        those columns exactly as it found them rather than rewrite them with equal
        values.
    """
    profile_factory(name="Group Y")
    call = NormalizedCall(
        external_id="EM-RE",
        url="https://x",
        title="Same call",
        published_at=datetime.now(UTC),
    )
    embed_client = _embed_client()

    def _run():
        run_ingestion(
            freshness=ctx_for(),
            session_factory=pg_session_factory,
            adapter=_StaticAdapter([call]),
            alerter=_alerter(),
            embed_client=embed_client,
        )

    _run()

    # Precondition, so the skip assertion below cannot pass vacuously.
    assert embed_client.embed.call_count == 1
    with pg_session_factory() as s:
        c = s.execute(select(Call).where(Call.external_id == "EM-RE")).scalar_one()
        assert c.embedding is not None
        assert c.embedding_model == "bge-m3"
        assert c.embedding_fingerprint == call_embedding_fingerprint(c)
        before = (list(c.embedding), c.embedding_model, c.embedding_fingerprint)

    _run()
    _run()

    # Three identical ingest runs -> ONE embed.
    assert embed_client.embed.call_count == 1

    # And the two skipped runs wrote nothing to the row's embedding triple.
    with pg_session_factory() as s:
        c = s.execute(select(Call).where(Call.external_id == "EM-RE")).scalar_one()
        after = (list(c.embedding), c.embedding_model, c.embedding_fingerprint)
    assert after == before


def test_orchestrator_re_embeds_a_call_whose_text_changed_exactly_once(
    pg_session_factory,
    profile_factory,
):
    """The skip must not become a never-embed. A call whose embedded text changed
    re-embeds EXACTLY once: the changed ingest embeds, and the next identical
    ingest does not embed again."""
    profile_factory(name="Group Y2")
    published = datetime.now(UTC)

    embed_client = _embed_client()

    def _run(summary: str):
        run_ingestion(
            freshness=ctx_for(),
            session_factory=pg_session_factory,
            adapter=_StaticAdapter(
                [
                    NormalizedCall(
                        external_id="EM-CHANGED",
                        url="https://x",
                        title="Changing call",
                        summary=summary,
                        published_at=published,
                    )
                ]
            ),
            alerter=_alerter(),
            embed_client=embed_client,
        )

    def _fingerprint() -> str:
        with pg_session_factory() as s:
            c = s.execute(select(Call).where(Call.external_id == "EM-CHANGED")).scalar_one()
            assert c.embedding_fingerprint == call_embedding_fingerprint(c)
            return c.embedding_fingerprint

    _run("Original summary.")
    assert embed_client.embed.call_count == 1
    first = _fingerprint()

    # Text changed -> the fingerprint no longer describes the row -> re-embed,
    # once.
    _run("Rewritten summary, materially different.")
    assert embed_client.embed.call_count == 2
    second = _fingerprint()
    assert second != first

    # Same changed text again -> no third embed.
    _run("Rewritten summary, materially different.")
    assert embed_client.embed.call_count == 2
    assert _fingerprint() == second


def test_orchestrator_re_embeds_when_the_embedding_model_changed(
    pg_session_factory,
    profile_factory,
):
    """The dangerous condition of the three.

    `embedding_fingerprint` hashes build_call_text's output, which is TEXT ONLY.
    Swapping EMBEDDING_MODEL replaces every vector in the corpus while leaving
    that hash byte-identical -- this project has already shipped exactly that
    staleness once (the bge-m3 swap). So the fingerprint alone cannot carry
    freshness: the skip must also compare `embedding_model` against the model the
    client would use now.

    The last assertion is the point: the fingerprint is UNCHANGED across the swap,
    so a fingerprint-only skip would have declared this row current forever.
    """
    profile_factory(name="Group Y3")
    call = NormalizedCall(
        external_id="EM-MODEL",
        url="https://x",
        title="Model swap",
        published_at=datetime.now(UTC),
    )

    def _run(client):
        run_ingestion(
            freshness=ctx_for(),
            session_factory=pg_session_factory,
            adapter=_StaticAdapter([call]),
            alerter=_alerter(),
            embed_client=client,
        )

    old_client = _embed_client(vec=[0.1] * 1024)
    assert old_client.model == "bge-m3"
    _run(old_client)
    assert old_client.embed.call_count == 1
    with pg_session_factory() as s:
        c = s.execute(select(Call).where(Call.external_id == "EM-MODEL")).scalar_one()
        assert c.embedding_model == "bge-m3"
        fingerprint_before = c.embedding_fingerprint

    # Same call, same text, DIFFERENT embedding model.
    new_client = _embed_client(vec=[0.9] * 1024)
    new_client.model = "mxbai-embed-large"
    _run(new_client)

    assert new_client.embed.call_count == 1
    with pg_session_factory() as s:
        c = s.execute(select(Call).where(Call.external_id == "EM-MODEL")).scalar_one()
        assert c.embedding_model == "mxbai-embed-large"
        assert list(c.embedding) == [0.9] * 1024
        # The text never changed, so neither did the fingerprint. Only the model
        # comparison could have caught this.
        assert c.embedding_fingerprint == fingerprint_before


def test_orchestrator_counts_the_calls_it_skipped_because_they_were_current(
    pg_session_factory,
    profile_factory,
    caplog,
):
    """The skip must be VISIBLE (review 2026-10-26, finding 3).

    The defect the skip closed was found in a log: 419 `POST /api/embed` for 0
    inserts in one night. After the fix a healthy night produces zero embeds —
    and so does a step 2 that silently stopped running. `inserted`/`updated`
    cannot tell those apart, so `IngestionResult` and the `ingestion.ok` record
    carry `calls_skipped_current`.

    Discriminating on the NUMBER, in both directions, over three runs of the
    same three calls:

      - run 1 embeds all three and skips 0. A key that merely exists, or a
        counter hard-wired to len(touched_call_ids), fails here.
      - run 2 embeds none and skips 3. A counter hard-wired to 0 fails here.
      - run 3 changes ONE call's text: 1 embed, 2 skips. So the count tracks
        the rows actually skipped, not the size of the batch.

    The last assertion is about diagnosability rather than the counter: step 2
    SWALLOWS a per-call embed failure by design, so a transient DB/Ollama error
    would otherwise show up here as an unexplained count mismatch. Asserting
    that no `ingestion_embedding_failed` / `ingestion_enqueue_failed` record was
    emitted makes the counts provably about the skip.
    """
    profile_factory(name="Group Skipped")
    published = datetime.now(UTC)
    embed_client = _embed_client()
    logger_name = "grant_lab.ingestion.orchestrator"

    def _run(first_summary: str):
        summaries = {"EM-SK-1": first_summary, "EM-SK-2": "two", "EM-SK-3": "three"}
        return run_ingestion(
            freshness=ctx_for(),
            session_factory=pg_session_factory,
            adapter=_StaticAdapter(
                [
                    NormalizedCall(
                        external_id=xid,
                        url=f"https://x/{xid}",
                        title=f"Skippable {xid}",
                        summary=summary,
                        published_at=published,
                    )
                    for xid, summary in summaries.items()
                ]
            ),
            alerter=_alerter(),
            embed_client=embed_client,
        )

    with caplog.at_level(logging.INFO, logger=logger_name):
        # Run 1: nothing is current yet, so everything is embedded and nothing
        # is skipped. Without this the "3 skipped" below would also be satisfied
        # by an orchestrator that never embeds at all.
        first = _run("one")
        assert embed_client.embed.call_count == 3
        assert first.calls_skipped_current == 0

        # Run 2: byte-identical feed -> no new embeds, three skips.
        second = _run("one")
        assert embed_client.embed.call_count == 3
        assert second.calls_skipped_current == 3

        # Run 3: one call's text changed -> exactly one embed, and the skip
        # count drops to the two rows that really were current.
        third = _run("one, rewritten")
        assert embed_client.embed.call_count == 4
        assert third.calls_skipped_current == 2

    # The count reaches the operator in the one place the original defect was
    # visible: the per-run `ingestion.ok` record.
    ok_records = [r for r in caplog.records if getattr(r, "event", None) == "ingestion_ok"]
    assert [r.calls_skipped_current for r in ok_records] == [0, 3, 2]
    assert [(r.inserted, r.updated) for r in ok_records] == [(3, 0), (0, 3), (0, 3)]

    # Nothing was swallowed: the zero embeds in run 2 are the skip, not an error.
    swallowed = [
        getattr(r, "event", None)
        for r in caplog.records
        if getattr(r, "event", None) in {"ingestion_embedding_failed", "ingestion_enqueue_failed"}
    ]
    assert swallowed == []


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
        freshness=ctx_for(),
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
        freshness=ctx_for(),
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
    from grant_lab.sources.types import AdapterError

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
        freshness=ctx_for(),
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

    # Force step 3's bulk_enqueue_for_calls to raise a DB-shaped error. We
    # patch the binding *inside* the orchestrator module (where it's looked up
    # at call time) — patching grant_lab.ranking.queue.bulk_enqueue_for_calls
    # would not affect the already-imported reference in orchestrator.
    def _boom(*_a, **_kw):
        raise RuntimeError("simulated step-3 DB failure")

    import grant_lab.ingestion.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "bulk_enqueue_for_calls", _boom)

    alerter = MagicMock()

    run_ingestion(
        freshness=ctx_for(),
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
