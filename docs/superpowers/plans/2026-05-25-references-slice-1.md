# References Subsystem — Slice 1 (Foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Users can upload a reference file via the web UI; bytes land on disk, text is extracted, chunks are embedded into pgvector via the existing local Ollama stack, and the row is searchable via SQL. No agent integration, no fetch, no delete, no chat composer — those are later slices.

**Architecture:** Mirrors B7 attachments tightly: per-workspace filesystem layout, atomic write-then-row-then-commit, owner-only POST, owner+viewer GET. The new piece is a two-phase ingest (Phase 1 = B7-style atomic upload; Phase 2 = synchronous Stage 1 text-extract + chunk + embed in a separate transaction; failures retained as `text_status='failed'` for retry in a later slice). Section-aware chunking where the format supports it (Markdown headings, docx heading styles, ipynb markdown cells); PDFs labeled per-page; RTF/CSV/XLSX get one section.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 + Alembic, Postgres 16 + pgvector, Ollama `mxbai-embed-large` (already in stack), pdfminer.six (new — MIT, pure Python; no docker dep), python-docx (new), striprtf (new), openpyxl (new).

---

## File Structure

**New code:**
- `alembic/versions/0031_references.py` — migration for `references` + `reference_chunks` + `ALTER turn_jobs ADD fetch_count`
- `src/call_manager/proposal_assist/references/__init__.py` — empty
- `src/call_manager/proposal_assist/references/storage.py` — `ReferenceStorage` class (mirrors `AttachmentStorage` from B7)
- `src/call_manager/proposal_assist/references/extract.py` — `extract_text(mime, data, filename)` returns `(text, structured)` where `structured` is a list of `(section_path, text)` tuples or `None` if the format doesn't carry structure
- `src/call_manager/proposal_assist/references/chunker.py` — `chunk_text(extracted)` returns a list of `(section_path, text)` ready for insertion
- `src/call_manager/proposal_assist/references/embed.py` — thin wrapper around `OllamaEmbedClient` + a `FakeEmbedder` for tests
- `src/call_manager/proposal_assist/references/ingest.py` — `two_phase_ingest(...)` entry point with explicit Phase 1 + Phase 2
- `src/call_manager/proposal_assist/web/references.py` — router with three routes (GET panel / POST upload / GET raw)
- `src/call_manager/web/templates/proposal_assist/workspace_references.html` — list + upload page
- `src/call_manager/web/templates/proposal_assist/partials/reference_row.html` — per-row partial
- `tests/proposal_assist/references/__init__.py` — empty
- `tests/proposal_assist/references/test_storage.py`
- `tests/proposal_assist/references/test_extract.py`
- `tests/proposal_assist/references/test_chunker.py`
- `tests/proposal_assist/references/test_ingest.py`
- `tests/proposal_assist/test_web_workspaces_references.py`

**Modified:**
- `src/call_manager/db/models.py` — `Reference` + `ReferenceChunk` ORM models, plus `TurnJob.fetch_count`
- `src/call_manager/main.py` — include the new router
- `src/call_manager/web/templates/proposal_assist/workspace_main.html` — add "References" link to side nav
- `pyproject.toml` — add four new deps

**Out of scope for Slice 1** (lands in Slices 2–5): MCP tools, fetch dispatcher, delete/copy routes, chat composer integration, admin panel, reembed CLI. The migration already adds `outline_status` etc. as v1.1 scaffolding per the spec, but no worker writes those values in Slice 1.

---

## Task 1: Add dependencies

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock` (regenerated)

- [ ] **Step 1: Add the four new deps**

In `pyproject.toml`, find the `dependencies` list (where `Pillow` already lives from B7) and add:

```toml
    "pdfminer.six>=20231228",
    "python-docx>=1.1.0",
    "striprtf>=0.0.26",
    "openpyxl>=3.1.0",
```

- [ ] **Step 2: Resolve + install**

Run: `uv sync`

Expected: `Resolved N packages` (no version conflicts).

- [ ] **Step 3: Sanity-import each new dep**

Run:

```bash
uv run python -c "from pdfminer.high_level import extract_text; from docx import Document; from striprtf.striprtf import rtf_to_text; from openpyxl import load_workbook; print('ok')"
```

Expected: `ok` printed.

- [ ] **Step 4: Run the existing test suite to confirm no regression**

Run: `uv run pytest -q`

Expected: same green count as master at branch base (952 passed, 1 skipped at HEAD `30c26b6`).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "deps(references): add pdfminer.six + python-docx + striprtf + openpyxl"
```

---

## Task 2: Alembic migration

**Files:**
- Create: `alembic/versions/0031_references.py`

- [ ] **Step 1: Generate a migration skeleton**

Run:

```bash
set -a; source /home/parlar_ai/dev/call-monitor/.env; set +a
uv run alembic revision -m "references_and_reference_chunks"
```

Note the generated filename (e.g., `alembic/versions/<hash>_references_and_reference_chunks.py`). Rename it to `alembic/versions/0031_references.py` to match the project's sequential naming convention (last existing is `0030_b7_attachment_dimensions.py`).

- [ ] **Step 2: Write the migration content**

Replace the generated file's body with:

```python
"""references and reference_chunks

Revision ID: 0031_references
Revises: 0030_b7_attachment_dimensions
Create Date: 2026-05-25 ...

"""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


revision = "0031_references"
down_revision = "0030_b7_attachment_dimensions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "references",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("bytes_total", sa.Integer(), nullable=True),
        # Stage 1 — text extract + chunk + embed status
        sa.Column("text_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("text_error", sa.Text(), nullable=True),
        sa.Column("text_indexed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("text_chars_total", sa.Integer(), nullable=True),
        # v1.1 scaffolding — PageIndex outline (always 'skipped' in v1)
        sa.Column("outline_status", sa.Text(), nullable=False, server_default="skipped"),
        sa.Column("outline_error", sa.Text(), nullable=True),
        sa.Column("outline_indexed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "uploaded_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('paper','prior_proposal','analysis','data')",
            name="ck_references_kind",
        ),
        sa.CheckConstraint(
            "source_type IN ('uploaded','crossref','pubmed','arxiv','web')",
            name="ck_references_source_type",
        ),
        sa.CheckConstraint(
            "text_status IN ('pending','running','done','failed','skipped')",
            name="ck_references_text_status",
        ),
        sa.CheckConstraint(
            "outline_status IN ('pending','running','done','failed','skipped')",
            name="ck_references_outline_status",
        ),
    )
    op.create_index("ix_references_ws", "references", ["workspace_id"])
    op.create_index(
        "ix_references_indexing_backlog",
        "references",
        ["workspace_id", "created_at"],
        postgresql_where=sa.text(
            "text_status IN ('pending','running') "
            "OR outline_status IN ('pending','running')"
        ),
    )

    op.create_table(
        "reference_chunks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "reference_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("references.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("section_path", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "reference_id", "chunk_index", name="uq_reference_chunks_ref_idx"
        ),
    )
    # HNSW index for semantic search; pgvector already loaded by earlier migration.
    op.execute(
        "CREATE INDEX ix_reference_chunks_ws_embedding "
        "ON reference_chunks USING hnsw (embedding vector_cosine_ops)"
    )

    op.add_column(
        "turn_jobs",
        sa.Column(
            "fetch_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("turn_jobs", "fetch_count")
    op.execute("DROP INDEX IF EXISTS ix_reference_chunks_ws_embedding")
    op.drop_table("reference_chunks")
    op.drop_index("ix_references_indexing_backlog", table_name="references")
    op.drop_index("ix_references_ws", table_name="references")
    op.drop_table("references")
```

- [ ] **Step 3: Apply the migration**

Run:

```bash
set -a; source /home/parlar_ai/dev/call-monitor/.env; set +a
uv run alembic upgrade head
```

Expected: `INFO [alembic.runtime.migration] Running upgrade 0030_b7_attachment_dimensions -> 0031_references, references_and_reference_chunks`.

- [ ] **Step 4: Verify schema by introspection**

Run:

```bash
docker exec call-monitor-db-1 psql -U call_manager -d call_manager -c "\d references" | head -25
docker exec call-monitor-db-1 psql -U call_manager -d call_manager -c "\d reference_chunks" | head -15
docker exec call-monitor-db-1 psql -U call_manager -d call_manager -c "\d turn_jobs" | grep fetch_count
```

