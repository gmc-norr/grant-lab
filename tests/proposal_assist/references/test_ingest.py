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

from grant_lab.db.models import Reference, ReferenceChunk
from grant_lab.proposal_assist.references.embed import FakeEmbedder
from grant_lab.proposal_assist.references.ingest import (
    IngestResult,
    ingest_bytes,
)
from grant_lab.proposal_assist.references.storage import ReferenceStorage
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

    from grant_lab.db.models import Reference
    from grant_lab.proposal_assist.references.embed import FakeEmbedder
    from grant_lab.proposal_assist.references.ingest import ingest_bytes
    from grant_lab.proposal_assist.references.storage import ReferenceStorage
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
    import grant_lab.proposal_assist.references.ingest as ingest_module

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


# ---------------------------------------------------------------------------
# Phase 3 citekey collision / retry tests (concurrent-ingest hardening)
# ---------------------------------------------------------------------------


def test_phase3_sequential_same_author_year_assigns_distinct_citekeys(pg_session_factory, tmp_path):
    """Two sequential ingests in the same workspace with identical author/year
    must produce distinct citekeys: smith2021 and smith2021a.

    This exercises generate_citekey's suffix logic end-to-end through Phase 3
    and locks in that the second reference is NOT dropped or failed.
    """
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    md = b"# Paper\n\nBody.\n"
    same_csl = {
        "title": "T",
        "author": [{"family": "Smith"}],
        "issued": {"date-parts": [[2021]]},
    }

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid = ws.id

    # First ingest — should get smith2021.
    with pg_session_factory() as s:
        r1 = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=embedder,
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            filename="paper1.md",
            mime_type="text/markdown",
            data=md,
            uploaded_by=None,
            citation_service=_StubCitationService(
                _FakeCitationLLMResult(
                    data=same_csl,
                    usage=_FakeLLMUsage(),
                    model="m",
                    cap_exceeded=False,
                )
            ),
        )
        s.commit()

    # Second ingest — same workspace, same CSL → should get smith2021a.
    with pg_session_factory() as s:
        r2 = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=embedder,
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            filename="paper2.md",
            mime_type="text/markdown",
            data=md,
            uploaded_by=None,
            citation_service=_StubCitationService(
                _FakeCitationLLMResult(
                    data=same_csl,
                    usage=_FakeLLMUsage(),
                    model="m",
                    cap_exceeded=False,
                )
            ),
        )
        s.commit()

    with pg_session_factory() as s:
        ref1 = s.scalar(select(Reference).where(Reference.id == r1.reference_id))
        ref2 = s.scalar(select(Reference).where(Reference.id == r2.reference_id))

    assert ref1.citekey == "smith2021"
    assert ref2.citekey == "smith2021a"
    assert ref2.metadata_status == "extracted"  # NOT failed, NOT null


def test_phase3_retry_on_integrity_error_recovers_citekey(
    pg_session_factory, tmp_path, monkeypatch
):
    """Simulated commit race: force IntegrityError on the first citekey commit,
    verify the retry path recovers and produces a valid citekey (not 'failed').

    Strategy: pre-commit a Reference row with citekey='smith2021' so the DB
    uniqueness constraint fires; monkeypatch generate_citekey in the ingest
    module to return that taken key on the first call and a free key on the
    second, simulating the window between SELECT and COMMIT being won by a
    concurrent transaction.
    """
    import uuid as _uuid

    import grant_lab.proposal_assist.references.ingest as ingest_module

    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    md = b"# Paper\n\nBody.\n"

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid = ws.id

    # Pre-insert a Reference that already holds citekey='smith2021' so the DB
    # unique constraint fires when the retry's first attempt tries to commit it.
    import hashlib

    preexisting_id = _uuid.uuid4()
    with pg_session_factory() as s:
        from grant_lab.db.models import Reference as _Ref

        dummy_bytes = b"dummy"
        preexisting = _Ref(
            id=preexisting_id,
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            title="Pre-existing paper",
            filename="pre.md",
            storage_path=f"{wsid}/references/{preexisting_id}/original.md",
            mime_type="text/markdown",
            bytes_total=len(dummy_bytes),
            original_sha256=hashlib.sha256(dummy_bytes).hexdigest(),
            text_status="done",
            outline_status="skipped",
            uploaded_by=None,
            citekey="smith2021",
            metadata_status="extracted",
        )
        s.add(preexisting)
        s.commit()

    # Monkeypatch generate_citekey: first call returns the taken key, second
    # returns a free one — simulating what happens when a concurrent ingest
    # commits smith2021 between our SELECT and COMMIT.
    call_counts = {"n": 0}

    def patched_generate_citekey(csl, workspace_id, session):
        call_counts["n"] += 1
        if call_counts["n"] == 1:
            return "smith2021"  # already taken → IntegrityError on commit
        return "smith2021a"  # free on retry

    monkeypatch.setattr(ingest_module, "generate_citekey", patched_generate_citekey)

    stub_service = _StubCitationService(
        _FakeCitationLLMResult(
            data={
                "title": "T",
                "author": [{"family": "Smith"}],
                "issued": {"date-parts": [[2021]]},
            },
            usage=_FakeLLMUsage(),
            model="m",
            cap_exceeded=False,
        )
    )

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

    # The retry must have recovered: valid citekey, not failed.
    assert ref.citekey == "smith2021a"
    assert ref.metadata_status == "extracted"
    # Confirm the patch was actually triggered (retry happened).
    assert call_counts["n"] >= 2


