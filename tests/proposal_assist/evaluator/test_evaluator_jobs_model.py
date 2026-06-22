import pytest
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import EvaluatorJob
from tests.proposal_assist.conftest import make_workspace


def test_active_unique_rejects_second_active_job(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(EvaluatorJob(workspace_id=ws.id, status="queued"))
        s.commit()
        s.add(EvaluatorJob(workspace_id=ws.id, status="running"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_completed_does_not_block_a_new_active(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(EvaluatorJob(workspace_id=ws.id, status="completed", scorecard={"criteria": []}))
        s.commit()
        s.add(EvaluatorJob(workspace_id=ws.id, status="queued"))
        s.commit()  # no IntegrityError — completed is outside the partial index
