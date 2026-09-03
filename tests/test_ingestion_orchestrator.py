from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from call_manager.auth.email import InMemoryEmailSender
from call_manager.db.models import Call, Source
from call_manager.embeddings.client import EmbedResult
from call_manager.ingestion.alerts import ConsecutiveFailureTracker
from call_manager.ingestion.orchestrator import run_ingestion
from call_manager.sources.types import AdapterError, NormalizedCall


def _embed_client():
    """Per-test embed client mock — returns a usable model name and a fixed
    1024-dim vector. Tests that care about embed-step behavior build their
    own; the rest just need this to satisfy the required parameter."""
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


class _FailingAdapter:
    slug = "vinnova"

    def fetch_new_calls(self, since):
        yield NormalizedCall(external_id="P-1", url="https://example.org/u", title="T")  # one good
        raise AdapterError("network blip mid-stream")


def _alerter(sender):
    return ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email="root@umu.se")


def test_successful_run_persists_and_advances_timestamp(pg_session_factory):
    sender = InMemoryEmailSender()
    adapter = _StaticAdapter(
        [
            NormalizedCall(external_id="V-1", url="https://example.org/u1", title="One"),
            NormalizedCall(external_id="V-2", url="https://example.org/u2", title="Two"),
        ]
    )

    result = run_ingestion(
        session_factory=pg_session_factory,
        adapter=adapter,
        alerter=_alerter(sender),
        embed_client=_embed_client(),
    )
    assert result.inserted == 2
    assert result.updated == 0
    assert result.error is None

    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        assert src.last_fetched_at is not None
        n = s.execute(select(Call).where(Call.source_id == src.id)).scalars().all()
    assert len(n) == 2


def test_second_run_updates_existing_and_inserts_new(pg_session_factory):
    sender = InMemoryEmailSender()
    a1 = _StaticAdapter(
        [NormalizedCall(external_id="V-1", url="https://example.org/u", title="OLD")]
    )
    run_ingestion(
        session_factory=pg_session_factory,
        adapter=a1,
        alerter=_alerter(sender),
        embed_client=_embed_client(),
    )

    a2 = _StaticAdapter(
        [
            NormalizedCall(external_id="V-1", url="https://example.org/u", title="NEW"),
            NormalizedCall(external_id="V-2", url="https://example.org/u", title="Brand new"),
        ]
    )
    result = run_ingestion(
        session_factory=pg_session_factory,
        adapter=a2,
        alerter=_alerter(sender),
        embed_client=_embed_client(),
    )
    assert result.inserted == 1
    assert result.updated == 1


def test_failure_rolls_back_and_does_not_advance_timestamp(pg_session_factory):
    sender = InMemoryEmailSender()
    adapter = _FailingAdapter()

    # Snapshot timestamp before
    with pg_session_factory() as s:
        before = s.execute(
            select(Source.last_fetched_at).where(Source.slug == "vinnova")
        ).scalar_one()

    result = run_ingestion(
        session_factory=pg_session_factory,
        adapter=adapter,
        alerter=_alerter(sender),
        embed_client=_embed_client(),
    )
    assert result.error is not None
    assert "network blip" in result.error

    # Timestamp unchanged
    with pg_session_factory() as s:
        after = s.execute(
            select(Source.last_fetched_at).where(Source.slug == "vinnova")
        ).scalar_one()
    assert after == before  # both None on first run

    # Partial inserts rolled back
    with pg_session_factory() as s:
        n = s.execute(select(Call).where(Call.external_id == "P-1")).scalars().all()
    assert n == []


def test_three_consecutive_failures_alert_admin(pg_session_factory):
    sender = InMemoryEmailSender()
    alerter = _alerter(sender)
    adapter = _FailingAdapter()
    for _ in range(3):
        run_ingestion(
            session_factory=pg_session_factory,
            adapter=adapter,
            alerter=alerter,
            embed_client=_embed_client(),
        )
    assert len(sender.sent) == 1
    assert "vinnova" in sender.sent[0].subject


def test_unknown_source_slug_raises(pg_session_factory):
    sender = InMemoryEmailSender()

    class _BadSlug:
        slug = "no_such_source"

        def fetch_new_calls(self, since):
            return iter([])

    with pytest.raises(LookupError):
        run_ingestion(
            session_factory=pg_session_factory,
            adapter=_BadSlug(),
            alerter=_alerter(sender),
            embed_client=_embed_client(),
        )


def test_orchestrator_enqueues_one_job_per_active_profile(pg_session_factory, profile_factory):
    """The orchestrator enqueues a ranking job for every active profile per
    touched call."""
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from sqlalchemy import select

    from call_manager.db.models import RankingJob

    pid_a, _ = profile_factory(name="P-A")
    pid_b, _ = profile_factory(name="P-B")

    class _FakeAdapter:
        slug = "vinnova"

        def fetch_new_calls(self, since):
            yield NormalizedCall(
                external_id="X-1",
                url="https://x",
                title="X",
                published_at=datetime.now(UTC),
            )

    run_ingestion(
        session_factory=pg_session_factory,
        adapter=_FakeAdapter(),
        alerter=MagicMock(),
        embed_client=_embed_client(),
    )

    with pg_session_factory() as s:
        jobs = s.execute(select(RankingJob)).scalars().all()
    profile_ids = {j.profile_id for j in jobs}
    assert pid_a in profile_ids
    assert pid_b in profile_ids
    assert len(jobs) == 2
