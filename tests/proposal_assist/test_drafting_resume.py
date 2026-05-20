from call_monitor.proposal_assist.drafting.resume import (
    build_resume_messages,
)
from tests.proposal_assist.conftest import (
    make_chat_summary,
    make_chat_turn,
    make_workspace,
)


def test_resume_with_no_history_returns_empty_list(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        assert build_resume_messages(s, ws.id) == []


def test_resume_with_only_chat_turns_returns_all_in_position_order(
    pg_session_factory,
):
    """No summary → include every chat_turn, oldest-first by position."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        make_chat_turn(
            s,
            ws,
            role="user",
            content={"type": "text", "text": "hi"},
        )
        make_chat_turn(
            s,
            ws,
            role="assistant",
            content={"type": "text", "text": "hello"},
        )
        s.commit()

        messages = build_resume_messages(s, ws.id)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == {"type": "text", "text": "hi"}
        assert messages[1]["role"] == "assistant"


def test_resume_with_summary_prepends_summary_skips_summarized_turns(
    pg_session_factory,
):
    """Resume = [synthetic summary message] + [turns AFTER summarized_through].

    The summary itself becomes a synthetic user-role message that the SDK
    accepts as the conversation prefix."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        make_chat_turn(
            s,
            ws,
            role="user",
            content={"type": "text", "text": "old user"},
        )
        t2 = make_chat_turn(
            s,
            ws,
            role="assistant",
            content={"type": "text", "text": "old assistant"},
        )
        make_chat_summary(s, ws, t2, summary_text="prev conversation: ...")
        make_chat_turn(
            s,
            ws,
            role="user",
            content={"type": "text", "text": "new user"},
        )
        s.commit()

        messages = build_resume_messages(s, ws.id)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert "prev conversation" in messages[0]["content"][0]["text"]
        assert "[Summary of prior conversation]" in (messages[0]["content"][0]["text"])
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == {"type": "text", "text": "new user"}


def test_resume_uses_latest_summary_when_multiple_exist(pg_session_factory):
    """If a workspace has had compaction run twice, only the most recent
    summary is included; older summaries are subsumed."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        t1 = make_chat_turn(
            s,
            ws,
            role="user",
            content={"type": "text", "text": "v1 user"},
        )
        make_chat_summary(s, ws, t1, summary_text="first summary")
        t2 = make_chat_turn(
            s,
            ws,
            role="user",
            content={"type": "text", "text": "v2 user"},
        )
        make_chat_summary(s, ws, t2, summary_text="second summary")
        make_chat_turn(
            s,
            ws,
            role="user",
            content={"type": "text", "text": "newest"},
        )
        s.commit()

        messages = build_resume_messages(s, ws.id)
        assert len(messages) == 2
        text = messages[0]["content"][0]["text"]
        assert "second summary" in text
        assert "first summary" not in text


def test_resume_skips_summary_boundary_correctly_under_position_ordering(
    pg_session_factory,
):
    """Boundary semantics: 'summarized_through' is INCLUSIVE — the turn
    referenced is summarized, not retained. We rely on chat_turns.position
    (a global Postgres SEQUENCE, strictly monotonic, tie-free even within
    one transaction) — no time.sleep() needed."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        make_chat_turn(
            s,
            ws,
            role="user",
            content={"type": "text", "text": "old"},
        )
        t_boundary = make_chat_turn(
            s,
            ws,
            role="user",
            content={"type": "text", "text": "boundary"},
        )
        make_chat_turn(
            s,
            ws,
            role="user",
            content={"type": "text", "text": "new"},
        )
        make_chat_summary(s, ws, t_boundary)
        s.commit()

        messages = build_resume_messages(s, ws.id)
        # 1 summary + 1 surviving turn (the "new" turn).
        assert len(messages) == 2
        assert messages[1]["content"] == {"type": "text", "text": "new"}
