"""Direct tests of `workspace_write` — the transaction scope the six
proposal-assist owner-write routes run their body inside.

These exercise the manager itself, NOT a route. The route-level behaviour is
already pinned by tests/proposal_assist/test_api_writers_mutex_straddle.py,
which this slice must leave untouched; what is NOT pinned there is the
manager's contract when the caller does something the six routes happen not to
do — most importantly, catching the exception and reusing the session.
"""

from __future__ import annotations

import functools
import threading
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

import call_manager.proposal_assist.web._writes as writes_module
from call_manager.db.models import Workspace, WorkspaceEvent
from call_manager.proposal_assist.drafting.locking import (
    BUSY_ERROR_MESSAGE,
    WorkspaceDeleted,
    acquire_commit_mutex,
)
from call_manager.proposal_assist.web._writes import workspace_write
from tests.proposal_assist.conftest import make_workspace


def _hold_mutex(pg_session_factory, ws_id):
    """Hold the commit mutex until the returned Event is set. Mirrors
    `wedge_mutex` in test_api_writers_mutex_straddle.py; duplicated rather than
    imported because that file must not be edited by this slice."""
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
    assert holding.wait(timeout=5), "the holder never acquired the mutex"
    return release, t


def _hold_workspace_row(pg_session_factory, ws_id):
    """Hold the `workspaces` row FOR UPDATE, taking NO mutex, until the returned
    Event is set — so a manager-scoped body that takes the same row blocks and
    its bound fires."""
    holding = threading.Event()
    release = threading.Event()

    def _holder():
        with pg_session_factory() as s:
            s.get(Workspace, ws_id, with_for_update=True)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    t = threading.Thread(target=_holder)
    t.start()
    assert holding.wait(timeout=5), "the holder never took the workspaces row"
    return release, t


def _short_mutex_wait(monkeypatch, seconds: int = 1) -> None:
    """Shorten the manager's mutex wait from the production 10s.

    Patches the module global `workspace_write` looks up at call time. This only
    works because the manager calls `acquire_commit_mutex_live(session, ws_id)`
    with NO timeout_seconds keyword — a call-time keyword would override this
    partial's and silently restore the 10s default (spec §3).
    """
    monkeypatch.setattr(
        writes_module,
        "acquire_commit_mutex_live",
        functools.partial(writes_module.acquire_commit_mutex_live, timeout_seconds=seconds),
    )


def test_a_clean_exit_commits(pg_session_factory, current_user):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        with workspace_write(s, ws_id, timeout_seconds=2, what="test"):
            ws.title = "written under the manager"

    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).title == "written under the manager"


def _assert_busy(exc: HTTPException) -> None:
    assert exc.status_code == 429
    assert exc.detail == BUSY_ERROR_MESSAGE
    assert exc.headers == {"Retry-After": "2"}


def test_a_wedged_mutex_raises_the_contracted_429(pg_session_factory, current_user, monkeypatch):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    _short_mutex_wait(monkeypatch)
    release, t = _hold_mutex(pg_session_factory, ws_id)
    try:
        with pg_session_factory() as s:
            with pytest.raises(HTTPException) as exc:  # noqa: SIM117
                with workspace_write(s, ws_id, timeout_seconds=2, what="test"):
                    pytest.fail("the body must not run when the mutex is wedged")
            _assert_busy(exc.value)
            assert not s.in_transaction(), (
                "workspace_write left the transaction open after a mutex timeout"
            )
    finally:
        release.set()
        t.join(timeout=30)


