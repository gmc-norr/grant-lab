"""Freshness helpers — idempotent template seeding driven by source_text_hash
+ extractor_version. Phase 0 calls these to guarantee both templates are
up-to-date before snapshotting them into the workspace.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from call_monitor.db.models import Call, CallRubric, CallSectionExtract
from call_monitor.proposal_assist.extractors.rubric import extract_rubric
from call_monitor.proposal_assist.extractors.sections import (
    SECTION_NAMES,
    extract_sections,
)
from call_monitor.proposal_assist.hashing import source_text_hash


def ensure_section_extracts_fresh(
    *,
    session: Session,
    call: Call,
    client,
    extractor_version: str,
) -> None:
    """Idempotent: if all six rows exist with matching `source_text_hash` AND
    matching `extractor_version`, no LLM call is made. Otherwise the extractor
    is run **first** (so a transient LLM failure cannot wipe cached rows),
    then prior rows are deleted and six fresh rows inserted in the same unit
    of work.

    Caller commits. If the extractor raises, the caller's session should
    rollback — no destructive state has been written yet.
    """
    current_hash = source_text_hash(call.full_text or "")

    existing = session.scalars(
        select(CallSectionExtract).where(CallSectionExtract.call_id == call.id)
    ).all()

    if (
        len(existing) == len(SECTION_NAMES)
        and all(r.source_text_hash == current_hash for r in existing)
        and all(r.extractor_version == extractor_version for r in existing)
    ):
        return

    # EXTRACT FIRST — if this raises, we haven't touched the DB yet.
    sections = extract_sections(call_text=call.full_text or "", client=client)

    # Now safe to mutate. Delete + insert happen in the same transaction;
    # neither lands without the other because the caller commits once.
    if existing:
        session.execute(delete(CallSectionExtract).where(CallSectionExtract.call_id == call.id))
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
                extracted_by_model=client.model,
                extractor_version=extractor_version,
                source_text_hash=current_hash,
            )
        )


def ensure_rubric_fresh(
    *,
    session: Session,
    call: Call,
    client,
    extractor_version: str,
) -> None:
    """Idempotent: if the row exists with matching hash + version, no LLM
    call is made. Otherwise the extractor is run **first** (so a transient
    LLM failure cannot mutate the cached row), then the row is upserted.

    Caller commits. If the extractor raises, the caller's session should
    rollback — no destructive state has been written yet.
    """
    current_hash = source_text_hash(call.full_text or "")
    existing = session.get(CallRubric, call.id)
    if (
        existing is not None
        and existing.source_text_hash == current_hash
        and existing.extractor_version == extractor_version
    ):
        return

    # EXTRACT FIRST — if this raises, the existing row is untouched.
    payload = extract_rubric(call_text=call.full_text or "", client=client)
    now = datetime.now(UTC)

    if existing is None:
        session.add(
            CallRubric(
                call_id=call.id,
                criteria=payload["criteria"],
                funder_notes=payload["funder_notes"],
                extracted_at=now,
                extracted_by_model=client.model,
                extractor_version=extractor_version,
                source_text_hash=current_hash,
            )
        )
    else:
        existing.criteria = payload["criteria"]
        existing.funder_notes = payload["funder_notes"]
        existing.extracted_at = now
        existing.extracted_by_model = client.model
        existing.extractor_version = extractor_version
        existing.source_text_hash = current_hash
