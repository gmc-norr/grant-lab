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
import uuid as uuidlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from call_manager.db.models import Workspace, WorkspaceSetupJob, WorkspaceViewer
from call_manager.proposal_assist.drafting.locking import (
    BUSY_ERROR_MESSAGE,
    acquire_commit_mutex,
    commit_mutex_key,
)
from tests.proposal_assist.conftest import make_user, make_workspace


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
    # The contract is 429 + Retry-After: 2 + the SHARED message. BUSY_ERROR_MESSAGE
    # is imported, not re-typed, so a route that invents its own busy wording fails
    # here instead of silently diverging from the agent-facing message.
    assert resp.json()["error"]["message"] == BUSY_ERROR_MESSAGE, (
        f"429 body did not carry the shared busy message: {resp.text}"
    )
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).title == "original title"


def test_add_member_straddled_by_a_delete_returns_404_and_writes_nothing(
    client_authed_owner, current_user, pg_session_factory
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        target = make_user(s, email="c2-add-straddle@ki.se")
        s.commit()
        ws_id, target_id = ws.id, target.id

    resp = straddle_delete(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.post(
            f"/api/workspaces/{ws_id}/members", json={"email": "c2-add-straddle@ki.se"}
        ),
    )

    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"
    with pg_session_factory() as s:
        assert (
            s.query(WorkspaceViewer).filter_by(workspace_id=ws_id, user_id=target_id).first()
            is None
        ), "a blocked add_member inserted a WorkspaceViewer for a deleted workspace"


def test_add_member_returns_429_when_the_commit_mutex_is_wedged(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    import call_manager.web.api.settings as settings_api

    patch_route_mutex_timeout(monkeypatch, settings_api, seconds=1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        target = make_user(s, email="c2-add-wedged@ki.se")
        s.commit()
        ws_id, target_id = ws.id, target.id

    resp = wedge_mutex(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.post(
            f"/api/workspaces/{ws_id}/members", json={"email": "c2-add-wedged@ki.se"}
        ),
    )

    assert resp.status_code == 429, f"expected 429, got {resp.status_code}: {resp.text}"
    assert resp.headers.get("Retry-After") == "2"
    # The contract is 429 + Retry-After: 2 + the SHARED message. BUSY_ERROR_MESSAGE
    # is imported, not re-typed, so a route that invents its own busy wording fails
    # here instead of silently diverging from the agent-facing message.
    assert resp.json()["error"]["message"] == BUSY_ERROR_MESSAGE, (
        f"429 body did not carry the shared busy message: {resp.text}"
    )
    with pg_session_factory() as s:
        assert (
            s.query(WorkspaceViewer).filter_by(workspace_id=ws_id, user_id=target_id).first()
            is None
        )


def test_remove_member_straddled_by_a_delete_returns_404_and_writes_nothing(
    client_authed_owner, current_user, pg_session_factory
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        target = make_user(s, email="c2-rm-straddle@ki.se")
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=target.id))
        s.commit()
        ws_id, target_id = ws.id, target.id

    resp = straddle_delete(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.delete(f"/api/workspaces/{ws_id}/members/{target_id}"),
    )

    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"
    with pg_session_factory() as s:
        assert (
            s.query(WorkspaceViewer).filter_by(workspace_id=ws_id, user_id=target_id).first()
            is not None
        ), "a blocked remove_member deleted a WorkspaceViewer for a deleted workspace"


def test_remove_member_returns_429_when_the_commit_mutex_is_wedged(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    import call_manager.web.api.settings as settings_api

    patch_route_mutex_timeout(monkeypatch, settings_api, seconds=1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        target = make_user(s, email="c2-rm-wedged@ki.se")
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=target.id))
        s.commit()
        ws_id, target_id = ws.id, target.id

    resp = wedge_mutex(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.delete(f"/api/workspaces/{ws_id}/members/{target_id}"),
    )

    assert resp.status_code == 429, f"expected 429, got {resp.status_code}: {resp.text}"
    assert resp.headers.get("Retry-After") == "2"
    # The contract is 429 + Retry-After: 2 + the SHARED message. BUSY_ERROR_MESSAGE
    # is imported, not re-typed, so a route that invents its own busy wording fails
    # here instead of silently diverging from the agent-facing message.
    assert resp.json()["error"]["message"] == BUSY_ERROR_MESSAGE, (
        f"429 body did not carry the shared busy message: {resp.text}"
    )
    with pg_session_factory() as s:
        assert (
            s.query(WorkspaceViewer).filter_by(workspace_id=ws_id, user_id=target_id).first()
            is not None
        )


_RUBRIC_BODY = {
    "criteria": [
        {"name": "Impact", "description": "how much it matters", "weight": 1.0, "scale_max": 5}
    ],
    "funder_notes": "edited by a blocked writer",
}


def test_update_rubric_straddled_by_a_delete_returns_404_and_writes_nothing(
    client_authed_owner, current_user, pg_session_factory
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.rubric_funder_notes = "original notes"
        s.commit()
        ws_id = ws.id

    resp = straddle_delete(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.put(f"/api/workspaces/{ws_id}/rubric", json=_RUBRIC_BODY),
    )

    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).rubric_funder_notes == "original notes"


def test_update_rubric_returns_429_when_the_commit_mutex_is_wedged(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    import call_manager.web.api.rubric as rubric_api

    patch_route_mutex_timeout(monkeypatch, rubric_api, seconds=1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.rubric_funder_notes = "original notes"
        s.commit()
        ws_id = ws.id

    resp = wedge_mutex(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.put(f"/api/workspaces/{ws_id}/rubric", json=_RUBRIC_BODY),
    )

    assert resp.status_code == 429, f"expected 429, got {resp.status_code}: {resp.text}"
    assert resp.headers.get("Retry-After") == "2"
    # The contract is 429 + Retry-After: 2 + the SHARED message. BUSY_ERROR_MESSAGE
    # is imported, not re-typed, so a route that invents its own busy wording fails
    # here instead of silently diverging from the agent-facing message.
    assert resp.json()["error"]["message"] == BUSY_ERROR_MESSAGE, (
        f"429 body did not carry the shared busy message: {resp.text}"
    )
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).rubric_funder_notes == "original notes"


def _setup_job(session, ws_id, status: str) -> uuidlib.UUID:
    """Create a WorkspaceSetupJob in `status` for `ws_id` and return its id."""
    job = WorkspaceSetupJob(workspace_id=ws_id, status=status)
    session.add(job)
    session.flush()
    return job.id


def test_retry_setup_straddled_by_a_delete_returns_404_and_writes_nothing(
    client_authed_owner, current_user, pg_session_factory
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        job_id = _setup_job(s, ws.id, "failed")
        s.commit()
        ws_id = ws.id

    resp = straddle_delete(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.post(f"/api/workspaces/{ws_id}/setup/retry"),
    )

    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "failed", (
            "a blocked retry_setup re-queued a setup job for a deleted workspace"
        )


def test_retry_setup_returns_429_when_the_commit_mutex_is_wedged(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    import call_manager.web.api.rubric as rubric_api

    patch_route_mutex_timeout(monkeypatch, rubric_api, seconds=1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        job_id = _setup_job(s, ws.id, "failed")
        s.commit()
        ws_id = ws.id

    resp = wedge_mutex(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.post(f"/api/workspaces/{ws_id}/setup/retry"),
    )

    assert resp.status_code == 429, f"expected 429, got {resp.status_code}: {resp.text}"
    assert resp.headers.get("Retry-After") == "2"
    # The contract is 429 + Retry-After: 2 + the SHARED message. BUSY_ERROR_MESSAGE
    # is imported, not re-typed, so a route that invents its own busy wording fails
    # here instead of silently diverging from the agent-facing message.
    assert resp.json()["error"]["message"] == BUSY_ERROR_MESSAGE, (
        f"429 body did not carry the shared busy message: {resp.text}"
    )
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "failed"


def test_activate_straddled_by_a_delete_returns_404_and_writes_nothing(
    client_authed_owner, current_user, pg_session_factory
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.current_phase = "setup"
        s.flush()
        job_id = _setup_job(s, ws.id, "waiting_for_rubric_review")
        s.commit()
        ws_id = ws.id

    resp = straddle_delete(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.post(f"/api/workspaces/{ws_id}/activate", json=_RUBRIC_BODY),
    )

    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "waiting_for_rubric_review"
        assert s.get(Workspace, ws_id).current_phase == "setup", (
            "a blocked activate transitioned a deleted workspace to active"
        )


def test_activate_returns_429_when_the_commit_mutex_is_wedged(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    import call_manager.web.api.rubric as rubric_api

    patch_route_mutex_timeout(monkeypatch, rubric_api, seconds=1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.current_phase = "setup"
        s.flush()
        job_id = _setup_job(s, ws.id, "waiting_for_rubric_review")
        s.commit()
        ws_id = ws.id

    resp = wedge_mutex(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.post(f"/api/workspaces/{ws_id}/activate", json=_RUBRIC_BODY),
    )

    assert resp.status_code == 429, f"expected 429, got {resp.status_code}: {resp.text}"
    assert resp.headers.get("Retry-After") == "2"
    # The contract is 429 + Retry-After: 2 + the SHARED message. BUSY_ERROR_MESSAGE
    # is imported, not re-typed, so a route that invents its own busy wording fails
    # here instead of silently diverging from the agent-facing message.
    assert resp.json()["error"]["message"] == BUSY_ERROR_MESSAGE, (
        f"429 body did not carry the shared busy message: {resp.text}"
    )
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "waiting_for_rubric_review"
        assert s.get(Workspace, ws_id).current_phase == "setup"


def _assert_hook_can_cancel_while_route_is_blocked(pg_session_factory, ws_id, job_id, send):
    """THE lock-order gate.

    A holder takes and KEEPS the commit mutex. The route queues behind it. While
    the route is still blocked, we run the retirement hook's setup-job cancel —
    which is FOR UPDATE SKIP LOCKED — in a third session.

    Mutex-first: the route holds no row lock while it waits, so the cancel takes
    the row and terminalizes it.
    Job-first:   the route holds the setup-job row FOR UPDATE for the whole wait,
                 so the cancel SKIPS it and the job stays non-terminal until the
                 reconciliation tick (up to 60s later).

    The assertion runs WHILE the route is still blocked. Sampling after the mutex
    is released would let the route's own rollback free the row and repair the
    very thing under test.
    """
    from call_manager.proposal_assist.jobs_retirement import cancel_setup_jobs_for_workspace

    holding = threading.Event()
    release = threading.Event()
    out: dict = {}
    cancelled = None  # assigned in the try; initialised so a failure there does
    # not turn into a NameError that hides the real error below.

    def _holder():
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    def _sender():
        holding.wait(timeout=10)
        out["resp"] = send()

    holder = threading.Thread(target=_holder)
    sender = threading.Thread(target=_sender)
    holder.start()
    sender.start()
    try:
        assert holding.wait(timeout=5), "the holder never acquired the mutex"
        _wait_until_blocked(pg_session_factory, ws_id, timeout=10)
        # Stamp the deletion so the scenario is the real one — a delete that has
        # committed while the route waits. cancel_setup_jobs_for_workspace itself
        # has NO soft_deleted_at predicate (only reconcile_cancelled_jobs does),
        # so this is not what makes the cancel fire; it is what makes the test
        # describe a situation that can actually happen.
        with pg_session_factory() as s:
            s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
            s.commit()
        with pg_session_factory() as s:
            cancelled = cancel_setup_jobs_for_workspace(s, ws_id)
            s.commit()
    finally:
        release.set()
        holder.join(timeout=30)
        sender.join(timeout=30)

    # Match straddle_delete's guard. Nothing above depends on the route's own
    # response — _wait_until_blocked already proved it queued on the mutex — but
    # a wedged route must fail HERE rather than leak a live thread holding an
    # open DB session into whatever test runs next.
    assert not sender.is_alive(), "the route thread never finished — it is wedged, not slow"
    if "resp" not in out:
        pytest.fail("the route never returned — it is wedged, not merely slow")

    assert cancelled == 1, (
        "the retirement hook could not cancel the setup job while the route was "
        "blocked on the mutex — the route is holding the job row, which means it "
        "took the row lock BEFORE the mutex. SKIP LOCKED skipped it, so the delete "
        "leaves the job non-terminal until the reconciliation tick."
    )
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "cancelled"


def test_retry_setup_does_not_hold_the_setup_job_row_while_it_waits_for_the_mutex(
    client_authed_owner, current_user, pg_session_factory
):
    # SEED 'queued', NOT 'failed'. cancel_setup_jobs_for_workspace filters on
    # _CANCELLABLE = ("queued", "running", "waiting_for_rubric_review")
    # (jobs_retirement.py:55) BEFORE it ever consults a lock, and 'failed' is
    # terminal — so with a 'failed' row the cancel returns 0 no matter who holds
    # the row, and this gate would be red in BOTH lock orders. That makes the
    # Step 5 mutation vacuous: mutated and unmutated look identical.
    #
    # The interference we sample is unchanged: retry_setup takes its row lock
    # UNCONDITIONALLY before it inspects status. This gate never asserts the
    # route's own status code, so the 409 a 'queued' job would earn is
    # irrelevant here. The straddle and wedge gates above keep 'failed' — they
    # DO need the happy path.
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        job_id = _setup_job(s, ws.id, "queued")
        s.commit()
        ws_id = ws.id

    _assert_hook_can_cancel_while_route_is_blocked(
        pg_session_factory,
        ws_id,
        job_id,
        lambda: client_authed_owner.post(f"/api/workspaces/{ws_id}/setup/retry"),
    )


def test_activate_does_not_hold_the_setup_job_row_while_it_waits_for_the_mutex(
    client_authed_owner, current_user, pg_session_factory
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.current_phase = "setup"
        s.flush()
        job_id = _setup_job(s, ws.id, "waiting_for_rubric_review")
        s.commit()
        ws_id = ws.id

    _assert_hook_can_cancel_while_route_is_blocked(
        pg_session_factory,
        ws_id,
        job_id,
        lambda: client_authed_owner.post(f"/api/workspaces/{ws_id}/activate", json=_RUBRIC_BODY),
    )


def test_retry_setup_gives_up_on_a_held_setup_job_row_instead_of_pinning_the_mutex(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """The route takes the commit mutex and THEN the setup-job row. That second
    wait must be bounded: acquire_commit_mutex restores lock_timeout = DEFAULT,
    so an unbounded wait there holds the mutex indefinitely and a concurrent
    DELETE 429s behind it after burning its whole budget.

    Asserted as 'the request came back', not as elapsed time: without the bound
    the route does not return at all while we hold the row, so a thread that is
    still alive after the join IS the failure. Slice C1 learned that an elapsed-
    time assertion can pass for the opposite reason."""
    # Patch the name in the ROUTE module, not in row_locks. rubric.py does
    # `from ...row_locks import SETUP_JOB_LOCK_TIMEOUT_SECONDS`, which binds the
    # value into rubric's namespace at import time — patching row_locks would
    # silently no-op and this test would pass at the production 2s instead of 1s,
    # proving nothing about the bound.
    import call_manager.web.api.rubric as rubric_api

    monkeypatch.setattr(rubric_api, "SETUP_JOB_LOCK_TIMEOUT_SECONDS", 1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        job_id = _setup_job(s, ws.id, "failed")
        s.commit()
        ws_id = ws.id

    holding = threading.Event()
    release = threading.Event()
    out: dict = {}

    def _row_holder():
        """Hold the setup-job row FOR UPDATE — and take NO mutex, so the route
        reaches its job lock and waits there rather than at the mutex."""
        with pg_session_factory() as s:
            s.get(WorkspaceSetupJob, job_id, with_for_update=True)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    def _sender():
        holding.wait(timeout=10)
        out["resp"] = client_authed_owner.post(f"/api/workspaces/{ws_id}/setup/retry")

    holder = threading.Thread(target=_row_holder)
    sender = threading.Thread(target=_sender)
    holder.start()
    sender.start()
    try:
        assert holding.wait(timeout=5), "the holder never took the setup-job row"
        sender.join(timeout=15)
        assert not sender.is_alive(), (
            "retry_setup was still waiting on the setup-job row after 15s while "
            "holding the commit mutex — the row wait is unbounded"
        )
    finally:
        release.set()
        holder.join(timeout=30)
        sender.join(timeout=30)

    assert out["resp"].status_code == 429
    assert out["resp"].headers.get("Retry-After") == "2"
    assert out["resp"].json()["error"]["message"] == BUSY_ERROR_MESSAGE, (
        f"429 body did not carry the shared busy message: {out['resp'].text}"
    )
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "failed"


def test_activate_gives_up_on_a_held_setup_job_row_instead_of_pinning_the_mutex(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """activate's half of the bounded setup-row wait. Same hazard, same shape as
    the retry_setup gate: the route holds the commit mutex across this row wait,
    so an unbounded wait pins the mutex and a concurrent DELETE 429s behind it
    after burning its whole budget."""
    import call_manager.web.api.rubric as rubric_api

    monkeypatch.setattr(rubric_api, "SETUP_JOB_LOCK_TIMEOUT_SECONDS", 1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.current_phase = "setup"
        s.flush()
        job_id = _setup_job(s, ws.id, "waiting_for_rubric_review")
        s.commit()
        ws_id = ws.id

    holding = threading.Event()
    release = threading.Event()
    out: dict = {}

    def _row_holder():
        with pg_session_factory() as s:
            s.get(WorkspaceSetupJob, job_id, with_for_update=True)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    def _sender():
        holding.wait(timeout=10)
        out["resp"] = client_authed_owner.post(
            f"/api/workspaces/{ws_id}/activate", json=_RUBRIC_BODY
        )

    holder = threading.Thread(target=_row_holder)
    sender = threading.Thread(target=_sender)
    holder.start()
    sender.start()
    try:
        assert holding.wait(timeout=5), "the holder never took the setup-job row"
        sender.join(timeout=15)
        assert not sender.is_alive(), (
            "activate was still waiting on the setup-job row after 15s while holding "
            "the commit mutex — the row wait is unbounded"
        )
    finally:
        release.set()
        holder.join(timeout=30)
        sender.join(timeout=30)

    assert out["resp"].status_code == 429
    assert out["resp"].headers.get("Retry-After") == "2"
    assert out["resp"].json()["error"]["message"] == BUSY_ERROR_MESSAGE, (
        f"429 body did not carry the shared busy message: {out['resp'].text}"
    )
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "waiting_for_rubric_review"
        assert s.get(Workspace, ws_id).current_phase == "setup"


def test_activate_maps_a_post_job_lock_row_timeout_to_429_not_500(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """The bound outlives the acquisitions, so the TRANSLATION has to as well.

    bounded_lock_waits deliberately leaves SET LOCAL lock_timeout in force to the
    commit (row_locks.py's module docstring: the bound "must extend past the
    acquisitions to the commit"), but 55P03 -> RowLockTimeout happens only inside
    the context manager's body. Wrap the `with` narrowly around the setup-job
    SELECT and every later statement is still BOUNDED but no longer TRANSLATED:
    the 55P03 that `_apply_rubric`'s UPDATE fires at COMMIT escapes as a raw
    sqlalchemy OperationalError, and the caller gets a 500 where this slice
    contracts for 429 + Retry-After: 2.

    Reachable, not hypothetical. The holder below stands in for one of the four
    pass prefixes, which take the `workspaces` row FOR UPDATE and hold it across
    context assembly while holding NO commit mutex (row_locks.py:33-45) — so the
    mutex activate holds does not exclude them from that row.

    Note WHICH row is held: the WORKSPACE row, not the setup-job row. The job row
    must stay free so the route gets PAST its acquisition and blocks later, in
    exactly the stretch a narrow wrap leaves untranslated. The sibling gate above
    holds the job row and covers the acquisition itself.
    """
    import call_manager.web.api.rubric as rubric_api

    monkeypatch.setattr(rubric_api, "SETUP_JOB_LOCK_TIMEOUT_SECONDS", 1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.current_phase = "setup"
        ws.rubric_funder_notes = "original notes"
        s.flush()
        job_id = _setup_job(s, ws.id, "waiting_for_rubric_review")
        s.commit()
        ws_id = ws.id

    holding = threading.Event()
    release = threading.Event()
    out: dict = {}

    def _row_holder():
        with pg_session_factory() as s:
            s.get(Workspace, ws_id, with_for_update=True)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    def _sender():
        holding.wait(timeout=10)
        # raise_server_exceptions=True on this client: an UNtranslated
        # OperationalError propagates here instead of becoming a response, so
        # out stays empty. Both outcomes are failures; the guard below names them.
        out["resp"] = client_authed_owner.post(
            f"/api/workspaces/{ws_id}/activate", json=_RUBRIC_BODY
        )

    holder = threading.Thread(target=_row_holder)
    sender = threading.Thread(target=_sender)
    holder.start()
    sender.start()
    try:
        assert holding.wait(timeout=5), "the holder never took the workspace row"
        sender.join(timeout=15)
        assert not sender.is_alive(), (
            "activate was still blocked on the workspace row after 15s while holding "
            "the commit mutex — the bound does not reach past the job lock"
        )
    finally:
        release.set()
        holder.join(timeout=30)
        sender.join(timeout=30)

    if "resp" not in out:
        pytest.fail(
            "activate raised instead of responding — the lock timeout fired outside "
            "the bounded_lock_waits body, so it was never translated to RowLockTimeout"
        )
    assert out["resp"].status_code == 429, (
        f"expected 429, got {out['resp'].status_code}: {out['resp'].text}"
    )
    assert out["resp"].headers.get("Retry-After") == "2"
    assert out["resp"].json()["error"]["message"] == BUSY_ERROR_MESSAGE, (
        f"429 body did not carry the shared busy message: {out['resp'].text}"
    )
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "waiting_for_rubric_review"
        ws_after = s.get(Workspace, ws_id)
        assert ws_after.current_phase == "setup"
        assert ws_after.rubric_funder_notes == "original notes"
