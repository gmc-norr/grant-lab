"""Primitive C — the shared lease. Effects hold it in shared mode; lifecycle
transitions hold the same key exclusively."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import text

from call_manager.proposal_assist.drafting.locking import (
    CommitMutexTimeout,
    acquire_commit_mutex,
)
from call_manager.proposal_assist.fencing import acquire_shared_lease
from tests.proposal_assist.conftest import make_workspace


def _lock_timeout_ms(session) -> int:
    raw = session.execute(text("SHOW lock_timeout")).scalar()
    return int(raw.removesuffix("ms")) if raw.endswith("ms") else int(raw[:-1]) * 1000


def test_two_effects_hold_the_lease_concurrently(pg_session_factory):
    """Shared locks do not conflict. If effects serialized against each other the
    fence would be a global per-workspace write lock, not a lease."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()  # make_workspace does NOT flush — ws.id is None until it does
        ws_id = ws.id

    first_has_lease = threading.Event()
    second_got_lease = threading.Event()

    def _second():
        first_has_lease.wait(timeout=5)
        with pg_session_factory() as s2:
            acquire_shared_lease(s2, ws_id, timeout_seconds=3)
            second_got_lease.set()
            s2.rollback()

    t = threading.Thread(target=_second)
    with pg_session_factory() as s1:
        acquire_shared_lease(s1, ws_id)
        t.start()
        first_has_lease.set()
        assert second_got_lease.wait(timeout=10), "a second effect blocked on the lease"
        s1.rollback()
    t.join(timeout=10)


def test_the_lease_blocks_an_exclusive_lifecycle_acquisition(pg_session_factory):
    """The whole point. A lifecycle transition must not proceed while an effect
    is in flight."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()  # make_workspace does NOT flush — ws.id is None until it does
        ws_id = ws.id

    lease_held = threading.Event()
    outcome: dict = {}

    def _lifecycle():
        lease_held.wait(timeout=5)
        with pg_session_factory() as s2:
            try:
                acquire_commit_mutex(s2, ws_id, timeout_seconds=1)
                outcome["result"] = "acquired"
            except CommitMutexTimeout:
                outcome["result"] = "blocked"
            s2.rollback()

    t = threading.Thread(target=_lifecycle)
    with pg_session_factory() as s1:
        acquire_shared_lease(s1, ws_id)
        t.start()
        lease_held.set()
        t.join(timeout=20)
        s1.rollback()

    assert outcome["result"] == "blocked"


def test_the_lease_is_released_by_rollback(pg_session_factory):
    """pg_advisory_xact_lock_shared is TRANSACTION scoped — there is no explicit
    release, and an AUTOCOMMIT session would drop it instantly and silently
    provide no exclusion at all."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()  # make_workspace does NOT flush — ws.id is None until it does
        ws_id = ws.id

    with pg_session_factory() as s1:
        acquire_shared_lease(s1, ws_id)
        s1.rollback()

    with pg_session_factory() as s2:
        acquire_commit_mutex(s2, ws_id, timeout_seconds=2)  # must not raise
        s2.rollback()


def test_a_contended_lease_raises_commit_mutex_timeout(pg_session_factory):
    """Bounded, like every other acquisition here. An unbounded wait lets one
    wedged holder pin a worker indefinitely."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()  # make_workspace does NOT flush — ws.id is None until it does
        ws_id = ws.id

    holder_ready = threading.Event()
    release = threading.Event()

    def _exclusive_holder():
        with pg_session_factory() as s2:
            acquire_commit_mutex(s2, ws_id)
            holder_ready.set()
            release.wait(timeout=10)
            s2.rollback()

    t = threading.Thread(target=_exclusive_holder)
    t.start()
    try:
        holder_ready.wait(timeout=5)
        with pg_session_factory() as s1:
            with pytest.raises(CommitMutexTimeout):
                acquire_shared_lease(s1, ws_id, timeout_seconds=1)
            s1.rollback()
    finally:
        release.set()
        t.join(timeout=10)


def test_the_tail_stays_bounded_after_the_lease_is_won(pg_session_factory):
    """Slice C3's rule. Restoring DEFAULT (= 0 = disabled) here would un-bound
    every lock wait for the rest of the effect's transaction."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()  # make_workspace does NOT flush — ws.id is None until it does
        ws_id = ws.id

    with pg_session_factory() as s1:
        acquire_shared_lease(s1, ws_id, timeout_seconds=9)
        assert _lock_timeout_ms(s1) == 2000
        s1.rollback()


def test_a_non_positive_timeout_is_rejected(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        with pytest.raises(ValueError):
            acquire_shared_lease(s, ws.id, timeout_seconds=0)
        s.rollback()