def test_phase3_retry_exhaustion_marks_metadata_failed(pg_session_factory, tmp_path, monkeypatch):
    """When every citekey attempt collides (all 4 attempts raise IntegrityError),
    Phase 3 must be non-fatal: ingest returns a successful IngestResult and the
    reference row ends with metadata_status='failed'.

    Strategy: pre-insert a Reference with citekey='exhausted2020' in the same
    workspace; monkeypatch generate_citekey to ALWAYS return that same key so
    every attempt hits the DB uniqueness constraint. The outer Phase-3 except
    catches the re-raised IntegrityError and marks the row 'failed'.
    """
    import hashlib as _hashlib
    import uuid as _uuid

    import grant_lab.proposal_assist.references.ingest as ingest_module

    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    md = b"# Paper\n\nBody.\n"

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid = ws.id

    # Pre-insert a Reference that permanently holds citekey='exhausted2020' so
    # every attempt by the new ingest will collide.
    fixed_citekey = "exhausted2020"
    blocker_id = _uuid.uuid4()
    dummy = b"blocker"
    with pg_session_factory() as s:
        from grant_lab.db.models import Reference as _Ref

        blocker = _Ref(
            id=blocker_id,
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            title="Blocker",
            filename="blocker.md",
            storage_path=f"{wsid}/references/{blocker_id}/original.md",
            mime_type="text/markdown",
            bytes_total=len(dummy),
            original_sha256=_hashlib.sha256(dummy).hexdigest(),
            text_status="done",
            outline_status="skipped",
            uploaded_by=None,
            citekey=fixed_citekey,
            metadata_status="extracted",
        )
        s.add(blocker)
        s.commit()

    # Monkeypatch: always return the colliding citekey — no escape after N retries.
    monkeypatch.setattr(
        ingest_module,
        "generate_citekey",
        lambda csl, workspace_id, session: fixed_citekey,
    )

    stub_service = _StubCitationService(
        _FakeCitationLLMResult(
            data={
                "title": "Exhausted Retry Paper",
                "author": [{"family": "Exhausted"}],
                "issued": {"date-parts": [[2020]]},
            },
            usage=_FakeLLMUsage(),
            model="m",
            cap_exceeded=False,
        )
    )

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
            filename="exhausted.md",
            mime_type="text/markdown",
            data=md,
            uploaded_by=None,
            citation_service=stub_service,
        )
        s.commit()

    # Phase 3 exhaustion is non-fatal — IngestResult must be returned.
    assert isinstance(result, IngestResult)
    assert result.text_status == "done"

    # The reference must exist and have metadata_status='failed' (retry exhausted).
    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == result.reference_id))

    assert ref is not None
    assert ref.metadata_status == "failed"


class _EmptyCitationService:
    """Returns empty data dict — simulates LLM returning no extractable fields."""

    def complete_json(self, *, workspace_id, op, system, user):
        return _FakeCitationLLMResult(
            data={},
            usage=None,
            model=None,
            cap_exceeded=False,
        )


def test_phase3_empty_csl_assigns_fallback_citekey(pg_session_factory, tmp_path):
    """When the citation_service returns an empty dict (no extractable CSL),
    metadata_status must be 'failed' AND a fallback citekey of the form
    'ref-<12hex>' must still be assigned so the reference is citeable downstream.
    """
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    # Plain-text upload with no PDF metadata — heuristic produces nothing,
    # LLM returns empty dict.
    txt = b"Some plain text with no bibliographic information.\n"

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
            filename="plain.txt",
            mime_type="text/plain",
            data=txt,
            uploaded_by=None,
            citation_service=_EmptyCitationService(),
        )
        s.commit()

    assert isinstance(result, IngestResult)
    assert result.text_status == "done"

    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == result.reference_id))

    assert ref.metadata_status == "failed"
    assert ref.citekey is not None
    assert ref.citekey.startswith("ref-")


class _Res:
    def __init__(self, data):
        self.data = data
        self.cap_exceeded = False
        self.cap_scope = None


class _Svc:
    def __init__(self, data):
        self._data = data

    def complete_json(self, *, workspace_id, op, system, user):
        return _Res(dict(self._data))


def test_auto_classify_overrides_provisional_kind(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    storage = ReferenceStorage(root=tmp_path)
    with pg_session_factory() as s:
        res = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=FakeEmbedder(),
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            filename="d.md",
            mime_type="text/markdown",
            data=b"# Heading\n\nbody text here",
            uploaded_by=None,
            citation_service=_Svc({"title": "Dataset", "_kind": "data"}),
            auto_classify_kind=True,
        )
    with pg_session_factory() as s:
        assert s.get(Reference, res.reference_id).kind == "data"


def test_explicit_kind_not_overwritten(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    storage = ReferenceStorage(root=tmp_path)
    with pg_session_factory() as s:
        res = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=FakeEmbedder(),
            workspace_id=wsid,
            kind="prior_proposal",
            source_type="uploaded",
            source_uri=None,
            filename="d.md",
            mime_type="text/markdown",
            data=b"# H\n\nbody",
            uploaded_by=None,
            citation_service=_Svc({"title": "T", "_kind": "data"}),
            auto_classify_kind=False,
        )
    with pg_session_factory() as s:
        assert s.get(Reference, res.reference_id).kind == "prior_proposal"
