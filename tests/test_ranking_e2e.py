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
from grant_lab.embeddings.client import EmbedResult
from grant_lab.ranking.ollama_client import OllamaClient
from grant_lab.ranking.worker import drain_queue as _drain_queue
from tests.freshness_helpers import ctx_for

# drain_queue now requires embedding_threshold (no module-level default).
# Mirrors Settings.embedding_pregate_threshold default of 0.30.
drain_queue = partial(_drain_queue, embedding_threshold=0.30)


def _ollama_canned():
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
                                "rating": 5,
                                "rationale": "perfect AI match",
                                "quoted_evidence": "AI for medical imaging",
                            },
                            "eligibility": {
                                "rating": "pass",
                                "rationale": "research groups eligible",
                                "quoted_evidence": None,
                            },
                            "exclusions": {
                                "rating": "pass",
                                "rationale": "no exclusions",
                                "quoted_evidence": None,
                            },
                        }
                    ),
                },
                "logprobs": {
                    "content": [
                        {
                            "token": "5",
                            "logprob": -0.05,
                            "top_logprobs": [
                                {"token": "5", "logprob": -0.05},
                                {"token": "4", "logprob": -3.0},
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


def test_e2e_pipeline(pg_session_factory):
    """Enqueue a v2 job → drain → verify CallRanking + the 3 LLM criteria rows
    (topic_fit + eligibility + exclusions) + score 100. No timeline row is
    written (ranking spec D8)."""
    with pg_session_factory() as s:
        u = User(email="e2e@umu.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="E2E",
            kind=ProfileKind.ORGANIZATIONAL_UNIT,
            description="",
            structured={
                "areas": ["AI for medical imaging"],
                "must_include": [],
                "hard_exclusions": [],
                "budget_min": None,
                "budget_max": None,
                "trl_min": None,
                "trl_max": None,
            },
            created_by=u.id,
        )
        s.add(p)
        s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id="E2E",
            url="https://x",
            title="AI for medical imaging — call",
            summary="Open call for AI-driven medical imaging research.",
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
        s.commit()

    drain_queue(session_factory=pg_session_factory, client=_ollama_canned(), batch_size=10)

    with pg_session_factory() as s:
        ranking = s.execute(select(CallRanking)).scalar_one()
        criteria = s.execute(select(CallRankingCriterion)).scalars().all()

    # The fixture's single-token logprobs block can't offset-bind to the
    # structural rating literal, so EV falls back to argmax: 5 × 20 = 100.
    assert ranking.aggregate_score >= 99
    assert ranking.aggregate_reason is None
    assert ranking.prompt_version == "v5.0"

    by_name = {c.criterion: c for c in criteria}
    assert set(by_name.keys()) >= {"topic_fit", "eligibility", "exclusions"}
    assert by_name["topic_fit"].rating == "5"
    assert by_name["topic_fit"].quoted_evidence == "AI for medical imaging"
    assert by_name["eligibility"].rating == "pass"


# ---------------------------------------------------------------------------
# Plan D — full ingest → embed → enqueue → drain → DB e2e tests.
#
# Each link of the chain is unit-tested in isolation elsewhere; these tests
# pin down that the wiring between them actually works end-to-end. The two
# tests together exercise both halves of the embedding-pre-gate fork: low
# cosine short-circuits before the LLM, high cosine falls through to the
# LLM.
# ---------------------------------------------------------------------------


def _ollama_topic_fit_5():
    """Build an Ollama mock returning topic_fit rating 5, all PASS — score 100.
    Reused across e2e tests where the LLM is expected to actually run."""
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
                                "rating": 5,
                                "rationale": "great match",
                                "quoted_evidence": "cancer biomarker",
                            },
                            "eligibility": {
                                "rating": "pass",
                                "rationale": "research groups eligible",
                                "quoted_evidence": None,
                            },
                            "exclusions": {
                                "rating": "pass",
                                "rationale": "no exclusions",
                                "quoted_evidence": None,
                            },
                        }
                    ),
                },
                "logprobs": {
                    "content": [
                        {
                            "token": "5",
                            "logprob": -0.05,
                            "top_logprobs": [
                                {"token": "5", "logprob": -0.05},
                                {"token": "4", "logprob": -3.0},
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


class _StaticAdapter:
    """Adapter test-double: yields a fixed list of NormalizedCalls."""

    slug = "vinnova"

    def __init__(self, calls):
        self._calls = list(calls)

    def fetch_new_calls(self, since):
        yield from self._calls


def _setup_profile(pg_session_factory, *, profile_embedding):
    """Create a User + active Profile with a pre-loaded embedding vector
    (so the orchestrator's enqueue step + worker's rank_pair see a
    profile that's already been embedded). Returns (profile_id, user_id)."""
    from grant_lab.db.models import Profile, ProfileKind, User

    with pg_session_factory() as s:
        u = User(email="e2e-pregate@umu.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="Cancer biomarker group",
            kind=ProfileKind.ORGANIZATIONAL_UNIT,
            description="Cancer biomarker research and digital pathology.",
            structured={
                "areas": ["cancer biomarker"],
                "must_include": [],
                "hard_exclusions": [],
                "budget_min": None,
                "budget_max": None,
                "trl_min": None,
                "trl_max": None,
            },
            created_by=u.id,
            embedding=profile_embedding,
            embedding_model="bge-m3",
        )
        s.add(p)
        s.commit()
        return p.id, u.id


def test_e2e_ingest_then_drain_short_circuits_when_embedding_dissimilar(
    pg_session_factory,
):
    """Full chain happy-path-of-the-FAIL-fork:
      orchestrator → embed_call → enqueue_job → worker drains
      → rank_pair → _safe_cosine ~0.05 → embedding gate FAIL
      → CallRanking row written with aggregate_score=0, reason='embedding'

    The LLM client must NEVER be invoked — that's the whole point of the
    embedding pre-gate.
    """
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from grant_lab.auth.email import InMemoryEmailSender
    from grant_lab.ingestion.alerts import ConsecutiveFailureTracker
    from grant_lab.ingestion.orchestrator import run_ingestion
    from grant_lab.sources.types import NormalizedCall

    # Profile vector: [1, 0, 0, ...]. Off-topic call vector: [0.05, ~1, ...] so
    # cosine ≈ 0.05 (well below the 0.30 default threshold → gate FAIL).
    profile_vec = [1.0] + [0.0] * 1023
    call_vec = [0.05, 0.999] + [0.0] * 1022

    _setup_profile(pg_session_factory, profile_embedding=profile_vec)

    adapter = _StaticAdapter(
        [
            NormalizedCall(
                external_id="OFF-1",
                url="https://x/off",
                title="Quantum dot synthesis methods",
                summary="Materials science call about photovoltaic quantum dots.",
                published_at=datetime.now(UTC),
            ),
        ]
    )

    embed_client = MagicMock()
    embed_client.model = "bge-m3"
    embed_client.embed.return_value = EmbedResult(vector=call_vec, embedded_chars=1)

    alerter = ConsecutiveFailureTracker(
        threshold=3,
        sender=InMemoryEmailSender(),
        admin_email=None,
    )

    # Step 1: ingest + embed + enqueue.
    ingest_result = run_ingestion(
        freshness=ctx_for(),
        session_factory=pg_session_factory,
        adapter=adapter,
        alerter=alerter,
        embed_client=embed_client,
    )
    assert ingest_result.error is None
    assert ingest_result.inserted == 1
    # The orchestrator's embed step must have written to the call row.
    embed_client.embed.assert_called()

    # Step 2: drain the v2 ranking queue. LLM client must NOT be invoked.
    llm_client = MagicMock()
    llm_client.model = "gemma4:e4b-it-q4_K_M"
    llm_client.chat.side_effect = AssertionError(
        "LLM should not be called: embedding pre-gate must short-circuit"
    )
    drain_queue(
        session_factory=pg_session_factory,
        client=llm_client,
        batch_size=10,
    )

    # Step 3: assert the persisted CallRanking reflects the gate FAIL.
    with pg_session_factory() as s:
        ranking = s.execute(select(CallRanking)).scalar_one()
        # Worker also marks the job DONE.
        jobs = (
            s.execute(select(RankingJob).where(RankingJob.status == RankingJobStatus.DONE))
            .scalars()
            .all()
        )
        # Criterion rows: at minimum the pre-LLM gates (budget + trl + embedding
        # FAIL) — embedding FAIL stops further evaluation.
        criteria = s.execute(select(CallRankingCriterion)).scalars().all()

    assert ranking.aggregate_score == 0
    assert ranking.aggregate_reason == "embedding"
    # Recorded similarity must be the cosine, ~0.05.
    assert ranking.embed_similarity is not None
    assert ranking.embed_similarity < 0.30
    assert ranking.embed_similarity > 0.0
    # Rationale summary surfaces the cosine to 3 decimals.
    assert "topic_dissimilar" in ranking.rationale
    assert len(jobs) == 1
    # No topic_fit / eligibility / exclusions row — LLM never ran.
    criterion_names = {c.criterion for c in criteria}
    assert "topic_fit" not in criterion_names
    assert "eligibility" not in criterion_names
    assert "exclusions" not in criterion_names
    # The LLM was indeed never invoked (proved above by side_effect raising
    # would have crashed; the worker would have caught it and dead-lettered,
    # so make doubly sure here):
    llm_client.chat.assert_not_called()


def test_e2e_ingest_then_drain_invokes_llm_when_embedding_similar(
    pg_session_factory,
):
    """Symmetric happy-path-of-the-PASS-fork: cosine well above 0.30 →
    embedding gate PASSes → LLM runs → CallRanking written with non-zero
    score AND embed_similarity recorded.
    """
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from grant_lab.auth.email import InMemoryEmailSender
    from grant_lab.ingestion.alerts import ConsecutiveFailureTracker
    from grant_lab.ingestion.orchestrator import run_ingestion
    from grant_lab.sources.types import NormalizedCall

    # Both vectors near-aligned → cosine ~0.95 → gate PASS.
    profile_vec = [1.0] + [0.0] * 1023
    call_vec = [0.95, 0.31] + [0.0] * 1022  # cosine ≈ 0.95

    _setup_profile(pg_session_factory, profile_embedding=profile_vec)

    adapter = _StaticAdapter(
        [
            NormalizedCall(
                external_id="ON-1",
                url="https://x/on",
                title="Cancer biomarker validation grant",
                summary="Open call for translational cancer biomarker research.",
                published_at=datetime.now(UTC),
            ),
        ]
    )

    embed_client = MagicMock()
    embed_client.model = "bge-m3"
    embed_client.embed.return_value = EmbedResult(vector=call_vec, embedded_chars=1)

    alerter = ConsecutiveFailureTracker(
        threshold=3,
        sender=InMemoryEmailSender(),
        admin_email=None,
    )

    run_ingestion(
        freshness=ctx_for(),
        session_factory=pg_session_factory,
        adapter=adapter,
        alerter=alerter,
        embed_client=embed_client,
    )

    # Drain with a real-looking LLM mock that returns topic_fit=5.
    llm_client = _ollama_topic_fit_5()
    drain_queue(
        session_factory=pg_session_factory,
        client=llm_client,
        batch_size=10,
    )

    with pg_session_factory() as s:
        ranking = s.execute(select(CallRanking)).scalar_one()
        criteria = s.execute(select(CallRankingCriterion)).scalars().all()

    # LLM ran → topic_fit 5 → aggregate ~100, reason None.
    assert ranking.aggregate_score >= 90
    assert ranking.aggregate_reason is None
    # Cosine recorded — even on the clean LLM path.
    assert ranking.embed_similarity is not None
    assert ranking.embed_similarity > 0.9
    # Per-criterion rows for both the gates AND the LLM rubric exist.
    criterion_names = {c.criterion for c in criteria}
    assert {"embedding", "topic_fit", "eligibility", "exclusions"} <= criterion_names


def test_e2e_a_failed_re_embed_does_not_let_the_old_vector_reject_new_text(
    pg_session_factory,
):
    """Beta review 2026-10-31 F3, through the real chain.

    Night 1: an off-topic call is embedded (orthogonal to the profile) and
    correctly gated to 0 without the LLM. Night 2: the source corrects the
    call's text to something on-topic, but re-embedding FAILS, so the row keeps
    its night-1 vector and fingerprint while the changed call is re-queued. The
    old vector describes text the call no longer has; it must not reject the
    new text. The LLM has to read it.
    """
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from grant_lab.auth.email import InMemoryEmailSender
    from grant_lab.ingestion.alerts import ConsecutiveFailureTracker
    from grant_lab.ingestion.orchestrator import run_ingestion
    from grant_lab.sources.types import NormalizedCall

    _setup_profile(pg_session_factory, profile_embedding=[1.0] + [0.0] * 1023)

    def ingest(title, summary, embed_client):
        return run_ingestion(
            freshness=ctx_for(),
            session_factory=pg_session_factory,
            adapter=_StaticAdapter(
                [
                    NormalizedCall(
                        external_id="FIX-1",
                        url="https://x/fix",
                        title=title,
                        summary=summary,
                        published_at=datetime.now(UTC),
                    )
                ]
            ),
            alerter=ConsecutiveFailureTracker(
                threshold=3, sender=InMemoryEmailSender(), admin_email=None
            ),
            embed_client=embed_client,
        )

    night1 = MagicMock()
    night1.model = "bge-m3"
    night1.embed.return_value = EmbedResult(vector=[0.0, 1.0] + [0.0] * 1022, embedded_chars=1)
    ingest("Quantum dot synthesis", "Photovoltaic materials.", night1)
    gate_only = MagicMock()
    gate_only.model = "gemma4:e4b-it-q4_K_M"
    gate_only.chat.side_effect = AssertionError("night 1 must be gated without the LLM")
    drain_queue(session_factory=pg_session_factory, client=gate_only, batch_size=10)
    with pg_session_factory() as s:
        assert s.execute(select(CallRanking.aggregate_reason)).scalar_one() == "embedding"

    night2 = MagicMock()
    night2.model = "bge-m3"
    night2.embed.side_effect = RuntimeError("ollama down")
    result = ingest(
        "Cancer biomarker validation grant", "Translational cancer biomarker research.", night2
    )
    assert result.error is None
    night2.embed.assert_called()  # it tried, and failed

    drain_queue(session_factory=pg_session_factory, client=_ollama_topic_fit_5(), batch_size=10)

    with pg_session_factory() as s:
        ranking = s.execute(select(CallRanking)).scalar_one()
    assert ranking.aggregate_reason is None, "the stale vector rejected the corrected call"
    assert ranking.aggregate_score >= 90
    assert ranking.embed_similarity is None
