import uuid
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_monitor.db.models import ChatTurn
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
