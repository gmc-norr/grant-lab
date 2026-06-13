import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import ChatTurn
from call_manager.proposal_assist.turns.outcomes import outcome_message
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


_FINISH_MSG = "The drafting assistant couldn't finish this turn. Please try again."


@pytest.mark.parametrize(
    "outcome, raw_error, expected",
    [
        ("cancelled", None, "This turn was cancelled before it started."),
        ("cancelled", "workspace soft-deleted", "This turn was cancelled before it started."),
        (
            "failed",
            "reaped: stuck in running > 600s (workspace wedged)",
            "This turn was stopped after running too long.",
        ),
        ("failed", "orphaned", "This turn was interrupted. Please try again."),
        ("failed", "sdk_turn_error: boom", _FINISH_MSG),
        ("failed", "processor_failure: ValueError: secret/path/leak", _FINISH_MSG),
    ],
)
def test_outcome_message_maps_safely(outcome, raw_error, expected):
    msg = outcome_message(outcome, raw_error)
    assert msg == expected
    if raw_error:
        # raw diagnostics must never leak into the viewer-visible string
        assert raw_error not in msg
