from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import ChatTurn, TurnJob, WorkspaceEvent
from call_manager.proposal_assist.turns.outcomes import outcome_message, record_terminal_outcome
from call_manager.proposal_assist.turns.processor import _finalize_failed
from tests.proposal_assist.conftest import make_chat_turn, make_user, make_workspace


def test_system_role_chat_turn_inserts(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(
            ChatTurn(
                workspace_id=ws.id,
                role="system",
                content={"type": "turn_outcome", "outcome": "failed", "text": "x"},
            )
        )
        s.commit()
        row = s.scalar(select(ChatTurn).where(ChatTurn.workspace_id == ws.id))
        assert row.role == "system"


def test_unknown_role_still_rejected(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(ChatTurn(workspace_id=ws.id, role="bogus", content={"type": "text", "text": "x"}))
        with pytest.raises(IntegrityError):
            s.commit()


_FINISH_MSG = "The drafting assistant couldn't finish this turn. Please try again."


@pytest.mark.parametrize(
    "outcome, raw_error, expected",
    [
        ("cancelled", None, "This turn was cancelled before it started."),
        ("cancelled", "workspace soft-deleted", "This turn was cancelled before it started."),
        (
            "failed",
            "reaped: stuck in running > 600s (workspace wedged)",
            "This turn was stopped after running too long.",
        ),
        ("failed", "orphaned", "This turn was interrupted. Please try again."),
        ("failed", "sdk_turn_error: boom", _FINISH_MSG),
        ("failed", "processor_failure: ValueError: secret/path/leak", _FINISH_MSG),
    ],
)
def test_outcome_message_maps_safely(outcome, raw_error, expected):
    msg = outcome_message(outcome, raw_error)
    assert msg == expected
    if raw_error:
        # raw diagnostics must never leak into the viewer-visible string
        assert raw_error not in msg


def _running_job(s, ws, *, submitter, tokens_in=42):
    user_turn = make_chat_turn(
        s, ws, role="user", content={"type": "text", "text": "hi"}, tokens_in=tokens_in
    )
    job = TurnJob(
        workspace_id=ws.id,
        user_turn_id=user_turn.id,
        submitted_by_user_id=submitter.id,
        status="running",
        started_at=datetime.now(UTC),
    )
    s.add(job)
    s.flush()
    return job, user_turn


def test_records_failed_outcome_row_event_and_status(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        job, user_turn = _running_job(s, ws, submitter=owner)
        s.commit()
        ok = record_terminal_outcome(
            s,
            turn_job_id=job.id,
            workspace_id=ws.id,
            outcome="failed",
            expected_status="running",
            raw_error="sdk_turn_error: boom",
            tokens_in=10,
            tokens_out=5,
        )
        s.commit()
        assert ok is True
        jid, utid, wsid, oid = job.id, user_turn.id, ws.id, owner.id

    with pg_session_factory() as s:
        j = s.get(TurnJob, jid)
        assert j.status == "failed"
        assert j.completed_at is not None
        row = s.scalar(
            select(ChatTurn).where(ChatTurn.role == "system", ChatTurn.workspace_id == wsid)
        )
        assert row.content["type"] == "turn_outcome"
        assert row.content["outcome"] == "failed"
        assert row.content["text"] == (
            "The drafting assistant couldn't finish this turn. Please try again."
        )
        assert row.content["turn_job_id"] == str(jid)
        assert row.content["user_turn_id"] == str(utid)
        assert "boom" not in row.content["text"]
        ev = s.scalar(select(WorkspaceEvent).where(WorkspaceEvent.kind == "cloud_turn_failed"))
        assert ev.payload["error"] == "sdk_turn_error: boom"
        assert ev.actor_id == oid
        assert s.get(ChatTurn, utid).tokens_in is None


def test_expected_status_mismatch_returns_false_and_writes_nothing(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        job, _ = _running_job(s, ws, submitter=owner)
        job.status = "completed"  # already terminal
        s.commit()
        ok = record_terminal_outcome(
            s,
            turn_job_id=job.id,
            workspace_id=ws.id,
            outcome="failed",
            expected_status="running",
            raw_error="late",
        )
        s.commit()
        assert ok is False
        assert s.get(TurnJob, job.id).status == "completed"
        assert s.scalar(select(ChatTurn).where(ChatTurn.role == "system")) is None
        assert (
            s.scalar(
                select(WorkspaceEvent).where(WorkspaceEvent.kind == "cloud_turn_failed")
            )
            is None
        )


def test_wrong_workspace_returns_false(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        other = make_workspace(s, owner=owner)
        job, _ = _running_job(s, ws, submitter=owner)
        s.commit()
        ok = record_terminal_outcome(
            s,
            turn_job_id=job.id,
            workspace_id=other.id,
            outcome="failed",
            expected_status="running",
            raw_error="x",
        )
        s.commit()
        assert ok is False
        assert s.get(TurnJob, job.id).status == "running"


def test_emit_chat_false_writes_event_but_no_row(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        user_turn = make_chat_turn(s, ws, role="user", content={"type": "text", "text": "hi"})
        job = TurnJob(
            workspace_id=ws.id, user_turn_id=user_turn.id,
            submitted_by_user_id=owner.id, status="queued",
        )
        s.add(job)
        s.commit()
        ok = record_terminal_outcome(
            s, turn_job_id=job.id, workspace_id=ws.id, outcome="cancelled",
            expected_status="queued", raw_error="workspace soft-deleted", emit_chat=False,
        )
        s.commit()
        assert ok is True
        jid = job.id

    with pg_session_factory() as s:
        assert s.get(TurnJob, jid).status == "cancelled"
        assert s.scalar(select(ChatTurn).where(ChatTurn.role == "system")) is None
        assert s.scalar(
            select(WorkspaceEvent).where(WorkspaceEvent.kind == "cloud_turn_cancelled")
        ) is not None


def test_finalize_failed_wrapper_writes_row_and_event(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        job, user_turn = _running_job(s, ws, submitter=owner)
        s.commit()
        wsid, jid, utid = ws.id, job.id, user_turn.id
        oid = owner.id

    _finalize_failed(
        pg_session_factory,
        turn_job_id=jid,
        workspace_id=wsid,
        submitted_by_user_id=oid,
        error="sdk_turn_error: kaboom",
        tokens_in=3,
        tokens_out=1,
        user_turn_id=utid,
    )

    with pg_session_factory() as s:
        assert s.get(TurnJob, jid).status == "failed"
        row = s.scalar(
            select(ChatTurn).where(ChatTurn.role == "system", ChatTurn.workspace_id == wsid)
        )
        assert row.content["outcome"] == "failed"
        ev = s.scalar(
            select(WorkspaceEvent).where(WorkspaceEvent.kind == "cloud_turn_failed")
        )
        assert ev is not None
        assert s.get(ChatTurn, utid).tokens_in is None


def test_finalize_failed_wrapper_noop_on_already_terminal(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        job, _ = _running_job(s, ws, submitter=owner)
        job.status = "completed"  # already terminal — wrapper must be a clean no-op
        s.commit()
        wsid, jid, oid = ws.id, job.id, owner.id

    _finalize_failed(
        pg_session_factory,
        turn_job_id=jid,
        workspace_id=wsid,
        submitted_by_user_id=oid,
        error="late",
        tokens_in=0,
        tokens_out=0,
    )

    with pg_session_factory() as s:
        assert s.get(TurnJob, jid).status == "completed"  # unchanged
        assert s.scalar(select(ChatTurn).where(ChatTurn.role == "system")) is None
        assert (
            s.scalar(select(WorkspaceEvent).where(WorkspaceEvent.kind == "cloud_turn_failed"))
            is None
        )