Expected: both tables print column definitions; `fetch_count | integer | not null default 0` on `turn_jobs`.

- [ ] **Step 5: Test the downgrade + re-upgrade roundtrip**

Run:

```bash
uv run alembic downgrade -1
uv run alembic upgrade head
```

Expected: both succeed with no errors.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/0031_references.py
git commit -m "migration(references): add references + reference_chunks + turn_jobs.fetch_count

Two new tables per the references-corpus spec (2026-05-25, rev4) plus
the fetch_count column on turn_jobs for the mid-turn fetch cap that
lands in Slice 3.

v1.1 PageIndex scaffolding (outline_status/error/indexed_at) is in
the table now with default 'skipped' so v1.1 needs no schema migration.
"
```

---

## Task 3: ORM models

**Files:**
- Modify: `src/call_manager/db/models.py` — add `Reference`, `ReferenceChunk`, and `TurnJob.fetch_count`

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/references/__init__.py`:

```python
```

(Empty file.)

Create `tests/proposal_assist/references/test_models.py`:

```python
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
```

- [ ] **Step 2: Run test to confirm it fails**

Run:

```bash
uv run pytest tests/proposal_assist/references/test_models.py -v
```

Expected: `ImportError` — `Reference` / `ReferenceChunk` don't exist yet.

- [ ] **Step 3: Add ORM models in `src/call_manager/db/models.py`**

Find the end of the `Attachment` model definition (the one from B7). After it, before the next non-related declaration, add:

```python
class Reference(Base):
    """A per-workspace reference — uploaded paper, prior proposal, analysis,
    or data file. See docs/superpowers/specs/2026-05-25-references-corpus-design.md."""

    __tablename__ = "references"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('paper','prior_proposal','analysis','data')",
            name="ck_references_kind",
        ),
        CheckConstraint(
            "source_type IN ('uploaded','crossref','pubmed','arxiv','web')",
            name="ck_references_source_type",
        ),
        CheckConstraint(
            "text_status IN ('pending','running','done','failed','skipped')",
            name="ck_references_text_status",
        ),
        CheckConstraint(
            "outline_status IN ('pending','running','done','failed','skipped')",
            name="ck_references_outline_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    bytes_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending"
    )
    text_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_indexed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    text_chars_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outline_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="skipped"
    )
    outline_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    outline_indexed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    uploaded_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReferenceChunk(Base):
    """A chunk of a Reference, with its embedding for semantic search."""

    __tablename__ = "reference_chunks"
    __table_args__ = (
        UniqueConstraint(
            "reference_id", "chunk_index", name="uq_reference_chunks_ref_idx"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    reference_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("references.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    section_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1024), nullable=False)
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
```

Add the `fetch_count` column to the existing `TurnJob` model (find the class definition; add this attribute with the other column declarations):

```python
    fetch_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
```

