"""Spec §12 gate 13 — each of the six owner-write routes, straddled by a delete.

Two shapes per route:

  404-straddle: a holder takes the commit mutex; the route queues behind it; the
    holder stamps soft_deleted_at and COMMITS, releasing the mutex; the route
    wins it, re-reads, and must return 404 having written NOTHING.

  429-wedged: a holder takes the mutex and never releases it; the route must
    return 429 with Retry-After: 2, having written NOTHING.

A third shape covers the hazard this slice CREATED. Once a route holds the commit
mutex, `acquire_commit_mutex` has restored lock_timeout = DEFAULT, so every lock
it waits on afterwards is unbounded — and it pins the mutex for the whole wait,
which 429s the owner's DELETE and every other mutex-participating writer. So for
each route that takes a row lock after the mutex:

  429-row-held: a holder takes the ROW the route needs (and NO mutex), so the
    route wins the mutex and blocks later; the route must COME BACK with the
    same 429 + Retry-After: 2, having written NOTHING.

  Asserted as "the request came back" (`not sender.is_alive()`), never as elapsed
  time: without the bound the route does not return at all, so a live thread IS
  the failure. An elapsed-time assertion can pass for the opposite reason.

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
from sqlalchemy import select, text

from call_manager.db.models import (
    User,
    Workspace,
    WorkspaceEvent,
    WorkspaceSetupJob,
    WorkspaceViewer,
)
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
    the 55P03 fired by the under-mutex workspace re-read — and, absent that, by
    `_apply_rubric`'s UPDATE at COMMIT — escapes as a raw sqlalchemy
    OperationalError, and the caller gets a 500 where this slice contracts for
    429 + Retry-After: 2.

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


def hold_workspace_row(pg_session_factory, ws_id, send, *, route: str):
    """Hold the `workspaces` row FOR UPDATE — taking NO commit mutex — across the
    whole of `send`, and assert the request CAME BACK. Returns its response.

    The holder stands in for one of the four pass prefixes, which take this row
    FOR UPDATE while holding no mutex and keep it across context assembly's
    filesystem reads (row_locks.py:33-45). Taking no mutex is the point: the
    route must WIN the mutex and then block on the row, which is the stretch
    `acquire_commit_mutex`'s restore-to-DEFAULT leaves unbounded.

    Liveness, not timing: without the route's bound the sender thread never
    finishes, so `not sender.is_alive()` after a bounded join is the whole
    assertion. Slice C1 learned that an elapsed-time assertion can pass for the
    opposite reason.
    """
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
        # raise_server_exceptions=True on this client: an UNtranslated
        # OperationalError propagates here instead of becoming a response, so
        # `out` stays empty. The guard below names that outcome separately from
        # a wedge, because they have different causes and different fixes.
        holding.wait(timeout=10)
        out["resp"] = send()

    holder = threading.Thread(target=_row_holder)
    sender = threading.Thread(target=_sender)
    holder.start()
    sender.start()
    try:
        assert holding.wait(timeout=5), "the holder never took the workspace row"
        sender.join(timeout=15)
        assert not sender.is_alive(), (
            f"{route} was still blocked on the workspace row after 15s while holding "
            "the commit mutex — the row wait is unbounded, so it pins the mutex and "
            "the owner's DELETE 429s behind it after burning its whole budget"
        )
    finally:
        release.set()
        holder.join(timeout=30)
        sender.join(timeout=30)

    if "resp" not in out:
        pytest.fail(
            f"{route} raised instead of responding — the lock timeout fired OUTSIDE the "
            "bounded_lock_waits body, so it was never translated to RowLockTimeout and "
            "escaped as a raw OperationalError (500). The bound must enclose the row "
            "work THROUGH session.commit()."
        )
    return out["resp"]


def assert_busy_429(resp) -> None:
    """The house busy contract, byte-identical to the CommitMutexTimeout answer:
    429 + Retry-After: 2 + the SHARED message. BUSY_ERROR_MESSAGE is imported,
    not re-typed, so a route that invents its own wording fails here."""
    assert resp.status_code == 429, f"expected 429, got {resp.status_code}: {resp.text}"
    assert resp.headers.get("Retry-After") == "2"
    assert resp.json()["error"]["message"] == BUSY_ERROR_MESSAGE, (
        f"429 body did not carry the shared busy message: {resp.text}"
    )


def test_update_settings_gives_up_on_a_held_workspace_row_instead_of_pinning_the_mutex(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """update_settings UPDATEs the `workspaces` row while holding the commit
    mutex. That wait must be bounded and its 55P03 translated to the house 429."""
    # Patch the name on the ROUTE module, not on row_locks. settings.py does
    # `from ...row_locks import WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS`, which binds
    # the value into settings' namespace at import time — patching row_locks
    # would silently no-op and this gate would run at the production 2s, proving
    # nothing about the bound.
    import call_manager.web.api.settings as settings_api

    monkeypatch.setattr(settings_api, "WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS", 1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.title = "original title"
        s.commit()
        ws_id = ws.id

    resp = hold_workspace_row(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.patch(
            f"/api/workspaces/{ws_id}/settings",
            json={
                "title": "edited while the row was held",
                "status": "drafting",
                "deadline": None,
                "citation_style": "vancouver",
            },
        ),
        route="update_settings",
    )

    assert_busy_429(resp)
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).title == "original title"


def test_add_member_gives_up_on_a_held_workspace_row_instead_of_pinning_the_mutex(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """add_member never UPDATEs the workspace row, but its WorkspaceViewer and
    WorkspaceEvent inserts take FOR KEY SHARE on it as their FK parent — which
    conflicts with the holder's FOR UPDATE. Same hazard, same contract."""
    import call_manager.web.api.settings as settings_api

    monkeypatch.setattr(settings_api, "WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS", 1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        target = make_user(s, email="c2-add-rowheld@ki.se")
        s.commit()
        ws_id, target_id = ws.id, target.id

    resp = hold_workspace_row(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.post(
            f"/api/workspaces/{ws_id}/members", json={"email": "c2-add-rowheld@ki.se"}
        ),
        route="add_member",
    )

    assert_busy_429(resp)
    with pg_session_factory() as s:
        assert (
            s.query(WorkspaceViewer).filter_by(workspace_id=ws_id, user_id=target_id).first()
            is None
        ), "add_member committed a WorkspaceViewer despite the lock timeout"


def test_remove_member_gives_up_on_a_held_workspace_row_instead_of_pinning_the_mutex(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """The viewer DELETE itself takes no parent lock, but the WorkspaceEvent
    insert that follows it does — so the transaction blocks at COMMIT, after the
    DELETE has already been issued. The rollback must put the row back."""
    import call_manager.web.api.settings as settings_api

    monkeypatch.setattr(settings_api, "WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS", 1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        target = make_user(s, email="c2-rm-rowheld@ki.se")
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=target.id))
        s.commit()
        ws_id, target_id = ws.id, target.id

    resp = hold_workspace_row(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.delete(f"/api/workspaces/{ws_id}/members/{target_id}"),
        route="remove_member",
    )

    assert_busy_429(resp)
    with pg_session_factory() as s:
        assert (
            s.query(WorkspaceViewer).filter_by(workspace_id=ws_id, user_id=target_id).first()
            is not None
        ), "remove_member's viewer DELETE survived a transaction that timed out"


def test_update_rubric_gives_up_on_a_held_workspace_row_instead_of_pinning_the_mutex(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """update_rubric takes the `workspaces` row FOR UPDATE (the under-mutex
    re-read) and then UPDATEs it via _apply_rubric, all while holding the commit
    mutex — the sibling of activate's post-job-lock case, minus the job row (this
    route never takes one, which is why it reads its own
    WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS)."""
    import call_manager.web.api.rubric as rubric_api

    monkeypatch.setattr(rubric_api, "WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS", 1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.rubric_funder_notes = "original notes"
        s.commit()
        ws_id = ws.id

    resp = hold_workspace_row(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.put(f"/api/workspaces/{ws_id}/rubric", json=_RUBRIC_BODY),
        route="update_rubric",
    )

    assert_busy_429(resp)
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).rubric_funder_notes == "original notes"


# ---------------------------------------------------------------------------
# The COMMIT-TIME gates for the two rubric routes: a held `users` row.
#
# Everything above holds the `workspaces` row. That stopped reaching COMMIT for
# update_rubric and activate when the freshness fix (8817c33) made
# `session.refresh(ws, with_for_update=True)` the FIRST statement inside their
# bound: those routes now take the `workspaces` row EARLY, so a holder's 55P03
# fires at the refresh — inside any plausible narrow wrap — and dedenting
# `session.commit()` out of the bound leaves those two tests green. Gate 16
# (test_bounded_region_encloses_commit.py) covers the rule structurally for that
# reason; these two restore the RUNTIME half for the two routes that lost it.
#
# The interleaving that still discriminates is the one the routes never name.
# `_apply_rubric`'s `rubric_edited` event — and activate's `workspace_activated`
# — carry `actor_id`, an FK to `users` (db/models.py). Postgres validates it by
# taking `FOR KEY SHARE` on the acting user's row, at the INSERT that only
# happens when `session.commit()` flushes; `FOR KEY SHARE` conflicts with
# `FOR UPDATE`. So a held `users` row puts the contended wait at exactly the
# statement the bound has to cover, and nowhere earlier.
#
# Spec §8 (revision 7) asserts this as measured. It was — from a scratch test
# that was deleted, leaving the claim unreproduced in the tree. These are it.
# ---------------------------------------------------------------------------


def hold_user_row(pg_session_factory, user_id, send, *, route: str):
    """Hold the ACTING USER's `users` row FOR UPDATE — taking no commit mutex and
    never touching the `workspaces` row — across the whole of `send`, and assert
    the request CAME BACK. Returns its response.

    The holder stands in for `web/api/admin_users.py`'s
    `_lock_active_admin_count`, which takes `FOR UPDATE` over every active admin
    row while it counts, on the disable-last-admin and demote-last-admin paths.
    That transaction is SHORT — a lock-and-count, with `signal_user_revocation`
    and its 5s barrier outside the session — which is why `users` rows are not
    counted in test_row_lock_ceilings.py's contended-row arithmetic. Short is not
    zero, and that arithmetic is about how LONG a stall can be, not about whether
    one can happen: this wait is real, it is the last thing these two routes do,
    and untranslated it is a 500.

    Deliberately NOT the `workspaces` row (`hold_workspace_row` above): since the
    freshness fix both rubric routes take that row at the TOP of their bound, so
    its timeout fires at the refresh rather than at COMMIT.

    Liveness, not timing, exactly as `hold_workspace_row`: without the route's
    bound the sender thread never finishes, so `not sender.is_alive()` after a
    bounded join is the assertion.
    """
    holding = threading.Event()
    release = threading.Event()
    out: dict = {}

    def _row_holder():
        with pg_session_factory() as s:
            s.get(User, user_id, with_for_update=True)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    def _sender():
        # raise_server_exceptions=True on this client: an UNtranslated
        # OperationalError propagates out of the request instead of becoming a
        # response. Captured rather than left to threading.excepthook so the
        # guard below can NAME the error this gate is about — the distinction
        # between "answered 500" and "wedged" is the whole point of the two
        # separate failure messages.
        holding.wait(timeout=10)
        try:
            out["resp"] = send()
        except Exception as exc:
            out["exc"] = exc

    holder = threading.Thread(target=_row_holder)
    sender = threading.Thread(target=_sender)
    holder.start()
    sender.start()
    try:
        assert holding.wait(timeout=5), "the holder never took the users row"
        sender.join(timeout=15)
        assert not sender.is_alive(), (
            f"{route} was still blocked on the acting user's `users` row after 15s "
            "while holding the commit mutex — the commit-time FK wait is unbounded, "
            "so it pins the mutex and the owner's DELETE 429s behind it after "
            "burning its whole budget"
        )
    finally:
        release.set()
        holder.join(timeout=30)
        sender.join(timeout=30)

    if "resp" not in out:
        pytest.fail(
            f"{route} raised instead of responding — the 55P03 from the "
            "WorkspaceEvent insert's users-FK wait fired OUTSIDE the "
            "bounded_lock_waits body, so it was never translated to RowLockTimeout "
            "and escaped as a raw OperationalError (500) where this slice contracts "
            f"for 429. The bound must enclose the transaction THROUGH "
            f"session.commit(). Raised: {out.get('exc')!r}"
        )
    return out["resp"]


def test_update_rubric_maps_a_commit_time_users_fk_wait_to_429_not_500(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """update_rubric's last lock wait is at COMMIT, on a row the route never
    names: the `rubric_edited` event's `actor_id` FK takes `FOR KEY SHARE` on the
    acting user. Bounded AND translated -> the house 429, nothing written."""
    import call_manager.web.api.rubric as rubric_api

    # WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS is the constant THIS route's bound reads
    # (rubric.py's update_rubric); activate's reads SETUP_JOB_LOCK_TIMEOUT_SECONDS.
    # Patched on the ROUTE module, not on row_locks: rubric.py imports the value by
    # name, so patching row_locks would silently no-op and this gate would run at
    # the production 2s, proving nothing about the bound.
    monkeypatch.setattr(rubric_api, "WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS", 1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.rubric_funder_notes = "original notes"
        s.commit()
        ws_id = ws.id

    resp = hold_user_row(
        pg_session_factory,
        current_user.id,
        lambda: client_authed_owner.put(f"/api/workspaces/{ws_id}/rubric", json=_RUBRIC_BODY),
        route="update_rubric",
    )

    assert_busy_429(resp)
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).rubric_funder_notes == "original notes"
        assert (
            s.query(WorkspaceEvent).filter_by(workspace_id=ws_id, kind="rubric_edited").first()
            is None
        ), "update_rubric committed a rubric_edited event despite the lock timeout"


def test_activate_maps_a_commit_time_users_fk_wait_to_429_not_500(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """activate's sibling case. It writes TWO actor_id-bearing events
    (`rubric_edited` through _apply_rubric, `workspace_activated` of its own), so
    its commit takes the same `FOR KEY SHARE` on the acting user — after the job
    row and the workspace re-read have both succeeded, which is what puts the
    wait past every acquisition in its bound."""
    import call_manager.web.api.rubric as rubric_api

    # activate's bound reads SETUP_JOB_LOCK_TIMEOUT_SECONDS — it covers the whole
    # transaction, job row included — so that is the name to patch here.
    monkeypatch.setattr(rubric_api, "SETUP_JOB_LOCK_TIMEOUT_SECONDS", 1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.current_phase = "setup"
        ws.rubric_funder_notes = "original notes"
        s.flush()
        job_id = _setup_job(s, ws.id, "waiting_for_rubric_review")
        s.commit()
        ws_id = ws.id

    resp = hold_user_row(
        pg_session_factory,
        current_user.id,
        lambda: client_authed_owner.post(f"/api/workspaces/{ws_id}/activate", json=_RUBRIC_BODY),
        route="activate",
    )

    assert_busy_429(resp)
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "waiting_for_rubric_review"
        ws_after = s.get(Workspace, ws_id)
        assert ws_after.current_phase == "setup"
        assert ws_after.rubric_funder_notes == "original notes"
        assert (
            s.query(WorkspaceEvent)
            .filter_by(workspace_id=ws_id, kind="workspace_activated")
            .first()
            is None
        ), "activate committed a workspace_activated event despite the lock timeout"


# ---------------------------------------------------------------------------
# The STALE-READ gates: a route that serializes its commit but computes its diff
# from state it read BEFORE the mutex.
#
# require_owner loads the Workspace instance before the mutex; acquire_commit_
# mutex_live refreshes soft_deleted_at ONLY (a scalar SELECT that repopulates no
# attribute) and the sessionmaker sets expire_on_commit=False, so every other
# column on that instance is as old as the request. _apply_rubric derives
# content_changed and the rubric_edited event's old_criteria/old_funder_notes
# from exactly those columns.
#
# Two observable consequences, one gate each per route:
#   identical:  T1 R0->R1 commits while T2 waits; T2 submits the SAME R1. Stale,
#               T2 compares R1 against its cached R0, decides the content changed
#               and records a SECOND R0->R1 event for one edit.
#   distinct:   T1 R0->R1 commits while T2 waits; T2 submits R2. Stale, T2 records
#               old_criteria = R0, dropping R1 out of the audit chain entirely.
#
# T1 is the mutex HOLDER, not a second request. A second request would have to
# queue on the same mutex behind T2 — which arrived first — so the interleaving
# under test could not be produced at all. The holder calls the route's own
# _apply_rubric rather than reimplementing it, so what T1 writes is what the
# route writes, byte for byte. This mirrors straddle_delete, which stamps
# soft_deleted_at directly for the same reason.
#
# Interleaving is established with the keyed pg_locks probe, never a sleep: the
# holder does not write until _wait_until_blocked has SEEN T2 queued on the
# mutex, which is only reachable after T2's require_owner has already loaded the
# workspace. That ordering is the whole scenario.
# ---------------------------------------------------------------------------


def _rubric_body(marker: str) -> dict:
    return {
        "criteria": [
            {
                "name": f"Impact {marker}",
                "description": f"criterion revision {marker}",
                "weight": 1.0,
                "scale_max": 5,
            }
        ],
        "funder_notes": f"funder notes revision {marker}",
    }


def _normalized(body: dict) -> tuple[list[dict], str]:
    """Push a request body through the ROUTE's own validator.

    _apply_rubric's change detection is `criteria != old_c` on plain dicts, so a
    hand-built holder payload that differed only in shape (an int weight, a
    stripped key) would make every comparison report "changed" and the identical-
    write gate would pass for the wrong reason — with the bug still present.
    Normalizing through _validate_criteria/CriterionIn is what makes "identical"
    mean identical.
    """
    import call_manager.web.api.rubric as rubric_api

    criteria = rubric_api._validate_criteria(
        [rubric_api.CriterionIn(**c) for c in body["criteria"]]
    )
    return criteria, body["funder_notes"] or ""


def _seed_rubric(session, ws, body: dict) -> None:
    criteria, notes = _normalized(body)
    ws.rubric_criteria = criteria
    ws.rubric_funder_notes = notes


def apply_rubric_under_the_mutex(pg_session_factory, ws_id, user, body, send):
    """Run `send` so it blocks on the commit mutex, then commit a rubric edit
    underneath it via the route's own _apply_rubric. Returns send()'s response."""
    import call_manager.web.api.rubric as rubric_api

    criteria, notes = _normalized(body)
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
            # Fresh session, so this instance is loaded from the database — the
            # holder is never the stale party.
            rubric_api._apply_rubric(s, s.get(Workspace, ws_id), user, criteria, notes)
            s.commit()  # releases the mutex; the route wakes up next

    threads = [threading.Thread(target=_holder), threading.Thread(target=_sender)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    if "resp" not in out:
        pytest.fail("the route never returned — it is wedged, not merely slow")
    return out["resp"]


def _rubric_events(pg_session_factory, ws_id) -> list[WorkspaceEvent]:
    with pg_session_factory() as s:
        return list(
            s.scalars(
                select(WorkspaceEvent)
                .where(
                    WorkspaceEvent.workspace_id == ws_id,
                    WorkspaceEvent.kind == "rubric_edited",
                )
                .order_by(WorkspaceEvent.created_at)
            )
        )


def test_update_rubric_reads_the_rubric_the_mutex_holder_left_not_the_one_it_loaded(
    client_authed_owner, current_user, pg_session_factory
):
    """Identical concurrent write: exactly ONE rubric_edited event, not two."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        _seed_rubric(s, ws, _rubric_body("R0"))
        s.commit()
        ws_id = ws.id

    body = _rubric_body("R1")
    resp = apply_rubric_under_the_mutex(
        pg_session_factory,
        ws_id,
        current_user,
        body,  # T1 (the holder) writes R1 ...
        # ... and T2 (the route) submits the SAME R1.
        lambda: client_authed_owner.put(f"/api/workspaces/{ws_id}/rubric", json=body),
    )

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    events = _rubric_events(pg_session_factory, ws_id)
    assert len(events) == 1, (
        f"expected exactly 1 rubric_edited event for one edit, got {len(events)}: "
        f"{[e.payload for e in events]} — update_rubric compared the submitted rubric "
        "against the copy it loaded BEFORE the commit mutex, so it re-recorded an "
        "edit the holder had already committed"
    )
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).rubric_criteria == _normalized(body)[0]


def test_update_rubric_records_the_previous_rubric_as_old_criteria_not_a_stale_one(
    client_authed_owner, current_user, pg_session_factory
):
    """Distinct concurrent write: the audit chain stays continuous (R0->R1->R2)."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        _seed_rubric(s, ws, _rubric_body("R0"))
        s.commit()
        ws_id = ws.id

    r1, r2 = _rubric_body("R1"), _rubric_body("R2")
    resp = apply_rubric_under_the_mutex(
        pg_session_factory,
        ws_id,
        current_user,
        r1,
        lambda: client_authed_owner.put(f"/api/workspaces/{ws_id}/rubric", json=r2),
    )

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    events = _rubric_events(pg_session_factory, ws_id)
    assert len(events) == 2, f"expected R0->R1 and R1->R2, got {[e.payload for e in events]}"
    # Identify the route's event by its NEW value rather than by ordering: two
    # events written milliseconds apart can tie on created_at, and a tie would
    # decide this assertion by luck.
    r2_criteria, r2_notes = _normalized(r2)
    r1_criteria, r1_notes = _normalized(r1)
    routes = [e for e in events if e.payload["new_criteria"] == r2_criteria]
    assert len(routes) == 1, f"could not identify the route's event: {[e.payload for e in events]}"
    assert routes[0].payload["old_criteria"] == r1_criteria, (
        "the rubric_edited event records old_criteria from the rubric this request "
        "loaded BEFORE the mutex, not the one it is actually replacing — R1 is "
        f"missing from the audit chain: {routes[0].payload['old_criteria']}"
    )
    assert routes[0].payload["old_funder_notes"] == r1_notes
    assert routes[0].payload["new_funder_notes"] == r2_notes


def test_activate_reads_the_rubric_the_mutex_holder_left_not_the_one_it_loaded(
    client_authed_owner, current_user, pg_session_factory
):
    """activate's half of the identical-write gate.

    Worse here than on update_rubric: phases/setup.py's _m3 writes the rubric
    while holding job -> workspace, so the state activate loaded at require_owner
    can be replaced by the setup worker while activate queues on the mutex. The
    holder stands in for that writer (or for a concurrent PUT /rubric, which is
    allowed during setup — update_rubric gates on ownership, not on phase)."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.current_phase = "setup"
        _seed_rubric(s, ws, _rubric_body("R0"))
        s.flush()
        job_id = _setup_job(s, ws.id, "waiting_for_rubric_review")
        s.commit()
        ws_id = ws.id

    body = _rubric_body("R1")
    resp = apply_rubric_under_the_mutex(
        pg_session_factory,
        ws_id,
        current_user,
        body,
        lambda: client_authed_owner.post(f"/api/workspaces/{ws_id}/activate", json=body),
    )

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    events = _rubric_events(pg_session_factory, ws_id)
    assert len(events) == 1, (
        f"expected exactly 1 rubric_edited event for one edit, got {len(events)}: "
        f"{[e.payload for e in events]} — activate compared the submitted rubric "
        "against the copy it loaded BEFORE the commit mutex"
    )
    # The activation itself still happened: the gate is about the audit trail,
    # not about suppressing the route.
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "done"
        assert s.get(Workspace, ws_id).current_phase == "active"


def test_activate_records_the_previous_rubric_as_old_criteria_not_a_stale_one(
    client_authed_owner, current_user, pg_session_factory
):
    """activate's half of the distinct-write gate."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.current_phase = "setup"
        _seed_rubric(s, ws, _rubric_body("R0"))
        s.flush()
        job_id = _setup_job(s, ws.id, "waiting_for_rubric_review")
        s.commit()
        ws_id = ws.id

    r1, r2 = _rubric_body("R1"), _rubric_body("R2")
    resp = apply_rubric_under_the_mutex(
        pg_session_factory,
        ws_id,
        current_user,
        r1,
        lambda: client_authed_owner.post(f"/api/workspaces/{ws_id}/activate", json=r2),
    )

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    events = _rubric_events(pg_session_factory, ws_id)
    assert len(events) == 2, f"expected R0->R1 and R1->R2, got {[e.payload for e in events]}"
    r2_criteria, _ = _normalized(r2)
    r1_criteria, r1_notes = _normalized(r1)
    routes = [e for e in events if e.payload["new_criteria"] == r2_criteria]
    assert len(routes) == 1, f"could not identify the route's event: {[e.payload for e in events]}"
    assert routes[0].payload["old_criteria"] == r1_criteria, (
        "activate recorded old_criteria from the rubric it loaded BEFORE the mutex, "
        f"so R1 is missing from the audit chain: {routes[0].payload['old_criteria']}"
    )
    assert routes[0].payload["old_funder_notes"] == r1_notes
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "done"
