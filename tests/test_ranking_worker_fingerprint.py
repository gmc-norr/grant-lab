"""Task 5 gates: a ranking records the profile the ranker actually saw."""

from __future__ import annotations

import pytest

from grant_lab.db.models import CallRanking, Profile
from grant_lab.profiles.fingerprint import FINGERPRINT_VERSION, rank_fingerprint
from grant_lab.ranking.queue import enqueue_for_pair
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
def fake_rank_client():
    """A real OllamaClient over an httpx.MockTransport. There is NO fake with
    a `produce_analysis` knob, and none is needed -- the local Gemma path
    never produces summary/applicant_notes, so every drain through this
    client already exercises the retained-analysis branch."""
    return ollama_clean()


@pytest.fixture()
def ranked_pair(session, session_factory, fake_rank_client):
    """(call_id, profile_id, profile) with the pair already ranked once."""
    p = make_profile(session, description="Liver disease", structured={"areas": ["AI"]})
    c = make_call(session)
    enqueue_job(session, call_id=c.id, profile_id=p.id)
    session.commit()
    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )
    return c.id, p.id, p


def test_the_worker_stamps_the_fingerprint_it_ranked(session, ranked_pair):
    call_id, profile_id, profile = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    assert cr.profile_fingerprint == rank_fingerprint(profile)
    assert cr.profile_fingerprint.startswith(f"{FINGERPRINT_VERSION}:")


