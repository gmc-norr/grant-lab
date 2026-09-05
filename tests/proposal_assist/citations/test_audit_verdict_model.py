"""DB round-trip + constraint checks for the CitationAuditVerdict model."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from grant_lab.db.models import CitationAuditVerdict
from tests.proposal_assist.conftest import make_workspace


def test_audit_verdict_insert_round_trip(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(
            CitationAuditVerdict(
                workspace_id=ws.id,
                cache_key="sha256:deadbeef",
                verdict="supports",
                rationale="The quoted span directly states the claim.",
                validator_version="v1",
            )
        )
        s.commit()
        row = s.scalar(
            select(CitationAuditVerdict).where(
                CitationAuditVerdict.workspace_id == ws.id,
                CitationAuditVerdict.cache_key == "sha256:deadbeef",
            )
        )
        assert row is not None
        assert isinstance(row.id, int)
        assert row.verdict == "supports"
        assert row.validator_version == "v1"
        assert row.rationale == "The quoted span directly states the claim."
        assert row.created_at is not None


def test_audit_verdict_rationale_nullable(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(
            CitationAuditVerdict(
                workspace_id=ws.id,
                cache_key="sha256:nullrat",
                verdict="irrelevant",
                validator_version="v1",
            )
        )
        s.commit()
        row = s.scalar(
            select(CitationAuditVerdict).where(CitationAuditVerdict.cache_key == "sha256:nullrat")
        )
        assert row.rationale is None


def test_audit_verdict_rejects_invalid_verdict(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(
            CitationAuditVerdict(
                workspace_id=ws.id,
                cache_key="sha256:bogus",
                verdict="bogus",
                validator_version="v1",
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()
