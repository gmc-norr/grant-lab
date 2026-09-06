"""Task 9 gates: the scheduler carries the profile cursor across capped ticks.

Spec `docs/superpowers/specs/2026-09-06-matching-freshness-design.md`, change
5(b) second half: `_TICK_MAX_BATCHES` bounds the drain, and a tick that hits
that cap used to restart from `after_profile_id=0` tomorrow. Once the profile
scan was widened (change 5(a)), a long prefix of fresh profiles consumes the
whole allowance every day and a stale profile beyond it is never reached.

The spec asks for this to be pinned "with small `batch_size` and `max_batches`
values rather than a realistic corpus", so `tiny_tick` below drives the real
tick at batch_size=1 / max_batches=3 and the corpus is five profiles.
"""

from __future__ import annotations

import pytest

import grant_lab.scheduler as scheduler_mod
from grant_lab.config import Settings
from grant_lab.db.models import Profile
from grant_lab.embeddings.refresh import refresh_missing_embeddings_tick
from grant_lab.profiles.fingerprint import embedding_fingerprint
from tests.freshness_helpers import FakeEmbedder, make_profile


class _NullAlerter:
    def record_failure(self, *a, **kw):
        pass

    def record_success(self, *a, **kw):
        pass


@pytest.fixture()
def _settings_env(monkeypatch):
    """The env vars Settings() requires. The scheduler is built but never
    started, so no job fires except the one this test calls by hand."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")


@pytest.fixture()
def tiny_tick(monkeypatch):
    """Drive the scheduler's tick at batch_size=1, max_batches=3.

    The scheduler hardcodes batch_size=200 and takes the default
    max_batches=200, so reaching the cap for real would need a 40,000-row
    corpus. Only those two size knobs are overridden — the REAL
    `refresh_missing_embeddings_tick` still runs, against the real database,
    so the cursor argument the scheduler passes (or fails to pass) is what
    these tests measure.
    """

    def _small(**kwargs):
        kwargs["batch_size"] = 1
        kwargs["max_batches"] = 3
        return refresh_missing_embeddings_tick(**kwargs)

    monkeypatch.setattr(scheduler_mod, "refresh_missing_embeddings_tick", _small)


@pytest.fixture()
def fake_embedder():
    """One embedder shared by the seeding call and the scheduler's own job, so
    `embedding_model` matches across both and nothing is stale by accident."""
    return FakeEmbedder()


@pytest.fixture()
def embeddings_tick(pg_session_factory, fake_embedder, _settings_env, tiny_tick):
    """The scheduler's own `embeddings:refresh-missing` job function.

    Taken off the built scheduler rather than reimplemented, so the closure
    under test is the production one (the pattern
    tests/test_scheduler.py:423-436 uses for the collab checkpoint job).
    """
    from grant_lab.proposal_assist.drafting.factory import build_workspace_controller

    settings = Settings()
    controller = build_workspace_controller(settings=settings, session_factory=pg_session_factory)
    sched = scheduler_mod.build_scheduler(
        session_factory=pg_session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=fake_embedder,
        workspace_controller=controller,
        start=False,
    )
    job = sched.get_job("embeddings:refresh-missing")
    assert job is not None
    try:
        yield job.func
    finally:
        sched.shutdown(wait=False)


def _seed_five_fresh_profiles(session_factory, embedder) -> list[int]:
    """Five profiles, all embedded and fingerprinted against their own text."""
    with session_factory() as s:
        ids = [make_profile(s, description=f"Fresh {i}").id for i in range(5)]
        s.commit()
    refresh_missing_embeddings_tick(session_factory=session_factory, embed_client=embedder)
    return ids


def _go_stale(session_factory, profile_id: int) -> None:
    with session_factory() as s:
        s.get(Profile, profile_id).description = "Changed to something else"
        s.commit()


def _is_current(session_factory, profile_id: int) -> bool:
    with session_factory() as s:
        p = s.get(Profile, profile_id)
        return p.embedding_fingerprint == embedding_fingerprint(p)


def test_a_capped_tick_resumes_where_it_stopped_instead_of_restarting_at_zero(
    pg_session_factory, fake_embedder, embeddings_tick
):
    """GATE 9a — the trap the spec names.

    Five fresh profiles, a cap of three batches of one row. The first tick
    examines ids[0..2] and stops on the cap having done no work. If the
    scheduler does not carry the cursor, the second tick restarts at
    after_profile_id=0 and halts at the same wall, and the stale ids[4] is
    never reached — today, or on any later day.
    """
    ids = _seed_five_fresh_profiles(pg_session_factory, fake_embedder)
    _go_stale(pg_session_factory, ids[4])

    embeddings_tick()  # examines ids[0], ids[1], ids[2] -> hits max_batches
    embeddings_tick()  # must resume after ids[2] and reach ids[4]

    assert _is_current(pg_session_factory, ids[4]), (
        "the capped tick restarted at after_profile_id=0 and stalled at the same wall"
    )


def test_an_exhausted_scan_resets_the_cursor_so_a_row_going_stale_behind_it_is_revisited(
    pg_session_factory, fake_embedder, embeddings_tick
):
    """GATE 9b — the failure mode the fix itself could introduce.

    A carried cursor that never goes back to 0 is a worse bug than the one it
    fixes: every profile behind the high-water mark becomes permanently
    unreachable. Once a scan is exhausted the cursor must reset.
    """
    ids = _seed_five_fresh_profiles(pg_session_factory, fake_embedder)

    embeddings_tick()  # capped, stops after ids[2]
    embeddings_tick()  # ids[3], ids[4], then an empty page -> scan EXHAUSTED

    _go_stale(pg_session_factory, ids[0])
    embeddings_tick()  # only reaches ids[0] if the exhausted scan reset the cursor

    assert _is_current(pg_session_factory, ids[0]), (
        "an exhausted scan left the cursor at the high-water mark, so a profile "
        "that goes stale behind it is never revisited"
    )
