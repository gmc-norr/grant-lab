"""Tests for POST /workspaces/{id}/setup/retry — owner-only recovery endpoint
for a permanently-failed Phase-0 setup job, and for the failed-setup state
exposed in the cockpit-meta read-model.

TDD: these tests MUST fail before the implementation is added.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import User, WorkspaceSetupJob
from grant_lab.main import create_app
from tests.proposal_assist.conftest import make_call, make_user, make_workspace

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _env(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


@pytest.fixture
def _app(_env):
    return create_app()


@pytest.fixture
def owner(_app, pg_session_factory):
    with pg_session_factory() as s:
        u = make_user(s, email="retry-owner@umu.se")
        s.commit()
        uid = u.id
    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        return s.get(User, uid)


@pytest.fixture
def non_owner(_app, pg_session_factory):
    with pg_session_factory() as s:
        u = make_user(s, email="retry-non-owner@umu.se")
        s.commit()
        uid = u.id
    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        return s.get(User, uid)


@pytest.fixture
def _client_owner(_app, owner, pg_session_factory):
    _app.state.session_factory = pg_session_factory
    client = TestClient(_app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = _app.state.session_signer
    client.cookies.set("session", signer.make(user_id=owner.id))
    yield client
    client.__exit__(None, None, None)


@pytest.fixture
def _client_non_owner(_app, non_owner, pg_session_factory):
    _app.state.session_factory = pg_session_factory
    client = TestClient(_app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = _app.state.session_signer
    client.cookies.set("session", signer.make(user_id=non_owner.id))
    yield client
    client.__exit__(None, None, None)


def _make_ws_with_failed_job(pg_session_factory, owner) -> uuid.UUID:
    """Create a workspace with a failed setup job; return workspace id."""
    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ws = make_workspace(s, owner=owner, call_id=call.id, current_phase="setup")
        job = WorkspaceSetupJob(
            workspace_id=ws.id,
            status="failed",
            attempts=5,
            last_error="RuntimeError: something exploded",
        )
        s.add(job)
        s.commit()
        return ws.id


# ---------------------------------------------------------------------------
# Test: retry resets a failed setup job
# ---------------------------------------------------------------------------


def test_setup_retry_resets_failed_job(
    _client_owner,
    owner,
    pg_session_factory,
):
    ws_id = _make_ws_with_failed_job(pg_session_factory, owner)

    resp = _client_owner.post(f"/api/workspaces/{ws_id}/setup/retry")
    assert resp.status_code == 200
    data = resp.json()
    # Response should describe the new job state
    assert data["status"] == "queued"

    # Verify DB state
    with pg_session_factory() as s:
        job = s.scalar(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id)
        )
    assert job is not None
    assert job.status == "queued"
    assert job.attempts == 0
    assert job.started_at is None
    assert job.completed_at is None
    assert job.last_error is None


def test_setup_retry_non_owner_gets_403(
    _client_non_owner,
    owner,
    pg_session_factory,
):
    """Non-owner member (or stranger) cannot retry setup."""
    ws_id = _make_ws_with_failed_job(pg_session_factory, owner)

    resp = _client_non_owner.post(f"/api/workspaces/{ws_id}/setup/retry")
    assert resp.status_code == 403


def test_setup_retry_no_job_at_all_returns_404(
    _client_owner,
    owner,
    pg_session_factory,
):
    """If there is no setup job at all for the workspace, 404 is returned."""
    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ws = make_workspace(s, owner=owner, call_id=call.id, current_phase="setup")
        # No job added — simulates a workspace in a weird partial state
        s.commit()
        ws_id = ws.id

    resp = _client_owner.post(f"/api/workspaces/{ws_id}/setup/retry")
    assert resp.status_code == 404


def test_setup_retry_queued_job_returns_409(
    _client_owner,
    owner,
    pg_session_factory,
):
    """If the job is already queued or running, 409 (already in progress) is returned."""
    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ws = make_workspace(s, owner=owner, call_id=call.id, current_phase="setup")
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="running", attempts=1))
        s.commit()
        ws_id = ws.id

    resp = _client_owner.post(f"/api/workspaces/{ws_id}/setup/retry")
    assert resp.status_code == 409


def test_setup_retry_after_reset_job_is_claimable(
    _client_owner,
    owner,
    pg_session_factory,
):
    """After retry the job is in 'queued' state and can be claimed by the next tick."""
    from grant_lab.proposal_assist.phases.setup import claim_next_setup_job

    ws_id = _make_ws_with_failed_job(pg_session_factory, owner)

    resp = _client_owner.post(f"/api/workspaces/{ws_id}/setup/retry")
    assert resp.status_code == 200

    # The worker tick should now be able to claim it
    with pg_session_factory() as s:
        job = claim_next_setup_job(session=s)
        s.commit()

    assert job is not None
    assert job.workspace_id == ws_id
    assert job.status == "running"


# ---------------------------------------------------------------------------
# Test: cockpit-meta read-model exposes failed-setup state
# ---------------------------------------------------------------------------


def test_cockpit_meta_exposes_setup_status_failed(
    _client_owner,
    owner,
    pg_session_factory,
):
    """When the setup job is 'failed', cockpit-meta.setup_status == 'failed'
    and cockpit-meta.setup_last_error is non-null for the owner."""
    ws_id = _make_ws_with_failed_job(pg_session_factory, owner)

    resp = _client_owner.get(f"/api/workspaces/{ws_id}/cockpit-meta")
    assert resp.status_code == 200
    data = resp.json()
    assert data["setup_status"] == "failed"
    assert data["setup_last_error"] is not None
    assert "exploded" in data["setup_last_error"]


def test_cockpit_meta_hides_setup_last_error_from_non_owner_member(
    _client_non_owner,
    non_owner,
    owner,
    pg_session_factory,
):
    """R2-M4: setup_last_error may carry internal diagnostics, so it must be
    HIDDEN (None) from a non-owner MEMBER — while setup_status (a non-PII state)
    is still visible. The member must be a viewer so they pass require_member;
    the owner-gate on setup_last_error is the only thing under test."""
    from grant_lab.db.models import WorkspaceViewer

    ws_id = _make_ws_with_failed_job(pg_session_factory, owner)
    # Add the non-owner as a viewer so cockpit-meta's require_member passes.
    with pg_session_factory() as s:
        s.add(WorkspaceViewer(workspace_id=ws_id, user_id=non_owner.id))
        s.commit()

    resp = _client_non_owner.get(f"/api/workspaces/{ws_id}/cockpit-meta")
    assert resp.status_code == 200
    data = resp.json()
    # Member sees the status (non-PII) ...
    assert data["setup_status"] == "failed"
    assert data["role"] == "member"
    # ... but NOT the internal last_error.
    assert data["setup_last_error"] is None


def test_cockpit_meta_setup_status_waiting_for_rubric_review(
    _client_owner,
    owner,
    pg_session_factory,
):
    """When setup job is 'waiting_for_rubric_review', setup_status reflects that."""
    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ws = make_workspace(s, owner=owner, call_id=call.id, current_phase="setup")
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="waiting_for_rubric_review"))
        s.commit()
        ws_id = ws.id

    resp = _client_owner.get(f"/api/workspaces/{ws_id}/cockpit-meta")
    assert resp.status_code == 200
    data = resp.json()
    assert data["setup_status"] == "waiting_for_rubric_review"
    assert data["setup_last_error"] is None


def test_cockpit_meta_setup_status_none_when_active(
    _client_owner,
    owner,
    pg_session_factory,
):
    """When workspace is 'active' (done phase), setup_status is 'done' or None."""
    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ws = make_workspace(s, owner=owner, call_id=call.id, current_phase="active")
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="done"))
        s.commit()
        ws_id = ws.id

    resp = _client_owner.get(f"/api/workspaces/{ws_id}/cockpit-meta")
    assert resp.status_code == 200
    data = resp.json()
    assert data["setup_status"] == "done"
    assert data["setup_last_error"] is None
