import uuid

from sqlalchemy import select

from call_monitor.db.models import ChatSummary, ChatTurn
from tests.proposal_assist.conftest import make_workspace


def test_chat_summary_insert(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        last_turn = ChatTurn(
            workspace_id=ws.id,
            role="assistant",
            content={"text": "summary boundary turn"},
        )
        s.add(last_turn)
        s.flush()
        s.add(
            ChatSummary(
                workspace_id=ws.id,
                summary="100 turns of brainstorming about cancer biomarkers.",
                summarized_through=last_turn.id,
                tokens=4321,
                model="claude-haiku-4-5",
            )
        )
        s.commit()
        cs = s.scalar(select(ChatSummary).where(ChatSummary.workspace_id == ws.id))
        assert isinstance(cs.id, uuid.UUID)
        assert cs.summary.startswith("100 turns")
        assert cs.summarized_through == last_turn.id
        assert cs.tokens == 4321
        assert cs.model == "claude-haiku-4-5"
