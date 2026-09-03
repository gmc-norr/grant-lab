from unittest.mock import MagicMock

from sqlalchemy import select

from call_manager.db.models import ChatSummary
from call_manager.proposal_assist.turns.compaction import (
    compact_workspace,
    needs_compaction,
    run_compaction_tick,
)
from tests.proposal_assist.conftest import make_chat_turn, make_user, make_workspace


def _stub_summarizer_client(text: str):
    """Build a stub AnthropicClient-like object whose chat() returns
    the given text in the standard envelope shape."""
    client = MagicMock()
    client.chat.return_value = {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "model": "claude-haiku-4-5",
    }
    client.model = "claude-haiku-4-5"
    return client


def test_needs_compaction_under_thresholds_returns_false(
    pg_session_factory,
    _settings_env,
):
    from call_manager.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        make_chat_turn(s, ws, role="user", content={"type": "text", "text": "x"})
        s.commit()
        assert needs_compaction(s, ws.id, settings=settings) is False


def test_needs_compaction_over_turn_threshold_returns_true(
    pg_session_factory,
    _settings_env,
    monkeypatch,
):
    monkeypatch.setenv("COMPACTION_TURN_THRESHOLD", "2")
    from call_manager.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        for i in range(3):
            make_chat_turn(s, ws, role="user", content={"type": "text", "text": f"t{i}"})
        s.commit()
        assert needs_compaction(s, ws.id, settings=settings) is True


def test_needs_compaction_over_token_threshold_returns_true(
    pg_session_factory,
    _settings_env,
    monkeypatch,
):
    monkeypatch.setenv("COMPACTION_TOKEN_THRESHOLD", "100")
    from call_manager.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        make_chat_turn(
            s,
            ws,
            role="assistant",
            content={"type": "text", "text": "x"},
            tokens_in=200,
        )
        s.commit()
        assert needs_compaction(s, ws.id, settings=settings) is True


def test_compact_workspace_writes_summary_row(
    pg_session_factory,
    _settings_env,
):
    """Happy path: un-summarized turns are rolled into a chat_summaries row;
    summarized_through points at the last turn included."""
    from call_manager.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        make_chat_turn(s, ws, role="user", content={"type": "text", "text": "hi"})
        t2 = make_chat_turn(s, ws, role="assistant", content={"type": "text", "text": "hello"})
        s.commit()
        wsid = ws.id
        last_turn_id = t2.id

    client = _stub_summarizer_client("User said hi; assistant replied hello.")

    compact_workspace(
        session_factory=pg_session_factory,
        workspace_id=wsid,
        summarizer_client=client,
        settings=settings,
    )

    with pg_session_factory() as s:
        summaries = list(s.scalars(select(ChatSummary).where(ChatSummary.workspace_id == wsid)))
    assert len(summaries) == 1
    assert "User said hi" in summaries[0].summary
    assert summaries[0].summarized_through == last_turn_id


def test_compact_workspace_resets_sdk_session_by_default(
    pg_session_factory,
    _settings_env,
):
    """compact_workspace clears workspaces.last_sdk_session_id in the same
    transaction that writes the summary, so the next turn starts a fresh SDK
    session (claude_adapter only passes resume=<id> when the column is
    non-NULL). Without this, the resumed session grows unbounded until the
    daily token cap wedges the workspace."""
    from call_manager.config import Settings
    from call_manager.db.models import Workspace

    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.last_sdk_session_id = "session-abc-123"
        make_chat_turn(s, ws, role="user", content={"type": "text", "text": "hi"})
        make_chat_turn(s, ws, role="assistant", content={"type": "text", "text": "hello"})
        s.commit()
        wsid = ws.id

    client = _stub_summarizer_client("A summary.")
    compact_workspace(
        session_factory=pg_session_factory,
        workspace_id=wsid,
        summarizer_client=client,
        settings=settings,
    )

    with pg_session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.id == wsid))
        assert ws.last_sdk_session_id is None
        # Summary still written in the same transaction.
        summaries = list(s.scalars(select(ChatSummary).where(ChatSummary.workspace_id == wsid)))
        assert len(summaries) == 1


