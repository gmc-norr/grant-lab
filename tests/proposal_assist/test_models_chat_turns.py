import uuid
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import ChatTurn
from tests.proposal_assist.conftest import make_workspace


def test_chat_turn_insert_user_role(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(
            ChatTurn(
                workspace_id=ws.id,
                role="user",
                content={"type": "text", "text": "Help me draft this."},
            )
        )
        s.commit()
        turn = s.scalar(select(ChatTurn).where(ChatTurn.workspace_id == ws.id))
        assert isinstance(turn.id, uuid.UUID)
        assert turn.role == "user"
        assert turn.content["text"] == "Help me draft this."
        assert turn.tokens_in is None
        assert turn.tokens_out is None
        assert turn.model is None
        assert isinstance(turn.created_at, datetime)


def test_chat_turn_insert_assistant_with_tokens(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(
            ChatTurn(
                workspace_id=ws.id,
                role="assistant",
                content={"type": "text", "text": "Here's a draft."},
                tokens_in=1234,
                tokens_out=567,
                model="claude-opus-4-7",
            )
        )
        s.commit()
        turn = s.scalar(select(ChatTurn).where(ChatTurn.workspace_id == ws.id))
        assert turn.role == "assistant"
        assert turn.tokens_in == 1234
        assert turn.tokens_out == 567
        assert turn.model == "claude-opus-4-7"


def test_chat_turn_role_check_constraint(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(
            ChatTurn(
                workspace_id=ws.id,
                role="not_a_role",
                content={},
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


def test_chat_turn_position_is_monotonic_within_transaction(pg_session_factory):
    """Multiple turns committed in one transaction get strictly
    monotonic position values — no ties. This is the property
    resume-from-history relies on for sound ordering."""
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s)
        # Three turns in ONE transaction:
        t1 = ChatTurn(
            workspace_id=ws.id,
            role="user",
            content={"type": "text", "text": "a"},
        )
        t2 = ChatTurn(
            workspace_id=ws.id,
            role="assistant",
            content={"type": "text", "text": "b"},
        )
        t3 = ChatTurn(
            workspace_id=ws.id,
            role="tool",
            content={"type": "text", "text": "c"},
        )
        s.add_all([t1, t2, t3])
        s.commit()
        s.refresh(t1)
        s.refresh(t2)
        s.refresh(t3)
        # Strictly monotonic — no ties.
        assert t1.position < t2.position < t3.position


def test_chat_turn_position_is_global_not_per_workspace(pg_session_factory):
    """The sequence is global; cross-workspace ordering is not
    meaningful but per-workspace ordering is (which is all the
    resume serializer needs)."""
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        from tests.proposal_assist.conftest import make_user

        ws_a = make_workspace(s, title="A", owner=make_user(s, email="owner-a@ki.se"))
        ws_b = make_workspace(s, title="B", owner=make_user(s, email="owner-b@ki.se"))
        t_a = ChatTurn(
            workspace_id=ws_a.id,
            role="user",
            content={"type": "text", "text": "a"},
        )
        t_b = ChatTurn(
            workspace_id=ws_b.id,
            role="user",
            content={"type": "text", "text": "b"},
        )
        s.add_all([t_a, t_b])
        s.commit()
        s.refresh(t_a)
        s.refresh(t_b)
        # Different positions (one is greater); we don't care which.
        assert t_a.position != t_b.position
