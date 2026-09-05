import pytest
from sqlalchemy.exc import IntegrityError

from grant_lab.db.models import CoherenceJob
from tests.proposal_assist.conftest import make_workspace


def _job(ws_id, status="queued"):
    return CoherenceJob(workspace_id=ws_id, status=status)


def test_active_unique_rejects_second_active_row(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(_job(ws.id, "queued"))
        s.flush()
        s.add(_job(ws.id, "running"))
        with pytest.raises(IntegrityError):
            s.flush()  # uq_coherence_jobs_active covers queued OR running


def test_enqueue_allowed_after_prior_completed(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        done = _job(ws.id, "completed")
        s.add(done)
        s.flush()
        # A new active row is fine once the prior left the active set.
        s.add(_job(ws.id, "queued"))
        s.flush()  # no IntegrityError
        assert s.query(CoherenceJob).filter_by(workspace_id=ws.id).count() == 2