def test_the_stamp_is_captured_before_rank_pair_not_after(session, session_factory):
    """GATE 9, written so it can discriminate.

    The edit must land MID-FLIGHT -- between the worker's
    `session.get(Profile, ...)` at worker.py:131 and its merge -- so the rank
    client itself commits it from a separate session inside the
    httpx.MockTransport handler. Capture-before stamps the pre-call hash
    (correct); capture-after-a-refresh stamps the mid-flight hash (the bug).
    Nothing today refreshes, so this is a forward guard.
    """
    p = make_profile(session, description="Liver disease", structured={"areas": ["AI"]})
    c = make_call(session)
    enqueue_job(session, call_id=c.id, profile_id=p.id)
    session.commit()
    pid, cid = p.id, c.id
    before = rank_fingerprint(p)

    client = ollama_clean()
    original = client._transport.handler

    def edit_then_answer(request):
        with session_factory() as mid:
            row = mid.get(Profile, pid)
            if row.description != "EDITED MID FLIGHT":
                row.description = "EDITED MID FLIGHT"
                mid.commit()
        return original(request)

    client._transport.handler = edit_then_answer

    drain_queue(
        session_factory=session_factory,
        client=client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    with session_factory() as fresh:
        row = fresh.get(Profile, pid)
        assert row.description == "EDITED MID FLIGHT", "the mid-flight edit did not land"
        assert rank_fingerprint(row) != before
        cr = fresh.get(CallRanking, (cid, pid))
        assert cr.profile_fingerprint == before, "the worker stamped a profile the ranker never saw"


def test_the_call_stamp_is_captured_before_rank_pair_not_after(
    session, session_factory, monkeypatch
):
    """The call-side twin of the test above (ranking spec D9).

    The edit lands on the very `Call` instance the worker holds, right after
    the real rank_pair returns: what a session.refresh after a concurrent edit
    would do. A separate-session commit (the profile test's shape) would stay
    invisible to that loaded instance, so a stamp recomputed after rank_pair
    would still pass; this shape does not let it.
    """
    from grant_lab.db.models import Call
    from grant_lab.profiles.fingerprint import call_rank_fingerprint
    from grant_lab.ranking import worker as worker_mod

    p = make_profile(session, description="Liver disease", structured={"areas": ["AI"]})
    c = make_call(session)
    enqueue_job(session, call_id=c.id, profile_id=p.id)
    session.commit()
    pid, cid = p.id, c.id
    before = call_rank_fingerprint(c)

    real_rank_pair = worker_mod.rank_pair

    def rank_then_edit(**kw):
        result = real_rank_pair(**kw)
        kw["call"].summary = "EDITED MID FLIGHT"
        return result

    monkeypatch.setattr(worker_mod, "rank_pair", rank_then_edit)
    drain_queue(
        session_factory=session_factory,
        client=ollama_clean(),
        embedding_threshold=0.0,
        batch_size=1,
    )

    with session_factory() as fresh:
        row = fresh.get(Call, cid)
        assert row.summary == "EDITED MID FLIGHT", "the mid-flight edit did not land"
        assert call_rank_fingerprint(row) != before
        cr = fresh.get(CallRanking, (cid, pid))
        assert cr.call_fingerprint == before, "the worker stamped a call the ranker never saw"


def test_retained_analysis_is_dropped_when_the_profile_changed(
    session, session_factory, ranked_pair, fake_rank_client
):
    """GATE 10. worker.py:238-243 copies the previous run's summary, notes and
    analysis_model when this run produced none. After a profile edit that
    retained advice describes the OLD profile, and stamping it with the NEW
    fingerprint would make it permanently invisible to repair."""
    call_id, profile_id, _ = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.summary = "Old advice about liver disease"
    cr.applicant_notes = [{"note": "old"}]
    cr.analysis_model = "claude-sonnet-4-6"
    session.commit()

    with session_factory() as other:
        other.get(Profile, profile_id).description = "Cardiac imaging"
        other.commit()

    # `ollama_clean()` already IS the gate-fail re-rank: the local Gemma path
    # never produces summary/applicant_notes (RankResult defaults them to
    # None, ranking/rank_pair.py:54-55). Do NOT set `produce_analysis = False`
    # -- that attribute does not exist on an OllamaClient and assigning it is
    # a silent no-op.
    enqueue_for_pair(session, call_id=call_id, profile_id=profile_id)
    session.commit()
    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    with session_factory() as fresh:
        cr = fresh.get(CallRanking, (call_id, profile_id))
        assert cr.summary is None
        assert cr.applicant_notes is None
        assert cr.analysis_model is None


def test_retained_analysis_survives_when_the_profile_did_not_change(
    session, session_factory, ranked_pair, fake_rank_client
):
    """The discriminating control for gate 10: an unchanged profile must KEEP
    its analysis, otherwise the guard is just 'always clear'."""
    call_id, profile_id, _ = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.summary = "Advice that is still current"
    cr.applicant_notes = [{"note": "still good"}]
    cr.analysis_model = "claude-sonnet-4-6"
    session.commit()

    enqueue_for_pair(session, call_id=call_id, profile_id=profile_id)
    session.commit()
    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    with session_factory() as fresh:
        cr = fresh.get(CallRanking, (call_id, profile_id))
        assert cr.summary == "Advice that is still current"
        assert cr.applicant_notes == [{"note": "still good"}]
        assert cr.analysis_model == "claude-sonnet-4-6"


def test_retained_analysis_is_dropped_when_the_call_changed(
    session, session_factory, ranked_pair, fake_rank_client
):
    """G9 (Astra #3): advice about the old call text must not survive a call edit."""
    from grant_lab.db.models import Call

    call_id, profile_id, _ = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.summary, cr.applicant_notes, cr.analysis_model = (
        "Old advice",
        [{"note": "old"}],
        "claude-sonnet-5",
    )
    session.commit()
    with session_factory() as other:
        other.get(Call, call_id).summary = "The funder rewrote the scope."
        other.commit()
    enqueue_for_pair(session, call_id=call_id, profile_id=profile_id)
    session.commit()
    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )
    with session_factory() as fresh:
        cr = fresh.get(CallRanking, (call_id, profile_id))
        assert (cr.summary, cr.applicant_notes, cr.analysis_model) == (None, None, None)


def test_retained_analysis_is_dropped_when_the_scoring_settings_changed(
    session, session_factory, ranked_pair, fake_rank_client
):
    """G9, the scoring side."""
    call_id, profile_id, _ = ranked_pair
    cr = session.get(CallRanking, (call_id, profile_id))
    cr.summary, cr.applicant_notes, cr.analysis_model = (
        "Old advice",
        [{"note": "old"}],
        "claude-sonnet-5",
    )
    session.commit()
    enqueue_for_pair(session, call_id=call_id, profile_id=profile_id)
    session.commit()
    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.05,
        batch_size=1,
    )
    with session_factory() as fresh:
        cr = fresh.get(CallRanking, (call_id, profile_id))
        assert cr.summary is None
