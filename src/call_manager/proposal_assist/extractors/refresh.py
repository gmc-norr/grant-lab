"""Freshness helpers — idempotent template seeding driven by source_text_hash
+ extractor_version. Phase 0 checks freshness, computes outside any
transaction, then persists under a lock.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from call_manager.db.models import Call, CallRubric, CallSectionExtract
from call_manager.proposal_assist.extractors.rubric import extract_rubric
from call_manager.proposal_assist.extractors.sections import (
    SECTION_NAMES,
    extract_sections,
)
from call_manager.proposal_assist.hashing import source_text_hash


class LlmClient(Protocol):
    """Minimum interface required by the freshness helpers.

    Matches both call_manager.ranking.anthropic_client.AnthropicClient
    and the local Ollama client. The `model` attribute is stamped onto
    the persisted row's `extracted_by_model` column.
    """

    model: str

    def chat(
        self,
        *,
        messages: list[dict],
        response_format: dict | None = None,
    ) -> dict: ...


def section_extracts_are_fresh(
    *, session: Session, call: Call, extractor_version: str
) -> bool:
    """True when every section row matches the call's current text hash AND the
    given extractor version — i.e. no LLM call is needed."""
    current_hash = source_text_hash(call.full_text or "")
    existing = session.scalars(
        select(CallSectionExtract).where(CallSectionExtract.call_id == call.id)
    ).all()
    return (
        len(existing) == len(SECTION_NAMES)
        and all(r.source_text_hash == current_hash for r in existing)
        and all(r.extractor_version == extractor_version for r in existing)
    )


def compute_section_extracts(*, call_text: str, client: LlmClient) -> dict[str, str]:
    """Pure: one LLM round-trip, no session, no writes. Raising here can never
    damage cached rows because nothing has been touched."""
    return extract_sections(call_text=call_text, client=client)


def persist_section_extracts(
    *,
    session: Session,
    call: Call,
    sections: dict[str, str],
    model: str,
    extractor_version: str,
    source_hash: str,
) -> None:
    """Replace the call's section rows. No LLM. Caller commits."""
    existing = session.scalars(
        select(CallSectionExtract).where(CallSectionExtract.call_id == call.id)
    ).all()
    if existing:
        session.execute(
            delete(CallSectionExtract).where(CallSectionExtract.call_id == call.id)
        )
        # Flush so the DELETE lands before our INSERTs hit the PK constraint.
        session.flush()

    now = datetime.now(UTC)
    for section_name in SECTION_NAMES:
        session.add(
            CallSectionExtract(
                call_id=call.id,
                section=section_name,
                extract_text=sections[section_name],
                extracted_at=now,
                extracted_by_model=model,
                extractor_version=extractor_version,
                source_text_hash=source_hash,
            )
        )


def rubric_is_fresh(*, session: Session, call: Call, extractor_version: str) -> bool:
    current_hash = source_text_hash(call.full_text or "")
    existing = session.get(CallRubric, call.id)
    return (
        existing is not None
        and existing.source_text_hash == current_hash
        and existing.extractor_version == extractor_version
    )


def compute_rubric(*, call_text: str, client: LlmClient) -> dict:
    """Pure: one LLM round-trip, no session, no writes."""
    return extract_rubric(call_text=call_text, client=client)


def persist_rubric(
    *,
    session: Session,
    call: Call,
    payload: dict,
    model: str,
    extractor_version: str,
    source_hash: str,
) -> None:
    """Upsert the call's rubric row. No LLM. Caller commits."""
    now = datetime.now(UTC)
    existing = session.get(CallRubric, call.id)
    if existing is None:
        session.add(
            CallRubric(
                call_id=call.id,
                criteria=payload["criteria"],
                funder_notes=payload["funder_notes"],
                extracted_at=now,
                extracted_by_model=model,
                extractor_version=extractor_version,
                source_text_hash=source_hash,
            )
        )
    else:
        existing.criteria = payload["criteria"]
        existing.funder_notes = payload["funder_notes"]
        existing.extracted_at = now
        existing.extracted_by_model = model
        existing.extractor_version = extractor_version
        existing.source_text_hash = source_hash
