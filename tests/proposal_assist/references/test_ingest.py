"""Two-phase ingest:
Phase 1 — write bytes atomically + insert Reference row + commit.
Phase 2 — sync Stage 1 (extract + chunk + embed) in a separate tx.
          On failure, text_status='failed', row + bytes retained.
Phase 3 — metadata extraction + citekey assignment in a separate tx.
          Non-fatal: Phase 1+2 result always returned even if Phase 3 fails.
"""

from dataclasses import dataclass

import pytest
from sqlalchemy import select

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.references.embed import FakeEmbedder
from call_manager.proposal_assist.references.ingest import (
    IngestResult,
    ingest_bytes,
)
from call_manager.proposal_assist.references.storage import ReferenceStorage
from tests.proposal_assist.conftest import make_user, make_workspace

# ---------------------------------------------------------------------------
# Stubs for Phase 3 tests
# ---------------------------------------------------------------------------


@dataclass
class _FakeLLMUsage:
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class _FakeCitationLLMResult:
    data: dict | None
    usage: object | None
    model: str | None
    cap_exceeded: bool = False
    cap_scope: str | None = None


class _StubCitationService:
    """Returns a fixed CitationLLMResult for any complete_json call."""

    def __init__(self, result: _FakeCitationLLMResult):
        self._result = result

    def complete_json(self, *, workspace_id, op, system, user):
        return self._result


class _RaisingCitationService:
    """Always raises on complete_json (to exercise LLM-error degradation path)."""

    def complete_json(self, *, workspace_id, op, system, user):
        raise RuntimeError("simulated LLM failure")


def test_ingest_text_markdown_creates_row_and_chunks(pg_session_factory, tmp_path):
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    md = b"# Title\n\nIntro.\n\n## Methods\n\nMethod text.\n"

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid, uid = ws.id, user.id

    with pg_session_factory() as s:
        result = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=embedder,
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            filename="paper.md",
            mime_type="text/markdown",
            data=md,
            uploaded_by=uid,
        )
        s.commit()

    assert isinstance(result, IngestResult)
    assert result.text_status == "done"

    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == result.reference_id))
        chunks = list(
            s.scalars(
                select(ReferenceChunk).where(ReferenceChunk.reference_id == result.reference_id)
            )
        )

    assert ref.text_status == "done"
    assert ref.text_indexed_at is not None
    assert ref.text_chars_total > 0
    assert len(chunks) >= 2  # at least Title and Methods sections
    section_paths = [c.section_path for c in chunks]
    assert any(p and "Methods" in p for p in section_paths)
    # File on disk
    p = tmp_path / str(wsid) / "references" / str(ref.id) / "original.md"
    assert p.read_bytes() == md
    text_p = tmp_path / str(wsid) / "references" / str(ref.id) / "text.txt"
    assert text_p.exists()


def test_ingest_extraction_failure_persists_row_with_failed_status(pg_session_factory, tmp_path):
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid, uid = ws.id, user.id

    # Send "PDF" bytes that aren't actually a PDF — extraction will fail.
    with pg_session_factory() as s:
        result = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=embedder,
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            filename="bad.pdf",
            mime_type="application/pdf",
            data=b"not a real pdf",
            uploaded_by=uid,
        )
        s.commit()

    assert result.text_status == "failed"

    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == result.reference_id))
        chunks = list(
            s.scalars(
                select(ReferenceChunk).where(ReferenceChunk.reference_id == result.reference_id)
            )
        )

    # Row + file PERSIST (retain-on-failure model — Codex High #1 rev4 resolution).
    assert ref is not None
    assert ref.text_status == "failed"
    assert ref.text_error is not None
    assert "pdf" in ref.text_error.lower()
    p = tmp_path / str(wsid) / "references" / str(ref.id) / "original.pdf"
    assert p.exists()
    # No chunks inserted.
    assert chunks == []


