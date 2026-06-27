from unittest.mock import MagicMock

from sqlalchemy import select

from call_manager.auth.email import InMemoryEmailSender
from call_manager.db.models import Call, Profile, RankingJob
from call_manager.embeddings.client import EmbedResult
from call_manager.ingestion.alerts import ConsecutiveFailureTracker
from call_manager.ingestion.orchestrator import run_ingestion
from call_manager.sources.types import AdapterError, NormalizedCall


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


def test_successful_ingest_enqueues_one_job_per_active_profile(pg_session_factory, profile_factory):
    p_a, _ = profile_factory(name="Group A")
    p_b, _ = profile_factory(name="Group B")
    sender = InMemoryEmailSender()
    alerter = ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email=None)
    adapter = _StaticAdapter(
        [
            NormalizedCall(external_id="V-1", url="https://x/1", title="One"),
            NormalizedCall(external_id="V-2", url="https://x/2", title="Two"),
        ]
    )

    run_ingestion(
        session_factory=pg_session_factory,
        adapter=adapter,
        alerter=alerter,
        embed_client=_embed_client(),
    )

    with pg_session_factory() as s:
        jobs = s.execute(select(RankingJob)).scalars().all()
        # 2 calls × 2 profiles = 4 jobs
        assert len(jobs) == 4
        pairs = sorted((j.call_id, j.profile_id) for j in jobs)
        expected_pairs = sorted(
            [
                (s.execute(select(Call.id).where(Call.external_id == eid)).scalar_one(), pid)
                for eid in ("V-1", "V-2")
                for pid in (p_a, p_b)
            ]
        )
    assert pairs == expected_pairs


def test_inactive_profile_does_not_get_jobs(pg_session_factory, profile_factory):
    p_active, _ = profile_factory(name="Active")
    p_inactive, _ = profile_factory(name="Inactive")
    with pg_session_factory() as s:
        s.get(Profile, p_inactive).is_active = False
        s.commit()
    sender = InMemoryEmailSender()
    alerter = ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email=None)
    adapter = _StaticAdapter([NormalizedCall(external_id="V-1", url="https://x", title="T")])
    run_ingestion(
        session_factory=pg_session_factory,
        adapter=adapter,
        alerter=alerter,
        embed_client=_embed_client(),
    )

    with pg_session_factory() as s:
        jobs = s.execute(select(RankingJob)).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].profile_id == p_active


def test_ingestion_failure_does_not_enqueue_jobs(pg_session_factory, profile_factory):
    profile_factory()
    sender = InMemoryEmailSender()
    alerter = ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email=None)

    class _Failing:
        slug = "vinnova"

        def fetch_new_calls(self, since):
            yield NormalizedCall(external_id="X", url="https://x", title="T")
            raise AdapterError("boom")

    run_ingestion(
        session_factory=pg_session_factory,
        adapter=_Failing(),
        alerter=alerter,
        embed_client=_embed_client(),
    )
    with pg_session_factory() as s:
        jobs = s.execute(select(RankingJob)).scalars().all()
    assert jobs == []