def test_compact_workspace_keeps_sdk_session_when_reset_disabled(
    pg_session_factory,
    _settings_env,
):
    """reset_sdk_session=False preserves last_sdk_session_id (escape hatch
    for callers that want a summary without a cold-session reset)."""
    from call_manager.config import Settings
    from call_manager.db.models import Workspace

    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.last_sdk_session_id = "session-keep-me"
        make_chat_turn(s, ws, role="user", content={"type": "text", "text": "hi"})
        s.commit()
        wsid = ws.id

    client = _stub_summarizer_client("A summary.")
    compact_workspace(
        session_factory=pg_session_factory,
        workspace_id=wsid,
        summarizer_client=client,
        settings=settings,
        reset_sdk_session=False,
    )

    with pg_session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.id == wsid))
        assert ws.last_sdk_session_id == "session-keep-me"


def test_compact_workspace_only_summarizes_new_turns_after_prior_summary(
    pg_session_factory,
    _settings_env,
):
    """If a prior summary exists, only turns AFTER its summarized_through
    boundary are included in the next summary."""
    from call_manager.config import Settings

    settings = Settings()
    client = _stub_summarizer_client("Second summary.")
    with pg_session_factory() as s:
        ws = make_workspace(s)
        old_turn = make_chat_turn(s, ws, role="user", content={"type": "text", "text": "old"})
        s.flush()
        # Write a prior summary that covers old_turn.
        prior = ChatSummary(
            workspace_id=ws.id,
            summary="Old summary.",
            summarized_through=old_turn.id,
            tokens=10,
            model="claude-haiku-4-5",
        )
        s.add(prior)
        new_turn = make_chat_turn(s, ws, role="user", content={"type": "text", "text": "new"})
        s.commit()
        wsid = ws.id
        new_turn_id = new_turn.id

    compact_workspace(
        session_factory=pg_session_factory,
        workspace_id=wsid,
        summarizer_client=client,
        settings=settings,
    )

    # The call to client.chat() should have ONLY seen the new turn — verify by
    # inspecting the messages= arg.
    sent_messages = client.chat.call_args.kwargs.get("messages") or client.chat.call_args.args[0]
    sent_payload = str(sent_messages)
    assert "old" not in sent_payload
    assert "new" in sent_payload

    with pg_session_factory() as s:
        summaries = list(
            s.scalars(
                select(ChatSummary)
                .where(ChatSummary.workspace_id == wsid)
                .order_by(ChatSummary.created_at)
            )
        )
    assert len(summaries) == 2
    assert summaries[1].summarized_through == new_turn_id


def test_compact_workspace_noop_when_no_new_turns(
    pg_session_factory,
    _settings_env,
):
    """If summarized_through already points to the latest turn, no work."""
    from call_manager.config import Settings

    settings = Settings()
    client = _stub_summarizer_client("(should not be called)")
    with pg_session_factory() as s:
        ws = make_workspace(s)
        t = make_chat_turn(s, ws, role="user", content={"type": "text", "text": "x"})
        s.flush()
        prior = ChatSummary(
            workspace_id=ws.id,
            summary="...",
            summarized_through=t.id,
            tokens=1,
            model="claude-haiku-4-5",
        )
        s.add(prior)
        s.commit()
        wsid = ws.id

    compact_workspace(
        session_factory=pg_session_factory,
        workspace_id=wsid,
        summarizer_client=client,
        settings=settings,
    )
    client.chat.assert_not_called()


def test_run_compaction_tick_processes_only_workspaces_over_threshold(
    pg_session_factory,
    _settings_env,
    monkeypatch,
):
    monkeypatch.setenv("COMPACTION_TURN_THRESHOLD", "2")
    from call_manager.config import Settings

    settings = Settings()
    client = _stub_summarizer_client("Summary.")
    with pg_session_factory() as s:
        ws_over = make_workspace(s, owner=make_user(s, email="over@umu.se"))
        for i in range(3):
            make_chat_turn(s, ws_over, role="user", content={"type": "text", "text": f"x{i}"})
        ws_under = make_workspace(s, owner=make_user(s, email="under@umu.se"))
        make_chat_turn(s, ws_under, role="user", content={"type": "text", "text": "x"})
        s.commit()
        over_id = ws_over.id
        under_id = ws_under.id

    count = run_compaction_tick(
        session_factory=pg_session_factory,
        summarizer_client_factory=lambda: client,
        settings=settings,
    )
    assert count == 1

    with pg_session_factory() as s:
        over_summaries = list(
            s.scalars(select(ChatSummary).where(ChatSummary.workspace_id == over_id))
        )
        under_summaries = list(
            s.scalars(select(ChatSummary).where(ChatSummary.workspace_id == under_id))
        )
    assert len(over_summaries) == 1
    assert len(under_summaries) == 0