def test_ingest_data_csv_uses_skipped_status(pg_session_factory, tmp_path):
    """kind='data' rows are still text-indexed in v1 (CSV/XLSX produce
    schema+sample text), so text_status goes to 'done' but outline
    stays 'skipped' per spec."""
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    csv = b"name,age\nalice,30\nbob,25\n"

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid, uid = ws.id, user.id

    with pg_session_factory() as s:
        result = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=embedder,
            workspace_id=wsid,
            kind="data",
            source_type="uploaded",
            source_uri=None,
            filename="d.csv",
            mime_type="text/csv",
            data=csv,
            uploaded_by=uid,
        )
        s.commit()

    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == result.reference_id))
    # For data kind we still text-index (so summarize_data has something);
    # outline stays skipped (was already skipped at insert; v1.1 may change).
    assert ref.text_status == "done"
    assert ref.outline_status == "skipped"


def test_phase1_commit_failure_cleans_bytes(pg_session_factory, tmp_path, monkeypatch):
    """Blocker regression (post-Slice-2 review): if Phase 1 commit raises
    after storage.write_atomic, the bytes must be unlinked. Otherwise we
    leave orphan files on disk with no DB row pointing at them."""
    from sqlalchemy.orm import Session

    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    md = b"# Title\n\nbody."

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid, uid = ws.id, user.id

    # Patch Session.commit to raise on its FIRST call within ingest_bytes
    # (the Phase 1 commit). Subsequent commits (Phase 2 internal) use
    # fresh sessions and aren't patched.
    original_commit = Session.commit
    calls = {"count": 0}

    def boom_then_real(self):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated Phase 1 commit failure")
        return original_commit(self)

    monkeypatch.setattr(Session, "commit", boom_then_real)

    with (
        pg_session_factory() as s,
        pytest.raises(RuntimeError, match="simulated Phase 1 commit failure"),
    ):
        ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=embedder,
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            filename="t.md",
            mime_type="text/markdown",
            data=md,
            uploaded_by=uid,
        )

    # The bytes that were briefly on disk must be cleaned up.
    workspace_refs_dir = tmp_path / str(wsid) / "references"
    leftover = list(workspace_refs_dir.iterdir()) if workspace_refs_dir.exists() else []
    assert leftover == [], f"Phase 1 commit-failure must unlink bytes; found leftover: {leftover}"


def test_ingest_records_original_sha256(pg_session_factory, tmp_path):
    import hashlib

    from sqlalchemy import select

    from call_manager.db.models import Reference
    from call_manager.proposal_assist.references.embed import FakeEmbedder
    from call_manager.proposal_assist.references.ingest import ingest_bytes
    from call_manager.proposal_assist.references.storage import ReferenceStorage
    from tests.proposal_assist.conftest import make_user, make_workspace

    storage = ReferenceStorage(root=tmp_path)
    data = b"# Title\n\nBody.\n"

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s))
        s.commit()
        wsid = ws.id

    with pg_session_factory() as s:
        result = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=FakeEmbedder(),
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            filename="p.md",
            mime_type="text/markdown",
            data=data,
            uploaded_by=None,
        )
        s.commit()

    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == result.reference_id))
    assert ref.original_sha256 == hashlib.sha256(data).hexdigest()
    assert ref.metadata_status == "pending"


# ---------------------------------------------------------------------------
# Phase 3 tests (Slice 1B Task 4)
# ---------------------------------------------------------------------------


