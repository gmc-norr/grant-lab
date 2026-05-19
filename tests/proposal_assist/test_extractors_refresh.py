import json
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from call_monitor.db.models import CallRubric, CallSectionExtract
from call_monitor.proposal_assist.extractors.refresh import (
    ensure_rubric_fresh,
    ensure_section_extracts_fresh,
)
from call_monitor.proposal_assist.extractors.sections import SECTION_NAMES
from tests.proposal_assist.conftest import make_call


def _section_envelope() -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps({s: f"extract for {s}" for s in SECTION_NAMES}),
                }
            }
        ],
        "model": "claude-opus-4-7",
    }


def _rubric_envelope() -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "criteria": [
                                {"name": "X", "description": "x", "weight": 1.0, "scale_max": 5}
                            ],
                            "funder_notes": "",
                        }
                    ),
                }
            }
        ],
        "model": "claude-opus-4-7",
    }


def test_ensure_section_extracts_fresh_creates_six_rows_first_time(
    pg_session_factory,
):
    client = Mock()
    client.chat.return_value = _section_envelope()
    client.model = "claude-opus-4-7"

    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ensure_section_extracts_fresh(
            session=s,
            call=call,
            client=client,
            extractor_version="sections-v1",
        )
        s.commit()
        rows = s.scalars(
            select(CallSectionExtract).where(CallSectionExtract.call_id == call.id)
        ).all()
        assert len(rows) == 6
        assert {r.section for r in rows} == set(SECTION_NAMES)
        assert client.chat.call_count == 1


def test_ensure_section_extracts_fresh_skips_when_hash_and_version_match(
    pg_session_factory,
):
    client = Mock()
    client.chat.return_value = _section_envelope()
    client.model = "claude-opus-4-7"

    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ensure_section_extracts_fresh(
            session=s,
            call=call,
            client=client,
            extractor_version="sections-v1",
        )
        s.commit()
        assert client.chat.call_count == 1

        ensure_section_extracts_fresh(
            session=s,
            call=call,
            client=client,
            extractor_version="sections-v1",
        )
        s.commit()
        assert client.chat.call_count == 1  # NOT re-called


def test_ensure_section_extracts_fresh_re_extracts_on_version_bump(
    pg_session_factory,
):
    client = Mock()
    client.chat.return_value = _section_envelope()
    client.model = "claude-opus-4-7"

    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ensure_section_extracts_fresh(
            session=s,
            call=call,
            client=client,
            extractor_version="sections-v1",
        )
        s.commit()
        ensure_section_extracts_fresh(
            session=s,
            call=call,
            client=client,
            extractor_version="sections-v2",  # bumped
        )
        s.commit()
        assert client.chat.call_count == 2
        rows = s.scalars(
            select(CallSectionExtract).where(CallSectionExtract.call_id == call.id)
        ).all()
        assert all(r.extractor_version == "sections-v2" for r in rows)


def test_ensure_section_extracts_fresh_re_extracts_on_source_text_change(
    pg_session_factory,
):
    client = Mock()
    client.chat.return_value = _section_envelope()
    client.model = "claude-opus-4-7"

    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ensure_section_extracts_fresh(
            session=s,
            call=call,
            client=client,
            extractor_version="sections-v1",
        )
        s.commit()

        call.full_text = (call.full_text or "") + " (RE-INGESTED)"
        s.commit()

        ensure_section_extracts_fresh(
            session=s,
            call=call,
            client=client,
            extractor_version="sections-v1",
        )
        s.commit()
        assert client.chat.call_count == 2