If the imports at the top of `models.py` are missing any of `Vector`, `UniqueConstraint`, `CheckConstraint`, `BigInteger`, `Text`, `Integer`, `DateTime`, `ForeignKey`, `func`, `Mapped`, `mapped_column`, `PgUUID`, add the missing names. (Most are likely already imported by B7's `Attachment` model.)

- [ ] **Step 4: Run test to confirm it passes**

Run: `uv run pytest tests/proposal_assist/references/test_models.py -v`

Expected: 3 passed.

- [ ] **Step 5: Confirm the full suite is still green**

Run: `uv run pytest -q`

Expected: 955 passed, 1 skipped (952 baseline + 3 new tests).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/db/models.py tests/proposal_assist/references/__init__.py tests/proposal_assist/references/test_models.py
git commit -m "feat(references): Reference + ReferenceChunk ORM models + TurnJob.fetch_count"
```

---

## Task 4: Storage helpers

**Files:**
- Create: `src/call_manager/proposal_assist/references/__init__.py`
- Create: `src/call_manager/proposal_assist/references/storage.py`
- Create: `tests/proposal_assist/references/test_storage.py`

- [ ] **Step 1: Write the failing tests**

Create `src/call_manager/proposal_assist/references/__init__.py`:

```python
```

(Empty.)

Create `tests/proposal_assist/references/test_storage.py`:

```python
"""ReferenceStorage mirrors AttachmentStorage from B7: per-workspace
filesystem layout, atomic writes, idempotent deletes."""

import os
import uuid
from pathlib import Path

import pytest

from call_manager.proposal_assist.references.storage import ReferenceStorage


def test_path_for_shape(tmp_path):
    storage = ReferenceStorage(root=tmp_path)
    wsid = uuid.uuid4()
    rid = uuid.uuid4()
    p = storage.path_for(workspace_id=wsid, reference_id=rid, suffix=".pdf")
    assert p == tmp_path / str(wsid) / "references" / str(rid) / "original.pdf"


def test_write_atomic_creates_directories_and_persists_bytes(tmp_path):
    storage = ReferenceStorage(root=tmp_path)
    wsid = uuid.uuid4()
    rid = uuid.uuid4()
    storage.write_atomic(
        workspace_id=wsid, reference_id=rid, suffix=".pdf", data=b"PDF bytes"
    )
    p = tmp_path / str(wsid) / "references" / str(rid) / "original.pdf"
    assert p.exists()
    assert p.read_bytes() == b"PDF bytes"
    # No leftover .partial file.
    assert not any(child.suffix == ".partial" for child in p.parent.iterdir())


def test_write_atomic_cleans_partial_on_failure(tmp_path, monkeypatch):
    storage = ReferenceStorage(root=tmp_path)
    wsid = uuid.uuid4()
    rid = uuid.uuid4()

    def boom(src, dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        storage.write_atomic(
            workspace_id=wsid, reference_id=rid, suffix=".pdf", data=b"x"
        )
    target_dir = tmp_path / str(wsid) / "references" / str(rid)
    if target_dir.exists():
        assert list(target_dir.iterdir()) == []


def test_write_text_persists_text_txt(tmp_path):
    storage = ReferenceStorage(root=tmp_path)
    wsid = uuid.uuid4()
    rid = uuid.uuid4()
    storage.path_for(workspace_id=wsid, reference_id=rid, suffix=".pdf").parent.mkdir(
        parents=True
    )
    storage.write_text(workspace_id=wsid, reference_id=rid, text="hello world")
    p = tmp_path / str(wsid) / "references" / str(rid) / "text.txt"
    assert p.exists()
    assert p.read_text() == "hello world"


def test_delete_one_is_idempotent(tmp_path):
    storage = ReferenceStorage(root=tmp_path)
    wsid = uuid.uuid4()
    rid = uuid.uuid4()
    storage.write_atomic(
        workspace_id=wsid, reference_id=rid, suffix=".pdf", data=b"x"
    )
    storage.delete_one(workspace_id=wsid, reference_id=rid)
    assert not (tmp_path / str(wsid) / "references" / str(rid)).exists()
    # Calling again must not raise.
    storage.delete_one(workspace_id=wsid, reference_id=rid)


def test_path_for_rejects_disallowed_suffix(tmp_path):
    storage = ReferenceStorage(root=tmp_path)
    with pytest.raises(ValueError, match="disallowed suffix"):
        storage.path_for(
            workspace_id=uuid.uuid4(),
            reference_id=uuid.uuid4(),
            suffix="../../etc/passwd",
        )
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `uv run pytest tests/proposal_assist/references/test_storage.py -v`

Expected: `ImportError: cannot import name 'ReferenceStorage'`.

- [ ] **Step 3: Implement `storage.py`**

Create `src/call_manager/proposal_assist/references/storage.py`:

```python
"""Filesystem layout for workspace references. Mirrors
AttachmentStorage from B7.

Each reference's files live under
<workspaces_dir>/<workspace_id>/references/<reference_id>/
    ├── original.<ext>   uploaded/fetched bytes
    ├── text.txt         extracted plain text (written by Stage 1)
    └── outline.json     v1.1 only — PageIndex output

The parent workspace dir is shared with B5 (repo/) and B7 (attachments/).
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

_ALLOWED_SUFFIXES = frozenset(
    {".pdf", ".md", ".txt", ".docx", ".rtf", ".csv", ".xls", ".xlsx", ".ipynb"}
)


class ReferenceStorage:
    def __init__(self, root: Path | str):
        self.root = Path(root)

    def path_for(
        self,
        *,
        workspace_id: uuid.UUID,
        reference_id: uuid.UUID,
        suffix: str,
    ) -> Path:
        suffix = suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            raise ValueError(f"disallowed suffix: {suffix!r}")
        return (
            self.root
            / str(workspace_id)
            / "references"
            / str(reference_id)
            / f"original{suffix}"
        )

    def text_path_for(
        self,
        *,
        workspace_id: uuid.UUID,
        reference_id: uuid.UUID,
    ) -> Path:
        return (
            self.root
            / str(workspace_id)
            / "references"
            / str(reference_id)
            / "text.txt"
        )

    def write_atomic(
        self,
        *,
        workspace_id: uuid.UUID,
        reference_id: uuid.UUID,
        suffix: str,
        data: bytes,
    ) -> Path:
        """Write bytes to a `.partial` temp file, fsync, then os.replace
        to the final path. POSIX guarantees the rename is atomic on the
        same filesystem, so concurrent readers see either no file or the
        complete file."""
        final = self.path_for(
            workspace_id=workspace_id, reference_id=reference_id, suffix=suffix
        )
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_suffix(final.suffix + ".partial")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, final)
        except Exception:
            if tmp.exists():
                tmp.unlink()
            raise
        return final

    def write_text(
        self,
        *,
        workspace_id: uuid.UUID,
        reference_id: uuid.UUID,
        text: str,
    ) -> Path:
        """Persist extracted text.txt alongside the original. Caller has
        already created the ref directory via write_atomic."""
        p = self.text_path_for(
            workspace_id=workspace_id, reference_id=reference_id
        )
        p.write_text(text, encoding="utf-8")
        return p

    def delete_one(
        self,
        *,
        workspace_id: uuid.UUID,
        reference_id: uuid.UUID,
    ) -> None:
        """Idempotent: remove the reference's directory (original + text.txt
        + outline.json if present). Caller has already deleted the DB row."""
        ref_dir = (
            self.root / str(workspace_id) / "references" / str(reference_id)
        )
        if ref_dir.exists():
            shutil.rmtree(ref_dir)
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `uv run pytest tests/proposal_assist/references/test_storage.py -v`

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/references/__init__.py src/call_manager/proposal_assist/references/storage.py tests/proposal_assist/references/test_storage.py
git commit -m "feat(references): ReferenceStorage with atomic write + idempotent delete"
```

---

## Task 5: Text extractors

**Files:**
- Create: `src/call_manager/proposal_assist/references/extract.py`
- Create: `tests/proposal_assist/references/test_extract.py`

The extractor returns a list of `(section_path, text)` tuples. For formats with native section structure (markdown, docx, ipynb), each tuple is one section. For PDFs, one tuple per page (section_path=`"Page N"`). For RTF/CSV/XLSX, a single tuple (section_path=`None`).

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/references/test_extract.py`:

```python
"""Text extractors per MIME. Returns a list of (section_path, text)
tuples ready for chunking."""

import io

import pytest
from openpyxl import Workbook

from call_manager.proposal_assist.references.extract import (
    ExtractError,
    extract_text,
)


def test_extract_markdown_splits_by_headings():
    md = b"""# Title

Intro paragraph.

## Methods

Method 1 text.

## Results

Some results.
"""
    sections = extract_text(mime="text/markdown", data=md)
    paths = [s[0] for s in sections]
    assert "Title" in paths
    assert "Title > Methods" in paths
    assert "Title > Results" in paths


def test_extract_plain_text_single_section():
    sections = extract_text(mime="text/plain", data=b"just some text")
    assert sections == [(None, "just some text")]


def test_extract_rtf_strips_formatting():
    rtf = rb"{\rtf1\ansi Hello \b world\b0 .}"
    sections = extract_text(mime="application/rtf", data=rtf)
    assert len(sections) == 1
    assert "Hello" in sections[0][1] and "world" in sections[0][1]


def test_extract_docx_uses_heading_styles():
    from docx import Document

    buf = io.BytesIO()
    d = Document()
    d.add_heading("Title", level=1)
    d.add_paragraph("Intro paragraph.")
    d.add_heading("Methods", level=2)
    d.add_paragraph("Method 1 text.")
    d.save(buf)

    sections = extract_text(
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        data=buf.getvalue(),
    )
    paths = [s[0] for s in sections]
    assert "Title" in paths
    assert "Title > Methods" in paths


def test_extract_csv_returns_schema_and_sample():
    csv = b"name,age,city\nalice,30,paris\nbob,25,berlin\ncarol,40,london\n"
    sections = extract_text(mime="text/csv", data=csv)
    assert len(sections) == 1
    section_path, text = sections[0]
    assert section_path is None
    assert "name" in text and "age" in text and "city" in text
    assert "alice" in text


def test_extract_xlsx_returns_schema_and_sample():
    buf = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.append(["name", "age", "city"])
    ws.append(["alice", 30, "paris"])
    ws.append(["bob", 25, "berlin"])
    wb.save(buf)

    sections = extract_text(
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        data=buf.getvalue(),
    )
    assert len(sections) == 1
    section_path, text = sections[0]
    assert section_path is None
    assert "name" in text and "alice" in text


def test_extract_pdf_returns_one_section_per_page():
    # Use a tiny valid PDF — generate with reportlab? Cheaper: hardcode
    # a one-page minimal PDF.
    pdf_bytes = _make_one_page_pdf("Hello world\nthis is page one")
    sections = extract_text(mime="application/pdf", data=pdf_bytes)
    assert len(sections) >= 1
    paths = [s[0] for s in sections]
    assert all(p.startswith("Page ") for p in paths)
    full_text = " ".join(s[1] for s in sections)
    assert "Hello world" in full_text


def test_extract_ipynb_splits_markdown_cells_as_sections():
    import json

    nb = {
        "cells": [
            {"cell_type": "markdown", "source": ["# Analysis\n", "intro"]},
            {"cell_type": "code", "source": ["print('hi')"], "outputs": [
                {"output_type": "stream", "text": "hi\n"}
            ]},
            {"cell_type": "markdown", "source": ["## Results\n", "good"]},
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    sections = extract_text(
        mime="application/x-ipynb+json",
        data=json.dumps(nb).encode(),
    )
    paths = [s[0] for s in sections]
    assert "Analysis" in paths
    assert "Analysis > Results" in paths


def test_extract_corrupted_pdf_raises():
    with pytest.raises(ExtractError):
        extract_text(mime="application/pdf", data=b"not a pdf at all")


def test_extract_unsupported_mime_raises():
    with pytest.raises(ExtractError, match="unsupported mime"):
        extract_text(mime="application/octet-stream", data=b"x")


def _make_one_page_pdf(text: str) -> bytes:
    """Generate a minimal one-page PDF without requiring reportlab.
    Uses pdfminer's pdfwriter? Actually simplest is to ship a fixture
    file. For testability we'll use a hardcoded small PDF — see fixture."""
    # The smallest valid PDF that pdfminer can parse with text is non-trivial
    # to hand-craft. We use reportlab as a test-time helper.
    from reportlab.pdfgen.canvas import Canvas
    from io import BytesIO

    buf = BytesIO()
    c = Canvas(buf)
    for line in text.split("\n"):
        c.drawString(72, 720, line)
        c.translate(0, -14)
    c.save()
    return buf.getvalue()
```

This test file pulls in `reportlab` only for the helper. Add it to dev deps:

In `pyproject.toml`, find the `[project.optional-dependencies]` or `[tool.uv]` test/dev section, or `dependency-groups.dev` and add `"reportlab>=4.0"`. If no dev group exists, create one. (Verify the convention by reading the file first.)

Run `uv sync` to install.

- [ ] **Step 2: Run tests to confirm they fail**

Run: `uv run pytest tests/proposal_assist/references/test_extract.py -v`

Expected: `ImportError: cannot import name 'extract_text'`.

- [ ] **Step 3: Implement `extract.py`**

Create `src/call_manager/proposal_assist/references/extract.py`:

```python
"""Text extraction for reference files, dispatched by MIME type.

Returns a list of (section_path, text) tuples. For formats with native
section structure (markdown, docx, ipynb), each tuple is one section.
For PDFs, one tuple per page (section_path='Page N'). For RTF / CSV /
XLSX / plain text, a single tuple (section_path=None).

Raises ExtractError on any failure — the caller (ingest) marks the
Reference row text_status='failed' and persists text_error.
"""

from __future__ import annotations

import io
import json
import re
from typing import Iterable


class ExtractError(RuntimeError):
    """Raised when text extraction fails for any reason."""


SectionList = list[tuple[str | None, str]]


def extract_text(*, mime: str, data: bytes) -> SectionList:
    """Dispatch on MIME and return [(section_path, text), ...]."""
    if mime == "text/plain":
        return [(None, data.decode("utf-8", errors="replace"))]
    if mime == "text/markdown":
        return _extract_markdown(data)
    if mime == "application/pdf":
        return _extract_pdf(data)
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return _extract_docx(data)
    if mime == "application/rtf":
        return _extract_rtf(data)
    if mime == "text/csv":
        return _extract_csv(data)
    if mime in (
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ):
        return _extract_xlsx(data)
    if mime == "application/x-ipynb+json":
        return _extract_ipynb(data)
    raise ExtractError(f"unsupported mime: {mime!r}")


# ----- per-format extractors ------------------------------------------------


_MD_HEADING_RE = re.compile(r"^(#+)\s+(.+)$")


def _extract_markdown(data: bytes) -> SectionList:
    text = data.decode("utf-8", errors="replace")
    sections: SectionList = []
    stack: list[tuple[int, str]] = []  # (depth, title) for breadcrumb
    current_lines: list[str] = []
    current_path: str | None = None

    def flush() -> None:
        nonlocal current_lines
        body = "\n".join(current_lines).strip()
        if body:
            sections.append((current_path, body))
        current_lines = []

    for line in text.splitlines():
        m = _MD_HEADING_RE.match(line)
        if m:
            flush()
            depth = len(m.group(1))
            title = m.group(2).strip()
            # Pop stack to depth-1 then push current.
            stack = [(d, t) for (d, t) in stack if d < depth]
            stack.append((depth, title))
            current_path = " > ".join(t for (_, t) in stack)
        else:
            current_lines.append(line)
    flush()
    if not sections:
        # No headings at all — fall back to a single section.
        return [(None, text)]
    return sections


def _extract_pdf(data: bytes) -> SectionList:
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextContainer
    except Exception as e:
        raise ExtractError(f"pdfminer load failed: {e}") from e
    sections: SectionList = []
    try:
        for page_no, layout in enumerate(extract_pages(io.BytesIO(data)), start=1):
            parts = [
                el.get_text()
                for el in layout
                if isinstance(el, LTTextContainer)
            ]
            text = "".join(parts).strip()
            if text:
                sections.append((f"Page {page_no}", text))
    except Exception as e:
        raise ExtractError(f"pdf extraction failed: {e}") from e
    if not sections:
        raise ExtractError("pdf produced no text")
    return sections


def _extract_docx(data: bytes) -> SectionList:
    try:
        from docx import Document
    except Exception as e:
        raise ExtractError(f"python-docx load failed: {e}") from e
    try:
        doc = Document(io.BytesIO(data))
    except Exception as e:
        raise ExtractError(f"docx parse failed: {e}") from e

    sections: SectionList = []
    stack: list[tuple[int, str]] = []
    current_lines: list[str] = []
    current_path: str | None = None

    def flush() -> None:
        nonlocal current_lines
        body = "\n".join(current_lines).strip()
        if body:
            sections.append((current_path, body))
        current_lines = []

    for para in doc.paragraphs:
        style = (para.style.name or "") if para.style else ""
        # Detect headings: "Heading 1", "Heading 2", ...
        m = re.match(r"Heading (\d+)", style)
        if m:
            flush()
            depth = int(m.group(1))
            title = para.text.strip()
            stack = [(d, t) for (d, t) in stack if d < depth]
            stack.append((depth, title))
            current_path = " > ".join(t for (_, t) in stack)
        else:
            current_lines.append(para.text)
    flush()
    if not sections:
        # No headings → whole doc as one section.
        return [(None, "\n".join(p.text for p in doc.paragraphs))]
    return sections


def _extract_rtf(data: bytes) -> SectionList:
    try:
        from striprtf.striprtf import rtf_to_text
    except Exception as e:
        raise ExtractError(f"striprtf load failed: {e}") from e
    try:
        text = rtf_to_text(data.decode("utf-8", errors="replace"))
    except Exception as e:
        raise ExtractError(f"rtf parse failed: {e}") from e
    return [(None, text.strip())]


def _extract_csv(data: bytes) -> SectionList:
    """Schema + first 5 rows as a markdown table — agent gets shape
    without raw data flooding."""
    import csv

    text = data.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ExtractError("empty CSV") from None
    rows = [row for _, row in zip(range(5), reader)]
    return [(None, _to_markdown_table(header, rows))]


def _extract_xlsx(data: bytes) -> SectionList:
    try:
        from openpyxl import load_workbook
    except Exception as e:
        raise ExtractError(f"openpyxl load failed: {e}") from e
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
    except Exception as e:
        raise ExtractError(f"xlsx parse failed: {e}") from e
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header = list(next(rows_iter))
    except StopIteration:
        raise ExtractError("empty xlsx") from None
    header = [str(h) if h is not None else "" for h in header]
    sample = []
    for _, row in zip(range(5), rows_iter):
        sample.append([str(c) if c is not None else "" for c in row])
    return [(None, _to_markdown_table(header, sample))]


def _extract_ipynb(data: bytes) -> SectionList:
    try:
        nb = json.loads(data.decode("utf-8", errors="replace"))
    except Exception as e:
        raise ExtractError(f"ipynb parse failed: {e}") from e

    sections: SectionList = []
    stack: list[tuple[int, str]] = []
    current_lines: list[str] = []
    current_path: str | None = None

    def flush() -> None:
        nonlocal current_lines
        body = "\n".join(current_lines).strip()
        if body:
            sections.append((current_path, body))
        current_lines = []

    for cell in nb.get("cells", []):
        kind = cell.get("cell_type")
        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(source)
        if kind == "markdown":
            # Each markdown cell may have headings; reuse markdown logic.
            for line in source.splitlines():
                m = _MD_HEADING_RE.match(line)
                if m:
                    flush()
                    depth = len(m.group(1))
                    title = m.group(2).strip()
                    stack = [(d, t) for (d, t) in stack if d < depth]
                    stack.append((depth, title))
                    current_path = " > ".join(t for (_, t) in stack)
                else:
                    current_lines.append(line)
        elif kind == "code":
            # Include first 200 chars + any text outputs.
            current_lines.append(source[:200])
            for out in cell.get("outputs", []) or []:
                if out.get("output_type") == "stream":
                    current_lines.append(str(out.get("text", "")))
    flush()
    if not sections:
        return [(None, "")]
    return sections


# ----- helpers --------------------------------------------------------------


def _to_markdown_table(header: Iterable[str], rows: Iterable[Iterable[str]]) -> str:
    header = list(header)
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in rows:
        cells = list(row)
        # Pad/truncate to header length
        cells = (cells + [""] * len(header))[: len(header)]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `uv run pytest tests/proposal_assist/references/test_extract.py -v`

Expected: 10 passed. (Note: the PDF test uses reportlab; if reportlab isn't installable in your env, mark that one test `@pytest.mark.skipif(not _has_reportlab(), ...)` — but the install should work on Linux/macOS.)

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/references/extract.py tests/proposal_assist/references/test_extract.py pyproject.toml uv.lock
git commit -m "feat(references): text extractors for pdf/md/docx/rtf/csv/xlsx/ipynb"
```

---

## Task 6: Section-aware chunker

**Files:**
- Create: `src/call_manager/proposal_assist/references/chunker.py`
- Create: `tests/proposal_assist/references/test_chunker.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/references/test_chunker.py`:

```python
"""Section-aware chunker: one chunk per section when small enough;
sub-chunks within large sections via recursive split with overlap."""

from call_manager.proposal_assist.references.chunker import chunk_sections


def test_small_section_becomes_one_chunk():
    sections = [("Intro", "short intro text")]
    chunks = chunk_sections(sections)
    assert chunks == [("Intro", "short intro text")]


def test_large_section_sub_chunks_with_overlap():
    # Build a 3500-char section — should split into 3 chunks of ~1500 with
    # 100-char overlap.
    body = "abc " * 875  # 3500 chars
    sections = [("Methods", body)]
    chunks = chunk_sections(sections, max_chunk_chars=1500, overlap_chars=100)
    assert len(chunks) >= 3
    for path, text in chunks:
        assert path == "Methods"
        assert len(text) <= 1500
    # Verify overlap exists: end of chunk N appears at start of chunk N+1.
    end_of_first = chunks[0][1][-100:]
    start_of_second = chunks[1][1][:200]
    assert end_of_first in start_of_second or end_of_first[:50] in start_of_second


def test_multiple_sections_preserved():
    sections = [
        ("Intro", "intro body"),
        ("Methods > Stats", "stats body"),
        ("Results", "results body"),
    ]
    chunks = chunk_sections(sections)
    assert len(chunks) == 3
    assert chunks[0][0] == "Intro"
    assert chunks[1][0] == "Methods > Stats"
    assert chunks[2][0] == "Results"


def test_empty_section_dropped():
    sections = [("A", ""), ("B", "real text")]
    chunks = chunk_sections(sections)
    assert chunks == [("B", "real text")]


def test_none_section_path_preserved():
    sections = [(None, "no structure here")]
    chunks = chunk_sections(sections)
    assert chunks == [(None, "no structure here")]
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `uv run pytest tests/proposal_assist/references/test_chunker.py -v`

Expected: `ImportError: cannot import name 'chunk_sections'`.

- [ ] **Step 3: Implement `chunker.py`**

Create `src/call_manager/proposal_assist/references/chunker.py`:

```python
"""Section-aware chunker: produces (section_path, text) chunks ready
for embedding.

Strategy: one chunk per input section when small enough; if any section
exceeds max_chunk_chars, sub-chunk recursively with overlap. section_path
is preserved across sub-chunks so the agent always knows the source
context.
"""

from __future__ import annotations


def chunk_sections(
    sections: list[tuple[str | None, str]],
    *,
    max_chunk_chars: int = 1500,
    overlap_chars: int = 100,
) -> list[tuple[str | None, str]]:
    """Convert a list of extracted sections into a list of chunks.

    sections: output of extract.extract_text(); list of (section_path, body).
    Returns a list of (section_path, chunk_text) where each chunk_text
    is at most max_chunk_chars long.
    """
    out: list[tuple[str | None, str]] = []
    for section_path, body in sections:
        body = body.strip()
        if not body:
            continue
        if len(body) <= max_chunk_chars:
            out.append((section_path, body))
            continue
        # Sub-chunk recursively.
        for sub in _split_with_overlap(body, max_chunk_chars, overlap_chars):
            out.append((section_path, sub))
    return out


def _split_with_overlap(text: str, size: int, overlap: int) -> list[str]:
    """Plain character-based split with overlap. Boring but reliable."""
    if overlap >= size:
        raise ValueError("overlap must be less than size")
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return chunks
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `uv run pytest tests/proposal_assist/references/test_chunker.py -v`

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/references/chunker.py tests/proposal_assist/references/test_chunker.py
git commit -m "feat(references): section-aware chunker with overlap sub-split"
```

---

## Task 7: Embedding wrapper + fake embedder for tests

**Files:**
- Create: `src/call_manager/proposal_assist/references/embed.py`

We expose a small protocol so tests can pass a `FakeEmbedder` without touching Ollama.

- [ ] **Step 1: Implement `embed.py`**

Create `src/call_manager/proposal_assist/references/embed.py`:

```python
"""Embedding wrapper for references.

Wraps the existing OllamaEmbedClient so ingest.py has a stable
seam and tests can inject a FakeEmbedder without touching Ollama.
"""

from __future__ import annotations

from typing import Protocol


class Embedder(Protocol):
    """Stable interface used by ingest. Pluggable for tests."""

    model: str

    def embed(self, text: str) -> list[float]: ...


class OllamaEmbedderAdapter:
    """Thin shim around the existing OllamaEmbedClient so we can match
    the Embedder protocol without the rest of the codebase importing
    embed clients directly."""

    def __init__(self, client):
        self._client = client
        self.model = getattr(client, "model", "unknown")

    def embed(self, text: str) -> list[float]:
        return self._client.embed(text)


class FakeEmbedder:
    """Test double — returns a deterministic vector based on text length
    so different texts get different vectors but the same text always
    returns the same vector."""

    def __init__(self, model: str = "fake-embedder", dim: int = 1024):
        self.model = model
        self._dim = dim

    def embed(self, text: str) -> list[float]:
        # Seed a small variation based on text length and first char,
        # mostly zeros — keeps the vector valid for pgvector but trivial.
        v = [0.0] * self._dim
        if text:
            v[0] = (len(text) % 1024) / 1024.0
            v[1] = (ord(text[0]) % 256) / 256.0
        return v
```

(No test file — this module is purely glue. The fakes get exercised via `test_ingest.py` in the next task.)

- [ ] **Step 2: Sanity import**

Run:

```bash
uv run python -c "from call_manager.proposal_assist.references.embed import FakeEmbedder; e = FakeEmbedder(); print(len(e.embed('hi')))"
```

Expected: `1024`.

- [ ] **Step 3: Commit**

```bash
git add src/call_manager/proposal_assist/references/embed.py
git commit -m "feat(references): Embedder protocol + FakeEmbedder for tests"
```

---

## Task 8: Two-phase ingest pipeline

**Files:**
- Create: `src/call_manager/proposal_assist/references/ingest.py`
- Create: `tests/proposal_assist/references/test_ingest.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/references/test_ingest.py`:

```python
"""Two-phase ingest:
  Phase 1 — write bytes atomically + insert Reference row + commit.
  Phase 2 — sync Stage 1 (extract + chunk + embed) in a separate tx.
            On failure, text_status='failed', row + bytes retained.
"""

import uuid

from sqlalchemy import select

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.references.embed import FakeEmbedder
from call_manager.proposal_assist.references.ingest import (
    IngestResult,
    ingest_bytes,
)
from call_manager.proposal_assist.references.storage import ReferenceStorage
from tests.proposal_assist.conftest import make_user, make_workspace


def test_ingest_text_markdown_creates_row_and_chunks(
    pg_session_factory, tmp_path
):
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
                select(ReferenceChunk).where(
                    ReferenceChunk.reference_id == result.reference_id
                )
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


def test_ingest_extraction_failure_persists_row_with_failed_status(
    pg_session_factory, tmp_path
):
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
                select(ReferenceChunk).where(
                    ReferenceChunk.reference_id == result.reference_id
                )
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
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `uv run pytest tests/proposal_assist/references/test_ingest.py -v`

Expected: `ImportError: cannot import name 'ingest_bytes'`.

- [ ] **Step 3: Implement `ingest.py`**

Create `src/call_manager/proposal_assist/references/ingest.py`:

```python
"""Two-phase reference ingest.

Phase 1 (atomic, B7 pattern): validate, write_atomic, insert Reference
row, commit. On any failure here → no row, no file (caller raises and
returns 400/500).

Phase 2 (sync Stage 1, retain-on-failure): extract text, chunk, embed,
INSERT chunks, UPDATE text_status='done'. On Stage 1 failure → UPDATE
text_status='failed' + text_error, leave row + bytes on disk for retry.

The two phases use SEPARATE transactions because Stage 1 can take
several seconds and we don't want to hold a tx open for that long.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.references.chunker import chunk_sections
from call_manager.proposal_assist.references.embed import Embedder
from call_manager.proposal_assist.references.extract import (
    ExtractError,
    extract_text,
)
from call_manager.proposal_assist.references.storage import ReferenceStorage


@dataclass
class IngestResult:
    reference_id: uuid.UUID
    text_status: str  # 'done' | 'failed'
    text_error: str | None


_MIME_TO_SUFFIX = {
    "application/pdf": ".pdf",
    "text/markdown": ".md",
    "text/plain": ".txt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/rtf": ".rtf",
    "text/csv": ".csv",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/x-ipynb+json": ".ipynb",
}


def suffix_for_mime(mime: str) -> str:
    """Public helper — web layer also needs this when sanitizing filenames."""
    if mime not in _MIME_TO_SUFFIX:
        raise ValueError(f"unsupported mime: {mime!r}")
    return _MIME_TO_SUFFIX[mime]


def ingest_bytes(
    *,
    session_factory,
    session: Session,
    storage: ReferenceStorage,
    embedder: Embedder,
    workspace_id: uuid.UUID,
    kind: str,
    source_type: str,
    source_uri: str | None,
    filename: str,
    mime_type: str,
    data: bytes,
    uploaded_by: int | None,
) -> IngestResult:
    """Run both phases. Caller is responsible for committing the session
    after Phase 1 (we commit Phase 2 internally on a fresh session)."""

    suffix = suffix_for_mime(mime_type)
    reference_id = uuid.uuid4()

    # Phase 1: write bytes, then insert row in the caller's tx.
    storage.write_atomic(
        workspace_id=workspace_id,
        reference_id=reference_id,
        suffix=suffix,
        data=data,
    )
    relative_path = (
        f"{workspace_id}/references/{reference_id}/original{suffix}"
    )
    ref = Reference(
        id=reference_id,
        workspace_id=workspace_id,
        kind=kind,
        source_type=source_type,
        source_uri=source_uri,
        title=None,  # Slice 2 will derive from metadata; v1 leaves NULL
        filename=filename,
        storage_path=relative_path,
        mime_type=mime_type,
        bytes_total=len(data),
        text_status="pending",
        outline_status="skipped",  # v1; v1.1 changes to 'pending'
        uploaded_by=uploaded_by,
    )
    session.add(ref)
    session.flush()
    # Caller commits.

    # Phase 2: run Stage 1 in a fresh session.
    with session_factory() as s2:
        ref2 = s2.get(Reference, reference_id)
        if ref2 is None:
            # Caller rolled back Phase 1 between flush and Phase 2.
            return IngestResult(
                reference_id=reference_id,
                text_status="failed",
                text_error="row not committed",
            )
        ref2.text_status = "running"
        s2.commit()

    try:
        sections = extract_text(mime=mime_type, data=data)
        chunks = chunk_sections(sections)
        if not chunks:
            raise ExtractError("no chunks produced from extracted text")

        # Persist text.txt for v1.1 PageIndex + reembed CLI.
        full_text = "\n\n".join(t for (_, t) in sections)
        storage.write_text(
            workspace_id=workspace_id,
            reference_id=reference_id,
            text=full_text,
        )

        # Embed + insert chunks in one tx.
        with session_factory() as s2:
            for i, (section_path, text) in enumerate(chunks):
                vec = embedder.embed(text)
                s2.add(
                    ReferenceChunk(
                        reference_id=reference_id,
                        workspace_id=workspace_id,
                        chunk_index=i,
                        section_path=section_path,
                        text=text,
                        embedding=vec,
                        embedding_model=embedder.model,
                    )
                )
            ref2 = s2.get(Reference, reference_id)
            ref2.text_status = "done"
            ref2.text_indexed_at = datetime.now(UTC)
            ref2.text_chars_total = len(full_text)
            s2.commit()
        return IngestResult(
            reference_id=reference_id, text_status="done", text_error=None
        )
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        with session_factory() as s2:
            ref2 = s2.get(Reference, reference_id)
            if ref2 is not None:
                ref2.text_status = "failed"
                ref2.text_error = msg
                s2.commit()
        return IngestResult(
            reference_id=reference_id, text_status="failed", text_error=msg
        )
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `uv run pytest tests/proposal_assist/references/test_ingest.py -v`

Expected: 3 passed.

- [ ] **Step 5: Run the full references test set to confirm everything together still works**

Run: `uv run pytest tests/proposal_assist/references -v`

Expected: 22 passed (3 models + 6 storage + 10 extract + 5 chunker + 3 ingest).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/references/ingest.py tests/proposal_assist/references/test_ingest.py
git commit -m "feat(references): two-phase ingest with retain-on-failure Stage 1"
```

---

## Task 9: Web routes — panel + upload + raw serve

**Files:**
- Create: `src/call_manager/proposal_assist/web/references.py`
- Create: `tests/proposal_assist/test_web_workspaces_references.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_web_workspaces_references.py`:

```python
"""References web routes (Slice 1 — list + upload + raw serve)."""

import io
import uuid

import pytest
from fastapi.testclient import TestClient

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Reference, User, Workspace
from call_manager.main import create_app


def _build_app(monkeypatch, pg_url, tmp_path):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))
    return create_app()


def _authed(app, *, user_id):
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=user_id))
    return client


def _seed_owner_and_workspace(session, *, email="owner@ki.se"):
    owner = User(email=email)
    session.add(owner)
    session.flush()
    ws = Workspace(owner_id=owner.id, title="T")
    session.add(ws)
    session.commit()
    return owner.id, ws.id


def test_panel_renders_for_owner_empty_list(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    try:
        resp = client.get(f"/workspaces/{wsid}/references")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    assert "references" in resp.text.lower()


def test_panel_returns_403_for_non_viewer(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        _, wsid = _seed_owner_and_workspace(s)
        other = User(email="other@ki.se")
        s.add(other)
        s.commit()
        other_id = other.id
    client = _authed(app, user_id=other_id)
    try:
        resp = client.get(f"/workspaces/{wsid}/references")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403


def test_upload_markdown_creates_row(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    md = b"# Hello\n\nworld\n"
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references",
            data={"kind": "paper"},
            files={"file": ("hello.md", io.BytesIO(md), "text/markdown")},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    assert "hello.md" in resp.text or "Hello" in resp.text

    from sqlalchemy import select
    with pg_session_factory() as s:
        refs = list(s.scalars(select(Reference).where(Reference.workspace_id == wsid)))
    assert len(refs) == 1
    assert refs[0].text_status == "done"


def test_upload_returns_403_for_non_owner(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        _, wsid = _seed_owner_and_workspace(s)
        viewer = User(email="viewer@ki.se")
        s.add(viewer)
        s.commit()
        viewer_id = viewer.id
        # Add viewer permission (mirrors B7 pattern; viewers CAN read, can't upload).
        from call_manager.db.models import WorkspaceViewer
        s.add(WorkspaceViewer(workspace_id=wsid, user_id=viewer_id))
        s.commit()
    client = _authed(app, user_id=viewer_id)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references",
            data={"kind": "paper"},
            files={"file": ("x.md", io.BytesIO(b"# t"), "text/markdown")},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403


def test_upload_rejects_bad_mime(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references",
            data={"kind": "paper"},
            files={"file": ("x.exe", io.BytesIO(b"MZ\x00\x00"), "application/x-msdownload")},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 400


def test_upload_failed_extraction_persists_row_with_failed_status(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Phase 2 failure: corrupted PDF; row + file retained; UI surfaces failure."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references",
            data={"kind": "paper"},
            files={"file": ("bad.pdf", io.BytesIO(b"not really a pdf"), "application/pdf")},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    assert "failed" in resp.text.lower()

    from sqlalchemy import select
    with pg_session_factory() as s:
        refs = list(s.scalars(select(Reference).where(Reference.workspace_id == wsid)))
    assert len(refs) == 1
    assert refs[0].text_status == "failed"
    assert refs[0].text_error is not None


def test_raw_serves_bytes_with_attachment_disposition(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Codex High #3 rev4 — references serve a wide MIME range; force
    download with attachment disposition + nosniff to prevent app-origin
    XSS from HTML/SVG/etc."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    md = b"# t\n"
    try:
        up = client.post(
            f"/workspaces/{wsid}/references",
            data={"kind": "paper"},
            files={"file": ("hello.md", io.BytesIO(md), "text/markdown")},
        )
        assert up.status_code == 200
        from sqlalchemy import select
        with pg_session_factory() as s:
            ref = s.scalar(select(Reference).where(Reference.workspace_id == wsid))
        resp = client.get(f"/workspaces/{wsid}/references/{ref.id}/raw")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    assert resp.content == md
    # Force download — NEVER inline.
    cd = resp.headers["content-disposition"]
    assert cd.startswith("attachment;")
    # nosniff to prevent MIME-sniffing into HTML.
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_raw_returns_404_for_other_workspace_id(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Cross-workspace IDOR check: passing the right ref_id but the
    wrong workspace_id in the URL returns 404, not 403 — don't leak
    existence."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid_a, wsid_a = _seed_owner_and_workspace(s, email="a@ki.se")
        uid_b, wsid_b = _seed_owner_and_workspace(s, email="b@ki.se")
    client = _authed(app, user_id=uid_a)
    md = b"# t\n"
    try:
        up = client.post(
            f"/workspaces/{wsid_a}/references",
            data={"kind": "paper"},
            files={"file": ("t.md", io.BytesIO(md), "text/markdown")},
        )
        from sqlalchemy import select
        with pg_session_factory() as s:
            ref = s.scalar(select(Reference).where(Reference.workspace_id == wsid_a))
        # Now: workspace_a's owner asks for ref_a's bytes via workspace_b's URL.
        resp = client.get(f"/workspaces/{wsid_b}/references/{ref.id}/raw")
    finally:
        client.__exit__(None, None, None)
    # owner_a isn't allowed on workspace_b → 403 from the workspace check.
    # That's fine (doesn't leak existence). Accept either 403 or 404.
    assert resp.status_code in (403, 404)
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_references.py -v`

Expected: `ModuleNotFoundError: No module named 'call_manager.proposal_assist.web.references'`.

- [ ] **Step 3: Implement the router**

Create `src/call_manager/proposal_assist/web/references.py`:

```python
"""Web routes for references — Slice 1 (panel + upload + raw serve).

Permission policy:
- GET  /workspaces/<id>/references           owner OR viewer
- POST /workspaces/<id>/references           owner only
- GET  /workspaces/<id>/references/<id>/raw  owner OR viewer

Atomicity: mirrors B7 attachments — Phase 1 atomic, Phase 2 sync Stage 1
retain-on-failure. See ingest.py.
"""

from __future__ import annotations

import html as _html
import logging
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import Reference, Workspace, WorkspaceViewer
from call_manager.db.session import get_db_dependency
from call_manager.proposal_assist.references.embed import OllamaEmbedderAdapter
from call_manager.proposal_assist.references.ingest import (
    IngestResult,
    ingest_bytes,
    suffix_for_mime,
)
from call_manager.proposal_assist.references.storage import ReferenceStorage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["references"])

_ALLOWED_MIMES = frozenset(
    {
        "application/pdf",
        "text/markdown",
        "text/plain",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/rtf",
        "text/csv",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/x-ipynb+json",
    }
)
_MAX_BYTES = 50 * 1024 * 1024  # 50 MB per spec REFERENCES_MAX_BYTES default
_CHUNK_BYTES = 64 * 1024


def _require_owner(*, workspace_id, user, session) -> Workspace:
    ws = session.scalar(
        select(Workspace).where(
            Workspace.id == workspace_id, Workspace.soft_deleted_at.is_(None)
        )
    )
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if ws.owner_id != user.id:
        raise HTTPException(status_code=403, detail="owner only")
    return ws


def _require_owner_or_viewer(*, workspace_id, user, session) -> Workspace:
    ws = session.scalar(
        select(Workspace).where(
            Workspace.id == workspace_id, Workspace.soft_deleted_at.is_(None)
        )
    )
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if ws.owner_id == user.id:
        return ws
    is_viewer = session.scalar(
        select(WorkspaceViewer.user_id).where(
            WorkspaceViewer.workspace_id == workspace_id,
            WorkspaceViewer.user_id == user.id,
        )
    )
    if is_viewer is None:
        raise HTTPException(status_code=403, detail="owner or viewer only")
    return ws


_FILENAME_OK = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_filename(name: str) -> str:
    cleaned = _FILENAME_OK.sub("_", name)
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    cleaned = cleaned.strip("._")
    return cleaned[:120] or "reference"


def _storage_for(request: Request) -> ReferenceStorage:
    return ReferenceStorage(root=Path(request.app.state.settings.workspaces_dir))


async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    buf = bytearray()
    while True:
        chunk = await upload.read(_CHUNK_BYTES)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"file exceeds {max_bytes // (1024 * 1024)} MB limit",
            )
    return bytes(buf)


@router.get("/{workspace_id}/references", response_class=HTMLResponse)
def references_panel(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(get_db_dependency),  # noqa: B008
) -> HTMLResponse:
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id, user=user, session=session
    )
    refs = list(
        session.scalars(
            select(Reference)
            .where(Reference.workspace_id == workspace_id)
            .order_by(Reference.created_at.desc())
        )
    )
    is_owner = ws.owner_id == user.id
    return request.app.state.templates.TemplateResponse(
        "proposal_assist/workspace_references.html",
        {
            "request": request,
            "user": user,
            "workspace": ws,
            "references": refs,
            "is_owner": is_owner,
        },
    )


@router.post("/{workspace_id}/references")
async def upload_reference(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(get_db_dependency),  # noqa: B008
    kind: str = Form("paper"),
    file: UploadFile = File(...),  # noqa: B008
) -> HTMLResponse:
    ws = _require_owner(workspace_id=workspace_id, user=user, session=session)

    content_type = (file.content_type or "").lower()
    if content_type not in _ALLOWED_MIMES:
        return HTMLResponse(
            f"<p>Unsupported content_type: {_html.escape(repr(content_type))}</p>",
            status_code=400,
        )
    if kind not in ("paper", "prior_proposal", "analysis", "data"):
        return HTMLResponse(
            f"<p>Invalid kind: {_html.escape(repr(kind))}</p>",
            status_code=400,
        )

    try:
        data = await _read_capped(file, _MAX_BYTES)
    except HTTPException as e:
        return HTMLResponse(
            f"<p>{_html.escape(str(e.detail))}</p>", status_code=e.status_code
        )

    storage = _storage_for(request)
    embed_client = request.app.state.embed_client  # OllamaEmbedClient from main.py
    embedder = OllamaEmbedderAdapter(embed_client)

    try:
        # ingest_bytes runs Phase 1 (atomic write + INSERT) using the
        # caller's session. We commit here before Phase 2 runs internally.
        result: IngestResult = ingest_bytes(
            session_factory=request.app.state.session_factory,
            session=session,
            storage=storage,
            embedder=embedder,
            workspace_id=ws.id,
            kind=kind,
            source_type="uploaded",
            source_uri=None,
            filename=_sanitize_filename(file.filename or f"reference{suffix_for_mime(content_type)}"),
            mime_type=content_type,
            data=data,
            uploaded_by=user.id,
        )
        session.commit()
    except OSError as e:
        logger.warning(
            "upload_reference: write_atomic failed for workspace=%s: %s",
            ws.id, e,
        )
        session.rollback()
        return HTMLResponse("<p>storage write failed</p>", status_code=500)
    except ValueError as e:
        session.rollback()
        return HTMLResponse(
            f"<p>{_html.escape(str(e))}</p>", status_code=400
        )

    # Phase 2 has already run (ingest_bytes handles its own tx). Load the
    # final row state and render the row partial.
    ref = session.scalar(select(Reference).where(Reference.id == result.reference_id))
    return request.app.state.templates.TemplateResponse(
        "proposal_assist/partials/reference_row.html",
        {"request": request, "ref": ref, "workspace": ws, "is_owner": True},
    )


@router.get("/{workspace_id}/references/{reference_id}/raw")
def serve_reference_raw(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(get_db_dependency),  # noqa: B008
) -> Response:
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id, user=user, session=session
    )
    ref = session.scalar(select(Reference).where(Reference.id == reference_id))
    if ref is None or ref.workspace_id != ws.id:
        # Don't leak existence — 404 for cross-workspace lookups too.
        raise HTTPException(status_code=404, detail="reference not found")

    storage = _storage_for(request)
    suffix = suffix_for_mime(ref.mime_type)
    path = storage.path_for(
        workspace_id=ws.id, reference_id=ref.id, suffix=suffix
    )
    if not path.exists():
        raise HTTPException(status_code=404, detail="reference bytes missing")

    safe_name = _sanitize_filename(ref.filename)
    return Response(
        content=path.read_bytes(),
        media_type=ref.mime_type,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=300",
        },
    )
```

- [ ] **Step 4: Run tests to confirm they fail with the next missing piece (the template)**

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_references.py -v`

Expected: tests likely fail because the `workspace_references.html` template doesn't exist yet, or because the router isn't registered. Move on to Task 10 to add the templates, then Task 11 to wire the router.

- [ ] **Step 5: Commit the router (even with failing tests — Task 10 + 11 finish it)**

```bash
git add src/call_manager/proposal_assist/web/references.py tests/proposal_assist/test_web_workspaces_references.py
git commit -m "feat(references): web router skeleton (panel + upload + raw serve)"
```

---

## Task 10: Templates

**Files:**
- Create: `src/call_manager/web/templates/proposal_assist/workspace_references.html`
- Create: `src/call_manager/web/templates/proposal_assist/partials/reference_row.html`

- [ ] **Step 1: Write `workspace_references.html`**

Create:

```html
{% extends "base.html" %}
{% block content %}
<hgroup>
  <h1>References — {{ workspace.title|e }}</h1>
  <p>
    <a href="/workspaces/{{ workspace.id }}">← back to workspace</a>
    | <a href="/workspaces/{{ workspace.id }}/files">Files</a>
  </p>
</hgroup>

{% if is_owner %}
<form method="post" enctype="multipart/form-data"
      action="/workspaces/{{ workspace.id }}/references"
      hx-post="/workspaces/{{ workspace.id }}/references"
      hx-target="#reference-list"
      hx-swap="afterbegin"
      hx-encoding="multipart/form-data"
      hx-on::after-request="this.reset()">
  <label>
    Kind:
    <select name="kind">
      <option value="paper">paper</option>
      <option value="prior_proposal">prior_proposal</option>
      <option value="analysis">analysis</option>
      <option value="data">data</option>
    </select>
  </label>
  <input type="file" name="file" required
         accept=".pdf,.md,.txt,.docx,.rtf,.csv,.xls,.xlsx,.ipynb">
  <button type="submit">Upload</button>
</form>
{% else %}
<p><em>(read-only — only the workspace owner can upload references)</em></p>
{% endif %}

<div id="reference-list">
  {% for ref in references %}
    {% include "proposal_assist/partials/reference_row.html" %}
  {% endfor %}
</div>
{% endblock %}
```

- [ ] **Step 2: Write `partials/reference_row.html`**

Create:

```html
{% set ref = ref %}
<div class="reference-row" data-reference-id="{{ ref.id }}">
  <strong>{{ ref.filename|e }}</strong>
  <span class="kind">[{{ ref.kind }}]</span>
  {% if ref.text_status == "done" %}
    <span class="status status-ok">✓ indexed ({{ ref.text_chars_total or 0 }} chars)</span>
  {% elif ref.text_status == "failed" %}
    <span class="status status-failed">✗ failed: {{ (ref.text_error or "")|e }}</span>
  {% elif ref.text_status == "skipped" %}
    <span class="status status-skipped">— data (schema only)</span>
  {% else %}
    <span class="status status-pending">⏳ {{ ref.text_status }}</span>
  {% endif %}
  <a href="/workspaces/{{ ref.workspace_id }}/references/{{ ref.id }}/raw">download</a>
  <small>{{ ref.created_at | display_dt }}</small>
</div>
```

- [ ] **Step 3: Commit templates**

```bash
git add src/call_manager/web/templates/proposal_assist/workspace_references.html src/call_manager/web/templates/proposal_assist/partials/reference_row.html
git commit -m "feat(references): workspace_references.html + reference_row partial"
```

---

## Task 11: Wire router + side-nav link + final integration

**Files:**
- Modify: `src/call_manager/main.py`
- Modify: `src/call_manager/web/templates/proposal_assist/workspace_main.html`

- [ ] **Step 1: Register the router in `main.py`**

Find the line in `main.py` that registers the existing `workspaces_router` (B6) or `files_router` (B7). Add:

```python
from call_manager.proposal_assist.web.references import router as references_router
```

at the top with the other proposal_assist router imports, and:

```python
app.include_router(references_router)
```

after the existing `files_router` registration.

- [ ] **Step 2: Add a side-nav link in `workspace_main.html`**

Find the existing "Files" link added by B7. Add `| <a href="/workspaces/{{ workspace.id }}/references">References</a>` next to it.

- [ ] **Step 3: Run the full references test suite**

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_references.py tests/proposal_assist/references -v`

Expected: all 35 tests pass (3 models + 6 storage + 10 extract + 5 chunker + 3 ingest + 8 web routes).

- [ ] **Step 4: Run the full project test suite**

Run: `uv run pytest -q`

Expected: 987 passed, 1 skipped (952 baseline + 35 new tests). Adjust the expected number if any pre-existing test got accidentally broken — investigate before proceeding.

- [ ] **Step 5: ruff format + check**

```bash
uv run ruff format src tests
uv run ruff check src tests
```

Expected: format reports any reformats; check passes.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/main.py src/call_manager/web/templates/proposal_assist/workspace_main.html
git commit -m "feat(references): wire router + side-nav link"
```

If `ruff format` reformatted anything, follow with:

```bash
git add -A
git commit -m "style(references): ruff format"
```

---

## Final Verification (manual smoke test)

- [ ] **Step 1: Start the dev server**

```bash
cd /home/parlar_ai/dev/call-monitor
set -a; source .env; set +a
DATABASE_URL='postgresql+psycopg://call_manager:call_manager@localhost:5432/call_manager' \
APP_BASE_URL=http://127.0.0.1:8001 \
uv run uvicorn call_manager.main:create_app --factory --host 127.0.0.1 --port 8001
```

- [ ] **Step 2: Walk the flow in a browser**

1. Log in (or mint a cookie if email delivery is off in dev).
2. Create a workspace.
3. Navigate to `/workspaces/<id>/references` via the new side-nav link.
4. Upload a small Markdown file. Verify it appears with "✓ indexed".
5. Click "download" — verify the browser downloads (NOT renders inline) and the file matches.
6. Upload a corrupted "PDF" (e.g., rename a text file). Verify it appears with "✗ failed: ..." status, row + bytes retained.
7. Open a SQL prompt and confirm `SELECT count(*) FROM reference_chunks WHERE workspace_id = '<wsid>'` matches the number of sections in the markdown file.

Document the smoke test in the merge commit message.

- [ ] **Step 3: Hand off to controller for merge**

The controller will use `superpowers:finishing-a-development-branch` to merge to master.

---

## Self-review notes

**Spec coverage** — Slice 1 lines from Section 5 of the spec, each mapped to a task:
- migration → Task 2
- ORM models → Task 3
- storage helpers → Task 4
- text extractors → Task 5
- section-aware chunker → Task 6
- embedding via OllamaEmbedClient → Task 7
- two-phase ingest → Task 8
- web routes (panel/upload/raw with attachment+nosniff) → Task 9
- templates → Task 10
- main.py wiring + side-nav link → Task 11

Not in Slice 1 per the spec: MCP tools, fetch dispatcher, delete/copy routes, chat composer integration, admin panel, reembed CLI. None should appear in this plan's tasks.

**Section detection limitation** — PDFs in v1 produce one chunk per page (section_path = "Page N"). Real heading detection requires either PyMuPDF (AGPL — flag) or font-size analysis via pdfminer.six's LAParams (more work). The spec said "PyMuPDF; if absent, headings via pdftotext layout"; v1 ships the simpler page-per-section approach. Document at the top of `extract.py:_extract_pdf`. v1.x can upgrade.

**Known gap shipped** — no delete route (lands in Slice 5). Users who upload a typo'd file have no UI recovery in v1 — they live with the failed row. Acceptable for Slice 1 because the next slice adds the agent tools that make the corpus actually useful; admin SQL delete is the operator workaround.

**Test count target** at end of Slice 1: 987 passed (master baseline 952 + 35 new). Adjust if reality differs.
