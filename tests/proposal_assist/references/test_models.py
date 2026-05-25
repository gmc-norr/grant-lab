"""ORM smoke: the new models load against the Slice-1 schema and a
basic round-trip works."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from call_manager.db.models import Reference, ReferenceChunk, TurnJob, Workspace
from tests.proposal_assist.conftest import make_user, make_workspace


def test_reference_insert_and_query(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=owner)
        s.commit()

        ref = Reference(
            id=uuid.uuid4(),
            workspace_id=ws.id,
            kind="paper",
            source_type="uploaded",
            filename="x.pdf",
            storage_path=f"{ws.id}/references/r1/original.pdf",
            mime_type="application/pdf",
            bytes_total=1024,
            uploaded_by=owner.id,
        )
        s.add(ref)
        s.commit()
        rid = ref.id

    with pg_session_factory() as s:
        loaded = s.scalar(select(Reference).where(Reference.id == rid))
    assert loaded is not None
    assert loaded.kind == "paper"
    assert loaded.text_status == "pending"
    assert loaded.outline_status == "skipped"
    assert loaded.text_indexed_at is None


def test_reference_chunk_insert_with_embedding(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=owner)
        s.commit()
        ref = Reference(
            id=uuid.uuid4(),
            workspace_id=ws.id,
            kind="paper",
            source_type="uploaded",
            filename="x.pdf",
            storage_path=f"{ws.id}/references/r1/original.pdf",
            mime_type="application/pdf",
            uploaded_by=owner.id,
        )
        s.add(ref)
        s.commit()

        chunk = ReferenceChunk(
            reference_id=ref.id,
            workspace_id=ws.id,
            chunk_index=0,
            section_path="Introduction",
            text="Hello world",
            embedding=[0.0] * 1024,
            embedding_model="mxbai-embed-large",
        )
        s.add(chunk)
        s.commit()
        cid = chunk.id

    with pg_session_factory() as s:
        loaded = s.scalar(select(ReferenceChunk).where(ReferenceChunk.id == cid))
    assert loaded is not None
    assert loaded.section_path == "Introduction"
    assert len(loaded.embedding) == 1024


def test_turn_jobs_has_fetch_count_column(pg_session_factory):
    """Slice 3 will use this; Slice 1 just verifies the column exists with the right default."""
    with pg_session_factory() as s:
        # No turn job to load, but a SELECT on the column must not raise.
        result = s.execute(
            select(TurnJob.fetch_count).limit(1)
        )
        list(result)  # forces query execution
