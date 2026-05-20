import pytest
from sqlalchemy import select

from call_manager.db.models import ChatTurn, TurnJob, WorkspaceEvent
from call_manager.proposal_assist.turns.caps import TokenCapExceeded
from call_manager.proposal_assist.turns.submit import submit_user_turn
from tests.proposal_assist.conftest import (
    make_chat_turn,
    make_user,
    make_workspace,
)


def test_submit_user_turn_writes_chat_turn_and_turn_job(
    pg_session_factory,
    _settings_env,
):
    from call_manager.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        s.commit()
        uid, wsid = user.id, ws.id

    with pg_session_factory() as s:
        job = submit_user_turn(
            session=s,
            workspace_id=wsid,
            content={"type": "text", "text": "Hello agent."},
            submitted_by_user_id=uid,
            settings=settings,
        )
        s.commit()
        job_id = job.id

    with pg_session_factory() as s:
        turn_rows = list(s.scalars(select(ChatTurn).where(ChatTurn.workspace_id == wsid)))
        job_row = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        events = list(
            s.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wsid,
                    WorkspaceEvent.kind == "cloud_turn_sent",
                )
            )
        )

    assert len(turn_rows) == 1
    assert turn_rows[0].role == "user"
    assert turn_rows[0].content == {"type": "text", "text": "Hello agent."}
    assert job_row.status == "queued"
    assert job_row.user_turn_id == turn_rows[0].id
    assert job_row.submitted_by_user_id == uid
    assert job_row.queued_at is not None
    # Audit event written at submit-time so it survives a crash before finalize.
    assert len(events) == 1
    assert events[0].actor_id == uid
    assert events[0].payload["turn_job_id"] == str(job_id)
    assert events[0].payload["phase"] == "queued"


def test_submit_user_turn_raises_when_input_cap_exceeded(
    pg_session_factory,
    _settings_env,
    monkeypatch,
):
    monkeypatch.setenv("DAILY_TOKEN_CAP_INPUT", "100")
    from call_manager.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        make_chat_turn(
            s, ws, role="assistant", content={"type": "text", "text": "x"}, tokens_in=200
        )
        s.commit()
        uid, wsid = user.id, ws.id

    with pg_session_factory() as s:
        with pytest.raises(TokenCapExceeded) as exc_info:
            submit_user_turn(
                session=s,
                workspace_id=wsid,
                content={"type": "text", "text": "hi"},
                submitted_by_user_id=uid,
                settings=settings,
            )
        s.rollback()

    assert exc_info.value.reason == "input_cap_exceeded"

    # No rows written.
    with pg_session_factory() as s:
        user_rows = list(
            s.scalars(
                select(ChatTurn).where(
                    ChatTurn.workspace_id == wsid,
                    ChatTurn.role == "user",
                )
            )
        )
        jobs = list(s.scalars(select(TurnJob).where(TurnJob.workspace_id == wsid)))
        events = list(
            s.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wsid,
                    WorkspaceEvent.kind == "cloud_turn_sent",
                )
            )
        )
    assert user_rows == []
    assert jobs == []
    assert events == []  # audit row also rolled back


def test_submit_user_turn_accepts_null_submitter_for_system_initiated(
    pg_session_factory,
    _settings_env,
):
    """System-initiated turns (future auto-summarization, recovery)
    legitimately have no user actor — pass None."""
    from call_manager.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    with pg_session_factory() as s:
        job = submit_user_turn(
            session=s,
            workspace_id=wsid,
            content={"type": "text", "text": "system message"},
            submitted_by_user_id=None,
            settings=settings,
        )
        s.commit()
        job_id = job.id

    with pg_session_factory() as s:
        job_row = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        events = list(
            s.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wsid,
                    WorkspaceEvent.kind == "cloud_turn_sent",
                )
            )
        )
    assert job_row.submitted_by_user_id is None
    assert events[0].actor_id is None
