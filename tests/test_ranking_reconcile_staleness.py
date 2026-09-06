"""Task 7 gates: reconcile asks whether a ranking is current, not whether it exists."""

from __future__ import annotations

import datetime as dt

import pytest

from grant_lab.db.models import CallRanking, Profile, RankingJob
from grant_lab.profiles.fingerprint import rank_fingerprint
from grant_lab.ranking.queue import reconcile_missing_rankings
from grant_lab.ranking.worker import drain_queue
from tests.freshness_helpers import enqueue_job, make_call, make_profile, ollama_clean


@pytest.fixture()
def session_factory(pg_session_factory):
    return pg_session_factory


@pytest.fixture()
def session(pg_session_factory):
    with pg_session_factory() as s:
        yield s


@pytest.fixture()
def ranked_pair(session, session_factory):
    p = make_profile(session, description="Liver disease", structured={"areas": ["AI"]})
    c = make_call(session)
    enqueue_job(session, call_id=c.id, profile_id=p.id)
    session.commit()
    drain_queue(
        session_factory=session_factory,
        client=ollama_clean(),
        embedding_threshold=0.0,
        batch_size=1,
    )
    return c.id, p.id, p


def _jobs(session, call_id, profile_id):
    return session.query(RankingJob).filter_by(call_id=call_id, profile_id=profile_id).count()


def test_a_matching_fingerprint_is_not_re_enqueued(session, ranked_pair):
    """Discriminating control: without this, 'always re-enqueue' passes
    every other test in this file."""
    call_id, profile_id, profile = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.profile_fingerprint = rank_fingerprint(profile)
    session.query(RankingJob).delete()
    session.commit()
    assert reconcile_missing_rankings(session) == 0


def test_a_differing_fingerprint_is_re_enqueued(session, ranked_pair):
    """GATE 11."""
    call_id, profile_id, _ = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.profile_fingerprint = "v1:something_else_entirely"
    session.query(RankingJob).delete()
    session.commit()
    assert reconcile_missing_rankings(session) >= 1
    assert _jobs(session, call_id, profile_id) == 1


def test_a_legacy_pair_with_an_untouched_profile_is_skipped(session, ranked_pair):
    """GATE 12. The user's decision: grandfather the pre-0075 corpus rather
    than re-rank everything on deploy.

    This is a unit gate on the SQL and nothing more. It does NOT show the
    corpus is grandfathered in production: the 03:30 embeddings tick writes
    profiles.embedding_fingerprint, that UPDATE bumps Profile.updated_at
    (onupdate=func.now()), and 04:00 reconcile then sees ranked_at <
    updated_at for every legacy pair. See PLAN-DEFECTS.md D50."""
    call_id, profile_id, profile = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.profile_fingerprint = "legacy"
    # ranked_at must be at or after the profile's last edit.
    cr.ranked_at = profile.updated_at
    session.query(RankingJob).delete()
    session.commit()
    assert reconcile_missing_rankings(session) == 0


def test_a_legacy_pair_whose_profile_was_edited_IS_re_enqueued(session, ranked_pair):
    """GATE 12b. An earlier revision's legacy arm was unconditional, so a
    pre-0075 pair stayed exempt FOREVER -- including after the edit whose
    background task was lost, which is the exact case this repair exists for."""
    call_id, profile_id, profile = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.profile_fingerprint = "legacy"
    cr.ranked_at = profile.updated_at - dt.timedelta(days=1)
    session.query(RankingJob).delete()
    session.commit()
    assert reconcile_missing_rankings(session) >= 1


def test_a_null_fingerprint_after_deploy_is_treated_as_stale(session, ranked_pair):
    """GATE 13. Every row present at migration time was stamped 'legacy', so a
    NULL here means a writer bug. Treat it as stale so the bug is loud."""
    call_id, profile_id, _ = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.profile_fingerprint = None
    session.query(RankingJob).delete()
    session.commit()
    assert reconcile_missing_rankings(session) >= 1


def test_no_active_profiles_issues_no_insert(session, ranked_pair):
    """GATE 14. An empty VALUES list is a SYNTAX ERROR, so without the early
    return the daily tick raises on a fresh or fully-archived database --
    where today's CROSS JOIN simply degrades to zero rows.

    `ranked_pair` is requested so there IS a profile to deactivate -- without
    it the table may already be empty and the gate proves nothing.
    """
    session.query(Profile).update({Profile.is_active: False})
    session.commit()
    assert reconcile_missing_rankings(session) == 0
