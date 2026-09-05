"""End-to-end: submit_user_turn → drain_turn_queue_tick → processor →
all rows materialized. Then second turn proves resume_session_id flows."""

import pytest
from sqlalchemy import select

from grant_lab.db.models import ChatTurn, TurnJob, Workspace, WorkspaceEvent
from grant_lab.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnDoneEvent,
)
from grant_lab.proposal_assist.drafting.controller import WorkspaceController
from grant_lab.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from grant_lab.proposal_assist.drafting.registry import SubprocessRegistry
from grant_lab.proposal_assist.turns.drain import drain_turn_queue_tick
from grant_lab.proposal_assist.turns.submit import submit_user_turn
from tests.proposal_assist.conftest import make_user, make_workspace


@pytest.mark.asyncio
async def test_full_turn_pipeline(pg_session_factory, _settings_env):
    """Submit → drain → processor → expected state.

    Then submit a second turn and verify resume_session_id flows."""
    from grant_lab.config import Settings

    settings = Settings()
    adapter = FakeAgentAdapter()
    adapter.script_next_turn(
        [
            AssistantTextEvent(text="Reading the spec..."),
            ToolUseEvent(tool_name="read_artifact", tool_use_id="u1", input={}),
            ToolResultEvent(tool_use_id="u1", content="<spec contents>", is_error=False),
            AssistantTextEvent(text="Done."),
            TurnDoneEvent(tokens_in=100, tokens_out=50, model="fake", session_id="sess-A"),
        ]
    )
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )

    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        s.commit()
        uid, wsid = user.id, ws.id
    with pg_session_factory() as s:
        job = submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="Get the spec.",
            submitted_by_user_id=uid,
            settings=settings,
        )
        s.commit()
        job_id = job.id

    await drain_turn_queue_tick(
        session_factory=pg_session_factory,
        controller=controller,
    )

    with pg_session_factory() as s:
        turns = list(
            s.scalars(
                select(ChatTurn).where(ChatTurn.workspace_id == wsid).order_by(ChatTurn.position)
            )
        )
        job_row = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        ws_row = s.scalar(select(Workspace).where(Workspace.id == wsid))
        sent_events = list(
            s.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wsid,
                    WorkspaceEvent.kind == "cloud_turn_sent",
                )
            )
        )
        completed_events = list(
            s.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wsid,
                    WorkspaceEvent.kind == "cloud_turn_completed",
                )
            )
        )

    assert [t.role for t in turns] == ["user", "assistant", "assistant", "tool", "assistant"]
    assert job_row.status == "completed"
    assert ws_row.last_sdk_session_id == "sess-A"
    # Audit-at-submit + outcome event = full trace.
    assert len(sent_events) == 1
    assert sent_events[0].actor_id == uid
    assert sent_events[0].payload["phase"] == "queued"
    assert len(completed_events) == 1
    assert completed_events[0].payload["tokens_in"] == 100

    # Now submit a SECOND turn. Terminate first to force a fresh launch
    # (otherwise the cached subprocess is reused and resume_session_id
    # isn't passed again).
    await controller.terminate(wsid)
    adapter.script_next_turn(
        [
            AssistantTextEvent(text="hi again"),
            TurnDoneEvent(tokens_in=10, tokens_out=5, model="fake", session_id="sess-B"),
        ]
    )

    with pg_session_factory() as s:
        submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="Another turn.",
            submitted_by_user_id=uid,
            settings=settings,
        )
        s.commit()

    await drain_turn_queue_tick(
        session_factory=pg_session_factory,
        controller=controller,
    )

    # The second launch's LaunchContext should have resume_session_id='sess-A'.
    assert adapter.launches[-1].resume_session_id == "sess-A"

    with pg_session_factory() as s:
        ws_row = s.scalar(select(Workspace).where(Workspace.id == wsid))
    assert ws_row.last_sdk_session_id == "sess-B"
