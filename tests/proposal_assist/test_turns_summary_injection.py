"""Compaction session-reset summary re-injection (residual follow-up).

When compaction resets the SDK session (workspaces.last_sdk_session_id -> NULL),
the next turn starts a FRESH server-side session with no history. The processor
re-injects the latest chat_summaries.summary into that first turn so the model
keeps prior context, and only while the session is cold (id is NULL) so it fires
exactly once per reset.
"""

import pytest
from sqlalchemy import select

from call_manager.db.models import ChatSummary, Workspace
from call_manager.proposal_assist.drafting.contract import AssistantTextEvent, TurnDoneEvent
from call_manager.proposal_assist.drafting.controller import WorkspaceController
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_manager.proposal_assist.drafting.registry import SubprocessRegistry
from call_manager.proposal_assist.turns.processor import _SUMMARY_PREAMBLE, _prepend_summary
from call_manager.proposal_assist.turns.submit import submit_user_turn
from tests.proposal_assist.conftest import make_chat_turn, make_user, make_workspace


def test_prepend_summary_str_shape():
    out = _prepend_summary("Please continue.", "Earlier we drafted aims.")
    assert isinstance(out, str)
    assert out.startswith(_SUMMARY_PREAMBLE)
    assert "Earlier we drafted aims." in out
    assert out.rstrip().endswith("Please continue.")


def test_prepend_summary_list_shape():
    blocks = [{"type": "text", "text": "look at this"}, {"type": "image", "source": {}}]
    out = _prepend_summary(blocks, "SUMMARY")
    assert isinstance(out, list)
    assert out[0] == {"type": "text", "text": f"{_SUMMARY_PREAMBLE}\n\nSUMMARY"}
    # Original blocks preserved after the injected one.
    assert out[1:] == blocks


def _setup_workspace_with_summary(pg_session_factory, *, last_sdk_session_id):
    """Create a workspace with one prior summary, returning (uid, wsid)."""
    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        ws.last_sdk_session_id = last_sdk_session_id
        s.flush()
        boundary = make_chat_turn(
            s, ws, role="assistant", content={"type": "text", "text": "earlier"}
        )
        s.flush()
        s.add(
            ChatSummary(
                workspace_id=ws.id,
                summary="The user wants a grant on cancer immunotherapy.",
                summarized_through=boundary.id,
                tokens=12,
                model="claude-haiku-4-5",
            )
        )
        s.commit()
        return user.id, ws.id


def _build_controller(pg_session_factory):
    adapter = FakeAgentAdapter()
    adapter.script_next_turn(
        [
            AssistantTextEvent(text="Sure."),
            TurnDoneEvent(tokens_in=5, tokens_out=5, model="fake", session_id="new-session"),
        ]
    )
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(registry=registry, session_factory=pg_session_factory)
    return controller, adapter


@pytest.mark.asyncio
async def test_summary_injected_on_cold_session(pg_session_factory, _settings_env):
    from call_manager.config import Settings
    from call_manager.proposal_assist.turns.processor import process_turn_once

    uid, wsid = _setup_workspace_with_summary(pg_session_factory, last_sdk_session_id=None)
    controller, adapter = _build_controller(pg_session_factory)

    with pg_session_factory() as s:
        job = submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="Continue please",
            submitted_by_user_id=uid,
            settings=Settings(),
        )
        s.commit()
        job_id = job.id

    await process_turn_once(
        session_factory=pg_session_factory, controller=controller, turn_job_id=job_id
    )

    sent = adapter.last_user_message
    assert isinstance(sent, str)
    assert _SUMMARY_PREAMBLE in sent
    assert "cancer immunotherapy" in sent
    assert sent.rstrip().endswith("Continue please")
    # The completed turn persisted the new session id, so a subsequent turn
    # would no longer be cold (and would not re-inject).
    with pg_session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.id == wsid))
        assert ws.last_sdk_session_id == "new-session"


@pytest.mark.asyncio
async def test_summary_not_injected_on_resumed_session(pg_session_factory, _settings_env):
    from call_manager.config import Settings
    from call_manager.proposal_assist.turns.processor import process_turn_once

    uid, wsid = _setup_workspace_with_summary(
        pg_session_factory, last_sdk_session_id="existing-session"
    )
    controller, adapter = _build_controller(pg_session_factory)

    with pg_session_factory() as s:
        job = submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="Continue please",
            submitted_by_user_id=uid,
            settings=Settings(),
        )
        s.commit()
        job_id = job.id

    await process_turn_once(
        session_factory=pg_session_factory, controller=controller, turn_job_id=job_id
    )

    sent = adapter.last_user_message
    assert isinstance(sent, str)
    assert _SUMMARY_PREAMBLE not in sent
    assert sent == "Continue please"
