import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import ChatTurn
from tests.proposal_assist.conftest import make_workspace


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
