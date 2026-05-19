from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_monitor.db.models import CallSectionExtract
from tests.proposal_assist.conftest import make_call


def test_call_section_extract_can_be_inserted(pg_session_factory):
    with pg_session_factory() as s:
        call = make_call(s)
        s.add(
            CallSectionExtract(
                call_id=call.id,
                section="eligibility",
                extract_text="Open to Swedish universities.",
                extracted_by_model="claude-opus-4-7",
                extractor_version="sections-v1",
                source_text_hash="0" * 64,
            )
        )
        s.commit()

        fetched = s.scalar(
            select(CallSectionExtract).where(
                CallSectionExtract.call_id == call.id,
                CallSectionExtract.section == "eligibility",
            )
        )
        assert fetched is not None
        assert fetched.extract_text == "Open to Swedish universities."
        assert fetched.extractor_version == "sections-v1"
        assert isinstance(fetched.extracted_at, datetime)


def test_call_section_extract_section_check_constraint(pg_session_factory):
    with pg_session_factory() as s:
        call = make_call(s)
        s.add(
            CallSectionExtract(
                call_id=call.id,
                section="not_a_real_section",  # CHECK violation
                extract_text="x",
                extracted_by_model="m",
                extractor_version="sections-v1",
                source_text_hash="0" * 64,
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()
