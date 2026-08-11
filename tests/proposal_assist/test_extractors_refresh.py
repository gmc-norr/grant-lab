import json
from dataclasses import dataclass

import pytest
from sqlalchemy import select

from call_manager.db.models import CallRubric, CallSectionExtract
from call_manager.proposal_assist.extractors.refresh import (
    compute_rubric,
    compute_section_extracts,
    persist_rubric,
    persist_section_extracts,
    rubric_is_fresh,
    section_extracts_are_fresh,
)
from call_manager.proposal_assist.extractors.sections import SECTION_NAMES
from call_manager.proposal_assist.hashing import source_text_hash
from tests.proposal_assist.conftest import make_call


def _make_call(session, *, text: str):
    """Thin wrapper over conftest's make_call, binding its `full_text` kwarg
    to the `text` name the split tests use throughout this file."""
    return make_call(session, full_text=text)


@dataclass
class _FakeClient:
    """One canned envelope whose JSON body satisfies both the section
    extractor's schema (all SECTION_NAMES keys) and the rubric extractor's
    schema (`criteria` + `funder_notes`), so the same fake serves both
    compute_section_extracts and compute_rubric."""

    model: str

    def chat(self, *, messages, response_format=None):
        content = json.dumps(
            {
                **{name: f"extract for {name}" for name in SECTION_NAMES},
                "criteria": [
                    {"name": "X", "description": "x", "weight": 1.0, "scale_max": 5}
                ],
                "funder_notes": "",
            }
        )
        return {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "model": self.model,
        }


@dataclass
class _RaisingClient:
    model: str

    def chat(self, *, messages, response_format=None):
        raise RuntimeError("extractor exploded")


def test_section_extracts_are_not_fresh_first_time(pg_session_factory):
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        assert section_extracts_are_fresh(
            session=s, call=call, extractor_version="v1"
        ) is False


def test_persist_writes_one_row_per_section(pg_session_factory):
    client = _FakeClient(model="m")
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        sections = compute_section_extracts(call_text=call.full_text, client=client)
        persist_section_extracts(
            session=s,
            call=call,
            sections=sections,
            model=client.model,
            extractor_version="v1",
            source_hash=source_text_hash(call.full_text),
        )
        s.commit()
        rows = s.scalars(
            select(CallSectionExtract).where(CallSectionExtract.call_id == call.id)
        ).all()
        assert len(rows) == len(SECTION_NAMES)
        assert section_extracts_are_fresh(
            session=s, call=call, extractor_version="v1"
        ) is True


def test_fresh_is_false_after_a_version_bump(pg_session_factory):
    client = _FakeClient(model="m")
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        persist_section_extracts(
            session=s,
            call=call,
            sections=compute_section_extracts(call_text=call.full_text, client=client),
            model=client.model,
            extractor_version="v1",
            source_hash=source_text_hash(call.full_text),
        )
        s.commit()
        assert section_extracts_are_fresh(
            session=s, call=call, extractor_version="v2"
        ) is False


def test_fresh_is_false_after_the_source_text_changes(pg_session_factory):
    client = _FakeClient(model="m")
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        persist_section_extracts(
            session=s,
            call=call,
            sections=compute_section_extracts(call_text=call.full_text, client=client),
            model=client.model,
            extractor_version="v1",
            source_hash=source_text_hash(call.full_text),
        )
        s.commit()
        call.full_text = "different body"
        s.commit()
        assert section_extracts_are_fresh(
            session=s, call=call, extractor_version="v1"
        ) is False


def test_a_failing_extractor_cannot_touch_existing_rows(pg_session_factory):
    """Previously an ordering convention inside one function ('EXTRACT FIRST').
    Now structural: compute raises, so persist is never reached."""
    good = _FakeClient(model="m")
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        persist_section_extracts(
            session=s,
            call=call,
            sections=compute_section_extracts(call_text=call.full_text, client=good),
            model=good.model,
            extractor_version="v1",
            source_hash=source_text_hash(call.full_text),
        )
        s.commit()
        before = {
            r.section: r.extract_text
            for r in s.scalars(
                select(CallSectionExtract).where(CallSectionExtract.call_id == call.id)
            )
        }

        boom = _RaisingClient(model="m")
        with pytest.raises(RuntimeError):
            compute_section_extracts(call_text=call.full_text, client=boom)

        after = {
            r.section: r.extract_text
            for r in s.scalars(
                select(CallSectionExtract).where(CallSectionExtract.call_id == call.id)
            )
        }
        assert after == before


def test_rubric_is_not_fresh_first_time(pg_session_factory):
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        assert rubric_is_fresh(session=s, call=call, extractor_version="v1") is False


def test_persist_rubric_writes_then_reads_fresh(pg_session_factory):
    client = _FakeClient(model="m")
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        persist_rubric(
            session=s,
            call=call,
            payload=compute_rubric(call_text=call.full_text, client=client),
            model=client.model,
            extractor_version="v1",
            source_hash=source_text_hash(call.full_text),
        )
        s.commit()
        assert s.get(CallRubric, call.id) is not None
        assert rubric_is_fresh(session=s, call=call, extractor_version="v1") is True


def test_rubric_is_not_fresh_after_a_version_bump(pg_session_factory):
    client = _FakeClient(model="m")
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        persist_rubric(
            session=s,
            call=call,
            payload=compute_rubric(call_text=call.full_text, client=client),
            model=client.model,
            extractor_version="v1",
            source_hash=source_text_hash(call.full_text),
        )
        s.commit()
        assert rubric_is_fresh(session=s, call=call, extractor_version="v2") is False


def test_rubric_is_not_fresh_after_the_source_text_changes(pg_session_factory):
    client = _FakeClient(model="m")
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        persist_rubric(
            session=s,
            call=call,
            payload=compute_rubric(call_text=call.full_text, client=client),
            model=client.model,
            extractor_version="v1",
            source_hash=source_text_hash(call.full_text),
        )
        s.commit()
        call.full_text = "different body"
        s.commit()
        assert rubric_is_fresh(session=s, call=call, extractor_version="v1") is False


def test_a_failing_rubric_extractor_cannot_touch_the_existing_row(pg_session_factory):
    good = _FakeClient(model="m")
    with pg_session_factory() as s:
        call = _make_call(s, text="body")
        s.commit()
        persist_rubric(
            session=s,
            call=call,
            payload=compute_rubric(call_text=call.full_text, client=good),
            model=good.model,
            extractor_version="v1",
            source_hash=source_text_hash(call.full_text),
        )
        s.commit()
        before = (
            s.get(CallRubric, call.id).criteria,
            s.get(CallRubric, call.id).extracted_by_model,
        )

        with pytest.raises(RuntimeError):
            compute_rubric(call_text=call.full_text, client=_RaisingClient(model="m"))

        row = s.get(CallRubric, call.id)
        assert (row.criteria, row.extracted_by_model) == before