def test_ensure_section_extracts_fresh_preserves_existing_on_extractor_failure(
    pg_session_factory,
):
    """Regression: a transient LLM failure must NOT wipe the previously-cached
    rows. Extract-first ordering means the LLM call is attempted before any
    DELETE; on failure, existing rows are untouched."""
    good_client = Mock()
    good_client.chat.return_value = _section_envelope()
    good_client.model = "claude-opus-4-7"

    failing_client = Mock()
    failing_client.chat.side_effect = RuntimeError("LLM 503")
    failing_client.model = "claude-opus-4-7"

    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()

        # Seed with good extracts.
        ensure_section_extracts_fresh(
            session=s,
            call=call,
            client=good_client,
            extractor_version="sections-v1",
        )
        s.commit()
        before = sorted(
            (r.section, r.extract_text)
            for r in s.scalars(
                select(CallSectionExtract).where(CallSectionExtract.call_id == call.id)
            ).all()
        )

        # Bump version to force re-extract; the client raises.
        with pytest.raises(RuntimeError):
            ensure_section_extracts_fresh(
                session=s,
                call=call,
                client=failing_client,
                extractor_version="sections-v2",
            )

        # Existing rows survive.
        after = sorted(
            (r.section, r.extract_text)
            for r in s.scalars(
                select(CallSectionExtract).where(CallSectionExtract.call_id == call.id)
            ).all()
        )
        assert before == after


def test_ensure_rubric_fresh_creates_row_first_time(pg_session_factory):
    client = Mock()
    client.chat.return_value = _rubric_envelope()
    client.model = "claude-opus-4-7"

    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ensure_rubric_fresh(
            session=s,
            call=call,
            client=client,
            extractor_version="rubric-v1",
        )
        s.commit()
        row = s.get(CallRubric, call.id)
        assert row is not None
        assert row.extractor_version == "rubric-v1"
        assert client.chat.call_count == 1


def test_ensure_rubric_fresh_skips_when_fresh(pg_session_factory):
    client = Mock()
    client.chat.return_value = _rubric_envelope()
    client.model = "claude-opus-4-7"

    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ensure_rubric_fresh(
            session=s,
            call=call,
            client=client,
            extractor_version="rubric-v1",
        )
        s.commit()
        ensure_rubric_fresh(
            session=s,
            call=call,
            client=client,
            extractor_version="rubric-v1",
        )
        s.commit()
        assert client.chat.call_count == 1


def test_ensure_rubric_fresh_preserves_existing_on_extractor_failure(
    pg_session_factory,
):
    good_client = Mock()
    good_client.chat.return_value = _rubric_envelope()
    good_client.model = "claude-opus-4-7"

    failing_client = Mock()
    failing_client.chat.side_effect = RuntimeError("LLM 503")
    failing_client.model = "claude-opus-4-7"

    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()

        ensure_rubric_fresh(
            session=s,
            call=call,
            client=good_client,
            extractor_version="rubric-v1",
        )
        s.commit()
        before_criteria = s.get(CallRubric, call.id).criteria

        with pytest.raises(RuntimeError):
            ensure_rubric_fresh(
                session=s,
                call=call,
                client=failing_client,
                extractor_version="rubric-v2",
            )
        s.rollback()  # discard any unflushed changes from the failed call
        s.expire_all()  # force the next read to hit Postgres, not the identity map
        # Row content unchanged.
        assert s.get(CallRubric, call.id).criteria == before_criteria


def test_ensure_rubric_fresh_re_extracts_on_version_bump(pg_session_factory):
    client = Mock()
    client.chat.return_value = _rubric_envelope()
    client.model = "claude-opus-4-7"

    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ensure_rubric_fresh(
            session=s,
            call=call,
            client=client,
            extractor_version="rubric-v1",
        )
        s.commit()
        assert client.chat.call_count == 1

        ensure_rubric_fresh(
            session=s,
            call=call,
            client=client,
            extractor_version="rubric-v2",  # bumped
        )
        s.commit()
        assert client.chat.call_count == 2
        row = s.get(CallRubric, call.id)
        assert row.extractor_version == "rubric-v2"


def test_ensure_rubric_fresh_re_extracts_on_source_text_change(pg_session_factory):
    client = Mock()
    client.chat.return_value = _rubric_envelope()
    client.model = "claude-opus-4-7"

    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ensure_rubric_fresh(
            session=s,
            call=call,
            client=client,
            extractor_version="rubric-v1",
        )
        s.commit()

        call.full_text = (call.full_text or "") + " (RE-INGESTED)"
        s.commit()

        ensure_rubric_fresh(
            session=s,
            call=call,
            client=client,
            extractor_version="rubric-v1",
        )
        s.commit()
        assert client.chat.call_count == 2
