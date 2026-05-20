"""SSE bridge tests. The simplest robust approach: invoke the route's
underlying async generator directly (skip HTTP), so we don't have to
coordinate TestClient.stream with background writes."""

from datetime import UTC, datetime

import pytest


@pytest.mark.asyncio
async def test_sse_emits_new_chat_turns_then_done(
    pg_session_factory,
    current_user,
):
    """End-to-end of the underlying generator: it emits a 'message' event
    per new chat_turn AND a final 'done' event when the turn_job is in
    a terminal state."""
    from call_manager.db.models import ChatTurn, TurnJob
    from tests.proposal_assist.conftest import make_chat_turn, make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        user_turn = ChatTurn(
            workspace_id=ws.id,
            role="user",
            content={"type": "text", "text": "hi"},
        )
        s.add(user_turn)
        s.flush()
        # turn_job in 'running' state (the processor would have moved it
        # there before writing assistant rows).
        job = TurnJob(
            workspace_id=ws.id,
            user_turn_id=user_turn.id,
            submitted_by_user_id=current_user.id,
            status="running",
            queued_at=datetime.now(UTC),
            started_at=datetime.now(UTC),
        )
        s.add(job)
        # Write the assistant reply BEFORE the stream opens — the
        # generator's first poll will pick it up immediately.
        make_chat_turn(s, ws, role="assistant", content={"type": "text", "text": "hello back"})
        s.commit()
        wsid, jid = ws.id, job.id

    # Mark job completed so the generator emits 'done' and returns.
    with pg_session_factory() as s:
        s.get(TurnJob, jid).status = "completed"
        s.get(TurnJob, jid).completed_at = datetime.now(UTC)
        s.commit()

    # Drive the underlying gen() function directly (bypass HTTP).
    # We import _stream_turn_generator (the implementer extracts the
    # generator body into a module-level helper for testability).
    from call_manager.proposal_assist.web.workspaces import (
        _stream_turn_generator,
    )

    events = []
    async for chunk in _stream_turn_generator(
        session_factory=pg_session_factory,
        workspace_id=wsid,
        turn_job_id=jid,
        poll_seconds=0.05,  # speed up the test
    ):
        events.append(chunk)
        if len(events) > 20:
            pytest.fail("stream did not terminate")
    # Should have at least one 'message' event with our reply text
    # and a final 'event: done' marker.
    full = "".join(events)
    assert "hello back" in full
    assert "event: done" in full


def test_render_chat_turn_html_escapes_text_content():
    """H2 regression: text content with HTML tags is escaped."""
    import uuid

    from call_manager.db.models import ChatTurn
    from call_manager.proposal_assist.web.workspaces import _render_chat_turn_html

    turn = ChatTurn(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        role="assistant",
        content={"type": "text", "text": "<script>alert('x')</script>"},
        position=1,
    )
    snippet = _render_chat_turn_html(turn)
    assert "<script>" not in snippet
    assert "&lt;script&gt;" in snippet


def test_render_chat_turn_html_escapes_tool_name():
    """H2 regression: tool_use names are escaped (tool names come from
    the SDK; even though we control our tools, future MCP additions
    could land untrusted names)."""
    import uuid

    from call_manager.db.models import ChatTurn
    from call_manager.proposal_assist.web.workspaces import _render_chat_turn_html

    turn = ChatTurn(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        role="assistant",
        content={"type": "tool_use", "id": "u1", "name": "<img>", "input": {}},
        position=1,
    )
    snippet = _render_chat_turn_html(turn)
    assert "<img>" not in snippet
    assert "&lt;img&gt;" in snippet
