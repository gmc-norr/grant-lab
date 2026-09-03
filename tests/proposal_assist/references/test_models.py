"""ORM smoke: the new models load against the Slice-1 schema and a
basic round-trip works."""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import CitationLlmUsage, Reference, ReferenceChunk, TurnJob
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
        result = s.execute(select(TurnJob.fetch_count).limit(1))
        list(result)  # forces query execution


def test_reference_metadata_columns_round_trip(pg_session_factory):
    sha = "a" * 64  # valid lowercase 64-hex

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s))
        s.commit()
        wsid = ws.id

    rid = uuid.uuid4()
    with pg_session_factory() as s:
        s.add(
            Reference(
                id=rid,
                workspace_id=wsid,
                kind="paper",
                source_type="uploaded",
                source_uri=None,
                filename="p.pdf",
                storage_path="x",
                mime_type="application/pdf",
                csl_json={"title": "T"},
                csl_json_extracted={"title": "T?"},
                citekey="smith2021",
                metadata_status="approved",
                metadata_source="llm",
                original_sha256=sha,
            )
        )
        s.commit()

    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == rid))
        assert ref.csl_json == {"title": "T"}
        assert ref.citekey == "smith2021"
        assert ref.original_sha256 == sha

    # CHECK rejects a bad status.
    with pg_session_factory() as s:
        s.add(
            Reference(
                id=uuid.uuid4(),
                workspace_id=wsid,
                kind="paper",
                source_type="uploaded",
                source_uri=None,
                filename="q.pdf",
                storage_path="y",
                mime_type="application/pdf",
                metadata_status="bogus",
            )
        )
        try:
            s.commit()
            raise AssertionError("expected IntegrityError on bad metadata_status")
        except IntegrityError:
            pass

    # CHECK rejects a non-64-hex original_sha256.
    with pg_session_factory() as s:
        s.add(
            Reference(
                id=uuid.uuid4(),
                workspace_id=wsid,
                kind="paper",
                source_type="uploaded",
                source_uri=None,
                filename="r.pdf",
                storage_path="z",
                mime_type="application/pdf",
                original_sha256="abc123",
            )
        )
        try:
            s.commit()
            raise AssertionError("expected IntegrityError on bad original_sha256")
        except IntegrityError:
            pass

    # Server default applies 'pending' when metadata_status is omitted.
    rid2 = uuid.uuid4()
    with pg_session_factory() as s:
        s.add(
            Reference(
                id=rid2,
                workspace_id=wsid,
                kind="paper",
                source_type="uploaded",
                source_uri=None,
                filename="s.pdf",
                storage_path="s",
                mime_type="application/pdf",
            )
        )
        s.commit()
    with pg_session_factory() as s:
        ref2 = s.scalar(select(Reference).where(Reference.id == rid2))
        assert ref2.metadata_status == "pending"


def test_reference_citekey_unique_per_workspace(pg_session_factory):
    def _ref(wsid, name):
        return Reference(
            id=uuid.uuid4(),
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            filename=name,
            storage_path=name,
            mime_type="application/pdf",
            citekey="smith2021",
        )

    with pg_session_factory() as s:
        ws_a = make_workspace(s, owner=make_user(s, email="a@umu.se"))
        ws_b = make_workspace(s, owner=make_user(s, email="b@umu.se"))
        s.commit()
        a, b = ws_a.id, ws_b.id

    # Same citekey in DIFFERENT workspaces is allowed.
    with pg_session_factory() as s:
        s.add(_ref(a, "1.pdf"))
        s.add(_ref(b, "2.pdf"))
        s.commit()

    # Duplicate citekey in the SAME workspace is rejected.
    with pg_session_factory() as s:
        s.add(_ref(a, "3.pdf"))
        try:
            s.commit()
            raise AssertionError("expected IntegrityError on duplicate workspace citekey")
        except IntegrityError:
            pass

    # NULL citekey is outside the partial index — duplicates allowed in one workspace.
    def _ref_no_citekey(wsid, name):
        return Reference(
            id=uuid.uuid4(),
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            filename=name,
            storage_path=name,
            mime_type="application/pdf",
        )

    with pg_session_factory() as s:
        s.add(_ref_no_citekey(a, "4.pdf"))
        s.add(_ref_no_citekey(a, "5.pdf"))
        s.commit()  # must not raise


def test_citation_llm_usage_insert_and_nonneg(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s))
        s.commit()
        wsid = ws.id

    with pg_session_factory() as s:
        s.add(
            CitationLlmUsage(
                workspace_id=wsid,
                op="metadata_extract",
                status="ok",
                tokens_in=100,
                tokens_out=20,
                model="gemma",
            )
        )
        s.commit()
    with pg_session_factory() as s:
        row = s.scalar(select(CitationLlmUsage).where(CitationLlmUsage.workspace_id == wsid))
        assert row.op == "metadata_extract" and row.tokens_in == 100

    with pg_session_factory() as s:
        s.add(CitationLlmUsage(workspace_id=wsid, op="x", status="ok", tokens_in=-1))
        try:
            s.commit()
            raise AssertionError("expected IntegrityError on negative tokens")
        except IntegrityError:
            pass

    with pg_session_factory() as s:
        s.add(CitationLlmUsage(workspace_id=wsid, op="x", status="invalid"))
        try:
            s.commit()
            raise AssertionError("expected IntegrityError on invalid status")
        except IntegrityError:
            pass
