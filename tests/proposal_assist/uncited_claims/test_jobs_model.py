import pytest
from sqlalchemy.exc import IntegrityError

from grant_lab.db.models import UncitedClaimsJob
from tests.proposal_assist.conftest import make_workspace


def test_row_roundtrips_with_claims_jsonb(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = UncitedClaimsJob(
            workspace_id=ws.id,
            status="completed",
            claims=[{"claim_text": "x", "section_index": 0, "reason": "r", "claim_hash": "h"}],
            evaluated_draft_sha="abc",
        )
        s.add(job)
        s.commit()
        got = s.get(UncitedClaimsJob, job.id)
        assert got.claims[0]["claim_hash"] == "h"
        assert got.input_tokens == 0 and got.output_tokens == 0


def test_active_unique_blocks_two_active_in_one_workspace(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(UncitedClaimsJob(workspace_id=ws.id, status="queued"))
        s.flush()
        s.add(UncitedClaimsJob(workspace_id=ws.id, status="running"))
        with pytest.raises(IntegrityError):
            s.flush()
