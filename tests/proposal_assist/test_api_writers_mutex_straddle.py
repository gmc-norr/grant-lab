"""Spec §12 gate 13 — each of the six owner-write routes, straddled by a delete.

Two shapes per route:

  404-straddle: a holder takes the commit mutex; the route queues behind it; the
    holder stamps soft_deleted_at and COMMITS, releasing the mutex; the route
    wins it, re-reads, and must return 404 having written NOTHING.

  429-wedged: a holder takes the mutex and never releases it; the route must
    return 429 with Retry-After: 2, having written NOTHING.

The holder stamps soft_deleted_at DIRECTLY rather than calling the delete route.
The route would take this same mutex on the same thread and self-deadlock. Do not
"simplify" that into a hang.

Blocking is detected by polling pg_locks, not by sleeping: a sleep races on a
loaded machine and turns a real gate into a flaky one.
"""

from __future__ import annotations

import functools
import threading
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from call_manager.db.models import Workspace
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex, commit_mutex_key
from tests.proposal_assist.conftest import make_workspace


def _advisory_waiters(session_factory, ws_id) -> int:
    """Count ungranted advisory locks ON THIS WORKSPACE'S commit mutex.

    Keyed, not a bare count. The precedent this copies
    (tests/proposal_assist/test_retire_mutex_straddle.py) counts every ungranted
    advisory lock in the database and justifies it with "the test DB is otherwise
    idle" — but our client fixture starts the real in-process APScheduler, whose
    ticks take advisory locks of their own. An unrelated waiter would satisfy an
    unkeyed probe early and let the job-first mutation pass, quietly destroying
    the gate this file exists for.

    pg_advisory_xact_lock(bigint) splits the key: classid is the high 32 bits,
    objid the low 32, both as int4.
    """
    key = commit_mutex_key(ws_id)
    with session_factory() as s:
        return s.scalar(
            text(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND NOT granted "
                "AND classid = :classid AND objid = :objid"
            ),
            {"classid": (key >> 32) & 0xFFFFFFFF, "objid": key & 0xFFFFFFFF},
        )


def _wait_until_blocked(session_factory, ws_id, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _advisory_waiters(session_factory, ws_id) >= 1:
            return
        time.sleep(0.02)
    raise AssertionError(
        f"no route blocked on {ws_id}'s commit mutex within {timeout}s — the route "
        "is not participating in the mutex at all"
    )


def test_the_blocking_probe_actually_observes_a_lock_wait(pg_session_factory, current_user):
    """The probe is load-bearing for every gate in this file: if it returned 0
    forever, _wait_until_blocked would raise and every straddle would look like a
    routing failure; if it returned >=1 spuriously, the lock-order gates would
    sample too early and pass under the forbidden order. Prove it does neither.

    Ported from tests/proposal_assist/test_retire_mutex_straddle.py, which carries
    the same self-test for the same reason."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    # Nobody is waiting yet.
    assert _advisory_waiters(pg_session_factory, ws_id) == 0

    holding = threading.Event()
    release = threading.Event()
    waiting = threading.Event()

    def _holder():
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    def _waiter():
        holding.wait(timeout=10)
        with pg_session_factory() as s:
            waiting.set()
            acquire_commit_mutex(s, ws_id)  # queues behind the holder
            s.rollback()

    threads = [threading.Thread(target=_holder), threading.Thread(target=_waiter)]
    for t in threads:
        t.start()
    try:
        assert holding.wait(timeout=5)
        assert waiting.wait(timeout=5)
        _wait_until_blocked(pg_session_factory, ws_id, timeout=10)  # must not raise
    finally:
        release.set()
        for t in threads:
            t.join(timeout=30)

    # ...and it goes back to zero once everyone is done.
    assert _advisory_waiters(pg_session_factory, ws_id) == 0


def patch_route_mutex_timeout(monkeypatch, module, seconds: int = 1) -> None:
    """Pin a short mutex timeout for one route module. We are exercising the
    route's error mapping, not the production timeout value, and the 10s default
    would stall the suite."""
    monkeypatch.setattr(
        module,
        "acquire_commit_mutex_live",
        functools.partial(module.acquire_commit_mutex_live, timeout_seconds=seconds),
    )


def straddle_delete(pg_session_factory, ws_id, send):
    """Run `send` so it blocks on the commit mutex, then commit a deletion
    underneath it. Returns whatever `send` returned."""
    holding = threading.Event()
    out: dict = {}

    def _sender():
        holding.wait(timeout=10)
        out["resp"] = send()

    def _holder():
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id)
            holding.set()
            _wait_until_blocked(pg_session_factory, ws_id, timeout=10)
            s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
            s.commit()  # releases the mutex; the route wakes up next

    threads = [threading.Thread(target=_holder), threading.Thread(target=_sender)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    if "resp" not in out:
        pytest.fail("the route never returned — it is wedged, not merely slow")
    return out["resp"]


def wedge_mutex(pg_session_factory, ws_id, send):
    """Hold the commit mutex for the whole of `send`. Returns what it returned."""
    holding = threading.Event()
    release = threading.Event()

    def _holder():
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    t = threading.Thread(target=_holder)
    t.start()
    try:
        assert holding.wait(timeout=5), "the holder never acquired the mutex"
        return send()
    finally:
        release.set()
        t.join(timeout=30)


def test_update_settings_straddled_by_a_delete_returns_404_and_writes_nothing(
    client_authed_owner, current_user, pg_session_factory
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.title = "original title"
        s.commit()
        ws_id = ws.id

    resp = straddle_delete(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.patch(
            f"/api/workspaces/{ws_id}/settings",
            json={
                "title": "edited by a blocked writer",
                "status": "drafting",
                "deadline": None,
                "citation_style": "vancouver",
            },
        ),
    )

    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).title == "original title"


def test_update_settings_returns_429_when_the_commit_mutex_is_wedged(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    import call_manager.web.api.settings as settings_api

    patch_route_mutex_timeout(monkeypatch, settings_api, seconds=1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.title = "original title"
        s.commit()
        ws_id = ws.id

    resp = wedge_mutex(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.patch(
            f"/api/workspaces/{ws_id}/settings",
            json={
                "title": "edited under a wedged mutex",
                "status": "drafting",
                "deadline": None,
                "citation_style": "vancouver",
            },
        ),
    )

    assert resp.status_code == 429, f"expected 429, got {resp.status_code}: {resp.text}"
    assert resp.headers.get("Retry-After") == "2"
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).title == "original title"