def test_a_held_row_raises_the_contracted_429(pg_session_factory, current_user):
    """The RowLockTimeout arm. No route uses the manager yet, so if this
    translation is broken nothing else in the suite catches it.

    The manager does not take rows — the body does, exactly as the migrated
    routes will. The manager's job is that the resulting 55P03 comes back as the
    contracted 429 rather than escaping as a raw OperationalError (a 500).
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    release, t = _hold_workspace_row(pg_session_factory, ws_id)
    try:
        with pg_session_factory() as s:
            ws = s.get(Workspace, ws_id)
            with pytest.raises(HTTPException) as exc:  # noqa: SIM117
                with workspace_write(s, ws_id, timeout_seconds=1, what="test"):
                    # Blocks on the holder; the bound fires after 1s.
                    s.refresh(ws, with_for_update=True)
            _assert_busy(exc.value)
            assert not s.in_transaction(), (
                "workspace_write left the transaction open after a row-lock timeout — "
                "a caller that reuses this session hits 'current transaction is aborted'"
            )
    finally:
        release.set()
        t.join(timeout=30)


def test_a_body_HTTPException_propagates_unchanged_and_commits_nothing(
    pg_session_factory, current_user
):
    """The exit `retry_setup` (404/409) and `activate` (409) depend on. Tested
    with the exception type they actually raise, not a stand-in."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        with pytest.raises(HTTPException) as exc:  # noqa: SIM117
            with workspace_write(s, ws_id, timeout_seconds=2, what="test"):
                s.add(
                    WorkspaceEvent(
                        workspace_id=ws_id,
                        kind="should_never_land_409",
                        actor_id=current_user.id,
                        payload={},
                    )
                )
                raise HTTPException(status_code=409, detail="not in that state")
        assert exc.value.status_code == 409, "the manager must not rewrite a body HTTPException"
        assert exc.value.detail == "not in that state"
        assert not s.in_transaction()
        # Reuse the SAME session and commit it, as a non-route caller may. The
        # manager's rollback must already have discarded the staged event, so
        # this commits an empty transaction; without the rollback the staged
        # WorkspaceEvent would be pending here and this commit would persist it,
        # which the fresh-session check below then catches.
        s.commit()

    with pg_session_factory() as s:
        kinds = [e.kind for e in s.query(WorkspaceEvent).filter_by(workspace_id=ws_id).all()]
    assert "should_never_land_409" not in kinds


def test_a_deleted_workspace_raises_WorkspaceDeleted_not_a_429(pg_session_factory, current_user):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.soft_deleted_at = datetime.now(UTC)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted):  # noqa: SIM117
            with workspace_write(s, ws_id, timeout_seconds=2, what="test"):
                pytest.fail("the body must not run for a deleted workspace")
        assert not s.in_transaction(), (
            "workspace_write left the transaction open after WorkspaceDeleted"
        )


def test_a_body_exception_propagates_and_rolls_back_for_a_REUSED_session(
    pg_session_factory, current_user
):
    """The contract that cannot be proved by throwing the session away.

    The six routes each sit inside `with session_factory() as session:`, whose
    exit closes the session and discards the transaction — so "nothing is
    committed" holds today by accident of the caller. This test catches the
    exception OUTSIDE the manager, keeps using the same session, and commits it.
    Without the manager's rollback the body's write persists and this fails.
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        with pytest.raises(RuntimeError, match="deliberate"):  # noqa: SIM117
            with workspace_write(s, ws_id, timeout_seconds=2, what="test"):
                s.add(
                    WorkspaceEvent(
                        workspace_id=ws_id,
                        kind="should_never_land",
                        actor_id=current_user.id,
                        payload={},
                    )
                )
                raise RuntimeError("deliberate")
        # The caller carries on with the SAME session, as a non-route caller may.
        s.commit()

    with pg_session_factory() as s:
        kinds = [
            e.kind
            for e in s.query(WorkspaceEvent).filter_by(workspace_id=ws_id).all()
        ]
    assert "should_never_land" not in kinds, (
        "the body's write survived a body exception — workspace_write did not "
        "roll back, so a caller that reuses the session persists a write the "
        "route decided against"
    )


def test_a_timeout_leaves_the_session_usable_rather_than_aborted(
    pg_session_factory, current_user, monkeypatch
):
    """After a 55P03 the Postgres transaction is aborted and refuses further
    statements. The manager's rollback is what clears it; without it a caller
    that catches the 429 and reuses the session gets a confusing failure far
    from its cause."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    _short_mutex_wait(monkeypatch)
    release, t = _hold_mutex(pg_session_factory, ws_id)
    try:
        with pg_session_factory() as s:
            with pytest.raises(HTTPException):  # noqa: SIM117
                with workspace_write(s, ws_id, timeout_seconds=2, what="test"):
                    pytest.fail("unreachable")
            # Must not raise "current transaction is aborted".
            assert s.get(Workspace, ws_id) is not None
    finally:
        release.set()
        t.join(timeout=30)
