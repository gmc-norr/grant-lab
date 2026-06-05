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


@pytest.mark.asyncio
async def test_sse_poll_loop_handles_deleted_turn_job(
    pg_session_factory,
    current_user,
):
    """Unguarded-deref regression: if the TurnJob row disappears AFTER the
    generator seeds (e.g. a concurrent purge) the poll loop's
    ``s.get(TurnJob, ...)`` returns None. The loop must terminate cleanly
    (emit 'done') instead of raising AttributeError on ``tj.status``."""
    from call_manager.db.models import ChatTurn, TurnJob
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        user_turn = ChatTurn(
            workspace_id=ws.id,
            role="user",
            content={"type": "text", "text": "hi"},
        )
        s.add(user_turn)
        s.flush()
        job = TurnJob(
            workspace_id=ws.id,
            user_turn_id=user_turn.id,
            submitted_by_user_id=current_user.id,
            status="running",
            queued_at=datetime.now(UTC),
            started_at=datetime.now(UTC),
        )
        s.add(job)
        s.commit()
        wsid, jid = ws.id, job.id

    # session_factory wrapper: the generator's FIRST session (the seed
    # block) sees the live job; before the SECOND session (the poll loop)
    # opens, delete the job so ``s.get(TurnJob, ...)`` returns None.
    call_count = {"n": 0}

    class _Factory:
        def __call__(self):
            call_count["n"] += 1
            if call_count["n"] == 2:
                # Delete the job right before the poll-loop session opens.
                with pg_session_factory() as s:
                    s.query(TurnJob).filter_by(id=jid).delete()
                    s.commit()
            return pg_session_factory()

    from call_manager.proposal_assist.web.workspaces import _stream_turn_generator

    events = []
    async for chunk in _stream_turn_generator(
        session_factory=_Factory(),
        workspace_id=wsid,
        turn_job_id=jid,
        poll_seconds=0.05,
    ):
        events.append(chunk)
        if len(events) > 20:
            pytest.fail("stream did not terminate")
    full = "".join(events)
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


def test_render_chat_turn_html_strips_carriage_return():
    """SSE-injection regression: a turn whose content contains a bare
    ``\\r`` must NOT produce an extra SSE line terminator. The rendered
    snippet is interpolated into ``data: {rendered}\\n\\n``; a raw CR is a
    valid SSE record separator, so it would let user text inject extra
    SSE records. _render_chat_turn_html strips both CR and LF, so the
    snippet stays a single SSE data record."""
    import uuid

    from call_manager.db.models import ChatTurn
    from call_manager.proposal_assist.web.workspaces import _render_chat_turn_html

    turn = ChatTurn(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        role="user",
        content={"type": "text", "text": "line1\rline2"},
        position=1,
    )
    snippet = _render_chat_turn_html(turn)
    # No CR / LF survives, so the SSE framing stays a single record.
    assert "\r" not in snippet
    assert "\n" not in snippet
    # The emitted SSE record (data: {snippet}\n\n) has exactly one
    # terminator pair — splitting on the SSE record separator yields one
    # non-empty payload.
    sse_record = f"data: {snippet}\n\n"
    payloads = [p for p in sse_record.split("\r\n\r\n") if p.strip()]
    assert sse_record.count("\r") == 0
    assert len(payloads) == 1