def test_phase3_upload_with_citation_service(pg_session_factory, tmp_path):
    """Upload + fake citation_service → csl_json_extracted, title, citekey, status all set."""
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    md = b"# Some Paper\n\nAbstract text.\n"

    stub_service = _StubCitationService(
        _FakeCitationLLMResult(
            data={
                "title": "Extracted Title",
                "author": [{"family": "Smith"}],
                "issued": {"date-parts": [[2021]]},
            },
            usage=_FakeLLMUsage(),
            model="m",
            cap_exceeded=False,
        )
    )

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid = ws.id

    with pg_session_factory() as s:
        result = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=embedder,
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            filename="paper.md",
            mime_type="text/markdown",
            data=md,
            uploaded_by=None,
            citation_service=stub_service,
        )
        s.commit()

    assert isinstance(result, IngestResult)
    assert result.text_status == "done"

    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == result.reference_id))

    assert ref.csl_json_extracted == {
        "title": "Extracted Title",
        "author": [{"family": "Smith"}],
        "issued": {"date-parts": [[2021]]},
    }
    assert ref.title == "Extracted Title"
    assert ref.citekey == "smith2021"
    assert ref.metadata_status == "extracted"
    assert ref.metadata_source == "llm"


def test_phase3_fetched_source_meta(pg_session_factory, tmp_path):
    """Fetched source_meta (crossref) → verified_external, no LLM needed."""
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    md = b"# X\n\nAbstract.\n"

    source_meta = {
        "message": {
            "title": ["X"],
            "author": [{"family": "Roe"}],
            "issued": {"date-parts": [[2020]]},
            "DOI": "10/x",
        }
    }

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid = ws.id

    with pg_session_factory() as s:
        result = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=embedder,
            workspace_id=wsid,
            kind="paper",
            source_type="crossref",
            source_uri=None,
            filename="paper.md",
            mime_type="text/markdown",
            data=md,
            uploaded_by=None,
            source_meta=source_meta,
            citation_service=None,
        )
        s.commit()

    assert isinstance(result, IngestResult)

    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == result.reference_id))

    assert ref.metadata_status == "verified_external"
    assert ref.metadata_source == "fetcher"
    assert ref.title == "X"
    assert ref.citekey == "roe2020"


def test_phase3_no_service_no_source_meta_stays_pending(pg_session_factory, tmp_path):
    """Neither citation_service nor source_meta → metadata_status stays 'pending'.

    Extraction is skipped entirely; prior behaviour is preserved.
    """
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    md = b"# Title\n\nBody.\n"

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid = ws.id

    with pg_session_factory() as s:
        result = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=embedder,
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            filename="p.md",
            mime_type="text/markdown",
            data=md,
            uploaded_by=None,
            # source_meta and citation_service intentionally omitted
        )
        s.commit()

    assert isinstance(result, IngestResult)
    assert result.text_status == "done"

    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == result.reference_id))

    assert ref.metadata_status == "pending"
    assert ref.citekey is None


def test_phase3_failure_is_nonfatal(pg_session_factory, tmp_path, monkeypatch):
    """Phase 3 exception (monkeypatched generate_citekey) must not break ingest.

    The ref row must still exist and metadata_status must be 'failed'.
    IngestResult is returned normally (text_status='done').
    """
    import call_manager.proposal_assist.references.ingest as ingest_module

    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    md = b"# Title\n\nBody.\n"

    # citation_service returns a valid CSL dict so metadata.py won't fail by
    # itself; the failure is injected in generate_citekey (Phase 3 outer try).
    stub_service = _StubCitationService(
        _FakeCitationLLMResult(
            data={
                "title": "Some Title",
                "author": [{"family": "Jones"}],
                "issued": {"date-parts": [[2022]]},
            },
            usage=_FakeLLMUsage(),
            model="m",
            cap_exceeded=False,
        )
    )

    def boom_citekey(*args, **kwargs):
        raise RuntimeError("injected citekey failure")

    monkeypatch.setattr(ingest_module, "generate_citekey", boom_citekey)

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid = ws.id

    with pg_session_factory() as s:
        result = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=embedder,
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            filename="paper.md",
            mime_type="text/markdown",
            data=md,
            uploaded_by=None,
            citation_service=stub_service,
        )
        s.commit()

    # IngestResult must come back — Phase 3 failure is non-fatal.
    assert isinstance(result, IngestResult)
    assert result.text_status == "done"

    # Row must exist with metadata_status='failed'.
    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == result.reference_id))

    assert ref is not None
    assert ref.metadata_status == "failed"
