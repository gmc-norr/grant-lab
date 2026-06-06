# Citation Metadata Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Revision 2** — incorporates a Codex review of this plan (enforced LLM service, cache-token accounting, explicit citation-LLM provider settings, index/constraint hardening, migration-test safety).

**Goal:** Lay the data + LLM-infrastructure foundation for citation provenance — CSL bibliographic metadata columns on `references`, original-bytes hashing at ingest, and a single **enforced** citation-LLM service that checks caps and records all usage to a ledger.

**Architecture:** This is **Slice 1A** of the citation-provenance spec ([`docs/superpowers/specs/2026-06-06-citation-provenance-evidence-ledger-design.md`](../specs/2026-06-06-citation-provenance-evidence-ledger-design.md)). It builds the pieces every later slice depends on but adds no user-visible behavior: schema (migration 0036), the `original_sha256` freeze anchor, and `CitationLLMService` — the only supported way to call an LLM in the citation subsystem (it wraps a structured client, enforces per-workspace/global daily caps, and writes a `citation_llm_usage` row on success/failure/timeout). Slice 1B (extended fetchers, heuristic+LLM extraction wired into ingest, citekey generation, owner approval UI, and lazy-hashing of pre-existing references — see "Deferred") and Slices 2–4 follow.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x + Alembic, PostgreSQL 16 (JSONB), pytest with the session-scoped pgvector testcontainer (`pg_session_factory` / `migrated_engine` from `tests/conftest.py`). LLM clients: `call_manager.ranking.anthropic_client.AnthropicClient`, `call_manager.ranking.ollama_client.OllamaClient` — both return an OpenAI-shaped envelope with a `usage` block (Anthropic additionally exposes `cache_read_input_tokens` / `cache_creation_input_tokens`).

---

## File Structure

- `alembic/versions/0036_citation_metadata_foundation.py` — **new** migration (references columns + `citation_llm_usage` table, CHECKs, indexes). Down-revision `0035`.
- `src/call_manager/db/models.py` — **modify**: metadata columns + `original_sha256` hex CHECK on `Reference`; new `CitationLlmUsage` model with `__table_args__` indexes matching the migration.
- `src/call_manager/proposal_assist/references/ingest.py` — **modify**: compute + store `original_sha256`.
- `src/call_manager/config.py` — **modify**: citation-LLM provider/model/timeout/max-output settings + daily cap settings.
- `src/call_manager/proposal_assist/citations/__init__.py` — **new** (empty).
- `src/call_manager/proposal_assist/citations/llm.py` — **new**: `StructuredLLM` (internal) + `build_citation_llm(settings)` + `CitationLLMConfigError`.
- `src/call_manager/proposal_assist/citations/usage.py` — **new**: `record_usage(...)` + `check_citation_caps(...)` (internals).
- `src/call_manager/proposal_assist/citations/service.py` — **new**: `CitationLLMService` — the only supported citation-LLM call path.
- Tests: `tests/test_migration_0036_citation_metadata.py`, `tests/proposal_assist/references/test_ingest.py` (append), `tests/proposal_assist/references/test_models.py` (append), `tests/proposal_assist/citations/test_llm.py`, `tests/proposal_assist/citations/test_service.py`.

---

### Task 1: Migration 0036 — references columns + `citation_llm_usage` (with CHECKs + indexes)

**Files:**
- Create: `alembic/versions/0036_citation_metadata_foundation.py`
- Test: `tests/test_migration_0036_citation_metadata.py`

- [ ] **Step 1: Write the migration**

```python
"""Citation metadata foundation: references CSL columns + citation_llm_usage

Revision ID: 0036
Revises: 0035
Create Date: 2026-06-06

Adds CSL-JSON bibliographic metadata + provenance/freezing columns to
references, and a citation_llm_usage ledger for the citation subsystem's LLM
spend (metadata extraction now; audit later). See
docs/superpowers/specs/2026-06-06-citation-provenance-evidence-ledger-design.md.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("references", sa.Column("csl_json", postgresql.JSONB(), nullable=True))
    op.add_column("references", sa.Column("csl_json_extracted", postgresql.JSONB(), nullable=True))
    op.add_column("references", sa.Column("citekey", sa.Text(), nullable=True))
    op.add_column(
        "references",
        sa.Column("metadata_status", sa.Text(), nullable=False, server_default="pending"),
    )
    op.add_column("references", sa.Column("metadata_source", sa.Text(), nullable=True))
    op.add_column("references", sa.Column("original_sha256", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_references_metadata_status",
        "references",
        "metadata_status IN ('pending','extracted','verified_external','approved','failed')",
    )
    op.create_check_constraint(
        "ck_references_original_sha256_hex",
        "references",
        "original_sha256 IS NULL OR original_sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.create_index(
        "uq_references_workspace_citekey",
        "references",
        ["workspace_id", "citekey"],
        unique=True,
        postgresql_where=sa.text("citekey IS NOT NULL"),
    )

    op.create_table(
        "citation_llm_usage",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("op", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('ok','failed','timeout')", name="ck_citation_llm_usage_status"
        ),
        sa.CheckConstraint(
            "tokens_in >= 0 AND tokens_out >= 0", name="ck_citation_llm_usage_tokens_nonneg"
        ),
    )
    # Composite serves the per-workspace daily sum; standalone created_at
    # serves the GLOBAL daily sum (which filters created_at only — the
    # composite's leading workspace_id column can't serve it efficiently).
    op.create_index(
        "ix_citation_llm_usage_ws_created", "citation_llm_usage", ["workspace_id", "created_at"]
    )
    op.create_index("ix_citation_llm_usage_created", "citation_llm_usage", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_citation_llm_usage_created", table_name="citation_llm_usage")
    op.drop_index("ix_citation_llm_usage_ws_created", table_name="citation_llm_usage")
    op.drop_table("citation_llm_usage")
    op.drop_index("uq_references_workspace_citekey", table_name="references")
    op.drop_constraint("ck_references_original_sha256_hex", "references", type_="check")
    op.drop_constraint("ck_references_metadata_status", "references", type_="check")
    for col in (
        "original_sha256",
        "metadata_source",
        "metadata_status",
        "citekey",
        "csl_json_extracted",
        "csl_json",
    ):
        op.drop_column("references", col)
```

- [ ] **Step 2: Write the round-trip migration test (restores head in `finally`)**

```python
"""Round-trip checks for migration 0036 (citation metadata foundation).

pg_url is the SHARED session container; this test downgrades then upgrades,
so it MUST leave the schema at head for later tests — done in finally.
"""

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def _columns(conn, table):
    return {c["name"] for c in inspect(conn).get_columns(table)}


def test_0036_round_trip(migrated_engine, pg_url):
    engine = create_engine(pg_url)
    try:
        with engine.connect() as conn:
            assert {"csl_json", "csl_json_extracted", "citekey", "metadata_status",
                    "metadata_source", "original_sha256"} <= _columns(conn, "references")
            assert "citation_llm_usage" in inspect(conn).get_table_names()
            idx = {i["name"] for i in inspect(conn).get_indexes("citation_llm_usage")}
            assert "ix_citation_llm_usage_created" in idx
            assert "ix_citation_llm_usage_ws_created" in idx

        command.downgrade(_cfg(pg_url), "0035")
        with engine.connect() as conn:
            assert "original_sha256" not in _columns(conn, "references")
            assert "citation_llm_usage" not in inspect(conn).get_table_names()
    finally:
        # ALWAYS restore head, even if an assertion above failed — later tests
        # in this session reuse the shared container at head.
        command.upgrade(_cfg(pg_url), "head")
        engine.dispose()
```

- [ ] **Step 3: Run the migration test**

Run: `uv run pytest tests/test_migration_0036_citation_metadata.py -v`
Expected: PASS (columns + table + both indexes at head; gone at 0035; restored).

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/0036_citation_metadata_foundation.py tests/test_migration_0036_citation_metadata.py
git commit -m "feat(citations): migration 0036 — references metadata + citation_llm_usage (checks+indexes)"
```

---

### Task 2: Models — `Reference` columns + `CitationLlmUsage`

**Files:**
- Modify: `src/call_manager/db/models.py`
- Test: `tests/proposal_assist/references/test_models.py` (append)

- [ ] **Step 1: Write the failing model tests**

Append to `tests/proposal_assist/references/test_models.py`:

```python
def test_reference_metadata_columns_round_trip(pg_session_factory):
    import uuid
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from call_manager.db.models import Reference
    from tests.proposal_assist.conftest import make_user, make_workspace

    sha = "a" * 64  # valid lowercase 64-hex

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s))
        s.commit()
        wsid = ws.id

    rid = uuid.uuid4()
    with pg_session_factory() as s:
        s.add(Reference(
            id=rid, workspace_id=wsid, kind="paper", source_type="uploaded",
            source_uri=None, filename="p.pdf", storage_path="x", mime_type="application/pdf",
            csl_json={"title": "T"}, csl_json_extracted={"title": "T?"},
            citekey="smith2021", metadata_status="approved", metadata_source="llm",
            original_sha256=sha,
        ))
        s.commit()

    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == rid))
        assert ref.csl_json == {"title": "T"}
        assert ref.citekey == "smith2021"
        assert ref.original_sha256 == sha

    # CHECK rejects a bad status.
    with pg_session_factory() as s:
        s.add(Reference(
            id=uuid.uuid4(), workspace_id=wsid, kind="paper", source_type="uploaded",
            source_uri=None, filename="q.pdf", storage_path="y", mime_type="application/pdf",
            metadata_status="bogus",
        ))
        try:
            s.commit()
            raise AssertionError("expected IntegrityError on bad metadata_status")
        except IntegrityError:
            pass

    # CHECK rejects a non-64-hex original_sha256.
    with pg_session_factory() as s:
        s.add(Reference(
            id=uuid.uuid4(), workspace_id=wsid, kind="paper", source_type="uploaded",
            source_uri=None, filename="r.pdf", storage_path="z", mime_type="application/pdf",
            original_sha256="abc123",
        ))
        try:
            s.commit()
            raise AssertionError("expected IntegrityError on bad original_sha256")
        except IntegrityError:
            pass


def test_reference_citekey_unique_per_workspace(pg_session_factory):
    import uuid
    from sqlalchemy.exc import IntegrityError
    from call_manager.db.models import Reference
    from tests.proposal_assist.conftest import make_user, make_workspace

    def _ref(wsid, name):
        return Reference(
            id=uuid.uuid4(), workspace_id=wsid, kind="paper", source_type="uploaded",
            source_uri=None, filename=name, storage_path=name, mime_type="application/pdf",
            citekey="smith2021",
        )

    with pg_session_factory() as s:
        ws_a = make_workspace(s, owner=make_user(s, email="a@ki.se"))
        ws_b = make_workspace(s, owner=make_user(s, email="b@ki.se"))
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


def test_citation_llm_usage_insert_and_nonneg(pg_session_factory):
    import uuid
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from call_manager.db.models import CitationLlmUsage
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s))
        s.commit()
        wsid = ws.id

    with pg_session_factory() as s:
        s.add(CitationLlmUsage(workspace_id=wsid, op="metadata_extract", status="ok",
                               tokens_in=100, tokens_out=20, model="gemma"))
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/proposal_assist/references/test_models.py -v -k "metadata_columns or citekey_unique or citation_llm_usage"`
Expected: FAIL (invalid kwargs / `ImportError: CitationLlmUsage`).

- [ ] **Step 3: Add columns + CHECKs to `Reference`**

In `class Reference(Base)`, add columns:

```python
    csl_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    csl_json_extracted: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    citekey: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    metadata_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
```

Add to `Reference.__table_args__`:

```python
        CheckConstraint(
            "metadata_status IN ('pending','extracted','verified_external','approved','failed')",
            name="ck_references_metadata_status",
        ),
        CheckConstraint(
            "original_sha256 IS NULL OR original_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_references_original_sha256_hex",
        ),
        Index(
            "uq_references_workspace_citekey",
            "workspace_id",
            "citekey",
            unique=True,
            postgresql_where=text("citekey IS NOT NULL"),
        ),
```

(If `Index` / `text` aren't yet imported in `models.py`, add them: `from sqlalchemy import ... Index, text`. Check the existing import block first and only add what's missing.)

- [ ] **Step 4: Add the `CitationLlmUsage` model (indexes match the migration exactly)**

After `class ReferenceChunk(Base)`:

```python
class CitationLlmUsage(Base):
    """Append-only ledger of every LLM call the citation subsystem makes
    (metadata extraction now; audit later). Basis for per-workspace + global
    daily caps. Written only via CitationLLMService. See the spec."""

    __tablename__ = "citation_llm_usage"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ok','failed','timeout')", name="ck_citation_llm_usage_status"
        ),
        CheckConstraint(
            "tokens_in >= 0 AND tokens_out >= 0", name="ck_citation_llm_usage_tokens_nonneg"
        ),
        Index("ix_citation_llm_usage_ws_created", "workspace_id", "created_at"),
        Index("ix_citation_llm_usage_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    op: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

Note: `workspace_id` does **not** use `index=True` — its index is the composite `ix_citation_llm_usage_ws_created` declared above, matching the migration (no stray `ix_citation_llm_usage_workspace_id`).

- [ ] **Step 5: Run the model tests**

Run: `uv run pytest tests/proposal_assist/references/test_models.py -v -k "metadata_columns or citekey_unique or citation_llm_usage"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/db/models.py tests/proposal_assist/references/test_models.py
git commit -m "feat(citations): Reference metadata columns + CitationLlmUsage model (checks+indexes)"
```

---

### Task 3: Capture `original_sha256` at ingest

**Files:**
- Modify: `src/call_manager/proposal_assist/references/ingest.py`
- Test: `tests/proposal_assist/references/test_ingest.py` (append)

- [ ] **Step 1: Write the failing test**

```python
def test_ingest_records_original_sha256(pg_session_factory, tmp_path):
    import hashlib
    from sqlalchemy import select
    from call_manager.proposal_assist.references.embed import FakeEmbedder
    from call_manager.proposal_assist.references.ingest import ingest_bytes
    from call_manager.proposal_assist.references.storage import ReferenceStorage
    from call_manager.db.models import Reference
    from tests.proposal_assist.conftest import make_user, make_workspace

    storage = ReferenceStorage(root=tmp_path)
    data = b"# Title\n\nBody.\n"

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s))
        s.commit()
        wsid = ws.id

    with pg_session_factory() as s:
        result = ingest_bytes(
            session_factory=pg_session_factory, session=s, storage=storage,
            embedder=FakeEmbedder(), workspace_id=wsid, kind="paper",
            source_type="uploaded", source_uri=None, filename="p.md",
            mime_type="text/markdown", data=data, uploaded_by=None,
        )
        s.commit()

    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == result.reference_id))
    assert ref.original_sha256 == hashlib.sha256(data).hexdigest()
    assert ref.metadata_status == "pending"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/proposal_assist/references/test_ingest.py::test_ingest_records_original_sha256 -v`
Expected: FAIL (`assert None == '<hash>'`).

- [ ] **Step 3: Set `original_sha256` in `ingest_bytes`**

Add `import hashlib` at the top of `ingest.py`. In `ingest_bytes`, before `ref = Reference(...)`:

```python
    original_sha256 = hashlib.sha256(data).hexdigest()
```

Add the keyword to the `Reference(...)` call (next to `bytes_total=len(data),`):

```python
            original_sha256=original_sha256,
```

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/proposal_assist/references/test_ingest.py::test_ingest_records_original_sha256 -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/references/ingest.py tests/proposal_assist/references/test_ingest.py
git commit -m "feat(citations): hash original reference bytes (original_sha256) at ingest"
```

---

### Task 4: Citation-LLM settings

**Files:**
- Modify: `src/call_manager/config.py`

- [ ] **Step 1: Add the settings to `Settings`**

Add to `class Settings` (near the other `*_daily_token_cap_*` fields). Provider defaults to `ollama` so uploaded reference text stays **on-prem by default**:

```python
    citation_llm_provider: str = Field(default="ollama", alias="CITATION_LLM_PROVIDER")
    citation_llm_model: str = Field(default="", alias="CITATION_LLM_MODEL")
    citation_llm_timeout_seconds: float = Field(
        default=60.0, alias="CITATION_LLM_TIMEOUT_SECONDS"
    )
    citation_llm_max_output_tokens: int = Field(
        default=1024, alias="CITATION_LLM_MAX_OUTPUT_TOKENS"
    )
    citation_llm_daily_token_cap_input: int = Field(
        default=200_000, alias="CITATION_LLM_DAILY_TOKEN_CAP_INPUT"
    )
    citation_llm_daily_token_cap_output: int = Field(
        default=50_000, alias="CITATION_LLM_DAILY_TOKEN_CAP_OUTPUT"
    )
    global_citation_llm_daily_token_cap_input: int = Field(
        default=2_000_000, alias="GLOBAL_CITATION_LLM_DAILY_TOKEN_CAP_INPUT"
    )
    global_citation_llm_daily_token_cap_output: int = Field(
        default=500_000, alias="GLOBAL_CITATION_LLM_DAILY_TOKEN_CAP_OUTPUT"
    )
```

- [ ] **Step 2: Commit**

```bash
git add src/call_manager/config.py
git commit -m "feat(citations): explicit citation-LLM provider/model/timeout/cap settings (local default)"
```

---

### Task 5: `StructuredLLM` client (cache-token accounting) + factory

**Files:**
- Create: `src/call_manager/proposal_assist/citations/__init__.py` (empty)
- Create: `src/call_manager/proposal_assist/citations/llm.py`
- Test: `tests/proposal_assist/citations/__init__.py` (empty), `tests/proposal_assist/citations/test_llm.py`

- [ ] **Step 1: Write the failing test (incl. cache-token regression)**

Create `tests/proposal_assist/citations/__init__.py` (empty) and `tests/proposal_assist/citations/test_llm.py`:

```python
import pytest

from call_manager.proposal_assist.citations.llm import (
    CitationLLMConfigError,
    StructuredLLM,
    build_citation_llm,
)


class _FakeClient:
    def __init__(self, content, usage=None):
        self._content = content
        self._usage = usage or {"prompt_tokens": 10, "completion_tokens": 4}
        self.calls = []

    def chat(self, *, messages, response_format=None):
        self.calls.append(messages)
        return {"model": "fake-model",
                "choices": [{"message": {"role": "assistant", "content": self._content}}],
                "usage": self._usage}


def test_complete_json_parses_and_reports_usage():
    client = _FakeClient('{"title": "X"}')
    result = StructuredLLM(client, "fake-model").complete_json(system="s", user="u")
    assert result.data == {"title": "X"}
    assert result.usage.tokens_in == 10
    assert result.usage.tokens_out == 4
    assert client.calls[0][0]["role"] == "system" and client.calls[0][1]["role"] == "user"


def test_complete_json_sums_anthropic_cache_tokens():
    # tokens_in must include cache-read + cache-creation, not just prompt_tokens.
    client = _FakeClient('{"ok": true}', usage={
        "prompt_tokens": 10, "completion_tokens": 4,
        "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50,
    })
    result = StructuredLLM(client, "fake-model").complete_json(system="s", user="u")
    assert result.usage.tokens_in == 160  # 10 + 100 + 50


def test_build_citation_llm_anthropic_requires_key():
    from call_manager.config import Settings
    import os
    for k, v in {"DATABASE_URL": "postgresql://x", "SESSION_SECRET": "test-secret-at-least-16-chars",
                 "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://localhost:8000"}.items():
        os.environ.setdefault(k, v)
    s = Settings()
    object.__setattr__(s, "citation_llm_provider", "anthropic")
    object.__setattr__(s, "anthropic_api_key", None)
    with pytest.raises(CitationLLMConfigError):
        build_citation_llm(s)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/proposal_assist/citations/test_llm.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement the client + factory**

Create `src/call_manager/proposal_assist/citations/__init__.py` (empty). Create `src/call_manager/proposal_assist/citations/llm.py`:

```python
"""Structured-JSON LLM wrapper for the citation subsystem (internal).

Built on the ranking clients (AnthropicClient / OllamaClient), NOT the
streaming drafting SDK. Public callers go through CitationLLMService, never
this class directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from call_manager.config import Settings


class CitationLLMConfigError(RuntimeError):
    """Misconfigured citation LLM (e.g. provider=anthropic with no API key)."""


@dataclass
class LLMUsage:
    tokens_in: int
    tokens_out: int


@dataclass
class LLMResult:
    data: dict
    usage: LLMUsage
    model: str


class StructuredLLM:
    def __init__(self, client, model: str) -> None:
        self._client = client
        self.model = model

    def complete_json(self, *, system: str, user: str) -> LLMResult:
        resp = self._client.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        )
        content = resp["choices"][0]["message"]["content"]
        usage = resp.get("usage") or {}
        # Anthropic reports cache-read / cache-creation tokens SEPARATELY from
        # prompt_tokens; true input usage is their sum. Ollama returns neither
        # (the .get(...,0) defaults make this safe for both).
        tokens_in = (
            int(usage.get("prompt_tokens", 0))
            + int(usage.get("cache_read_input_tokens", 0))
            + int(usage.get("cache_creation_input_tokens", 0))
        )
        return LLMResult(
            data=json.loads(content),
            usage=LLMUsage(tokens_in=tokens_in, tokens_out=int(usage.get("completion_tokens", 0))),
            model=resp.get("model", self.model),
        )


def build_citation_llm(settings: Settings) -> StructuredLLM:
    provider = settings.citation_llm_provider.lower()
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise CitationLLMConfigError(
                "CITATION_LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is unset"
            )
        from call_manager.ranking.anthropic_client import AnthropicClient

        model = settings.citation_llm_model or settings.remote_rank_model
        return StructuredLLM(
            AnthropicClient(
                api_key=settings.anthropic_api_key,
                model=model,
                max_tokens=settings.citation_llm_max_output_tokens,
                timeout=settings.citation_llm_timeout_seconds,
            ),
            model,
        )
    if provider == "ollama":
        from call_manager.ranking.ollama_client import OllamaClient

        model = settings.citation_llm_model or settings.gemma_model
        return StructuredLLM(
            OllamaClient(
                base_url=settings.ollama_url,
                model=model,
                timeout=settings.citation_llm_timeout_seconds,
                num_predict=settings.citation_llm_max_output_tokens,
            ),
            model,
        )
    raise CitationLLMConfigError(f"unknown CITATION_LLM_PROVIDER: {provider!r}")
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/proposal_assist/citations/test_llm.py -v`
Expected: PASS (all three, including the cache-token sum).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/citations/__init__.py src/call_manager/proposal_assist/citations/llm.py tests/proposal_assist/citations/__init__.py tests/proposal_assist/citations/test_llm.py
git commit -m "feat(citations): StructuredLLM client (cache-token accounting) + provider factory"
```

---

### Task 6: `CitationLLMService` — the single enforced call path (caps + ledger)

**Files:**
- Create: `src/call_manager/proposal_assist/citations/usage.py` (internal helpers)
- Create: `src/call_manager/proposal_assist/citations/service.py`
- Test: `tests/proposal_assist/citations/test_service.py`

- [ ] **Step 1: Write the failing service tests**

Create `tests/proposal_assist/citations/test_service.py`:

```python
from call_manager.db.models import CitationLlmUsage
from call_manager.proposal_assist.citations.llm import LLMResult, LLMUsage
from call_manager.proposal_assist.citations.service import CitationLLMService
from tests.proposal_assist.conftest import make_user, make_workspace


def _settings(**over):
    import os
    for k, v in {"DATABASE_URL": "postgresql://x", "SESSION_SECRET": "test-secret-at-least-16-chars",
                 "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://localhost:8000"}.items():
        os.environ.setdefault(k, v)
    from call_manager.config import Settings
    s = Settings()
    for k, v in over.items():
        object.__setattr__(s, k, v)
    return s


def _ws(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s))
        s.commit()
        return ws.id


class _OkLLM:
    def __init__(self):
        self.called = 0
    def complete_json(self, *, system, user):
        self.called += 1
        return LLMResult(data={"title": "T"}, usage=LLMUsage(tokens_in=30, tokens_out=5), model="m")


class _BoomLLM:
    def __init__(self, exc):
        self._exc = exc
        self.called = 0
    def complete_json(self, *, system, user):
        self.called += 1
        raise self._exc


def _rows(pg_session_factory, wsid):
    from sqlalchemy import select
    with pg_session_factory() as s:
        return list(s.scalars(select(CitationLlmUsage).where(CitationLlmUsage.workspace_id == wsid)))


def test_success_records_ok(pg_session_factory):
    wsid = _ws(pg_session_factory)
    svc = CitationLLMService(session_factory=pg_session_factory, llm=_OkLLM(), settings=_settings())
    res = svc.complete_json(workspace_id=wsid, op="metadata_extract", system="s", user="u")
    assert res.cap_exceeded is False and res.data == {"title": "T"}
    rows = _rows(pg_session_factory, wsid)
    assert len(rows) == 1 and rows[0].status == "ok" and rows[0].tokens_in == 30


def test_failure_records_failed_and_raises(pg_session_factory):
    import pytest
    wsid = _ws(pg_session_factory)
    svc = CitationLLMService(session_factory=pg_session_factory, llm=_BoomLLM(ValueError("x")),
                             settings=_settings())
    with pytest.raises(ValueError):
        svc.complete_json(workspace_id=wsid, op="metadata_extract", system="s", user="u")
    rows = _rows(pg_session_factory, wsid)
    assert len(rows) == 1 and rows[0].status == "failed"


def test_timeout_records_timeout_and_raises(pg_session_factory):
    import pytest
    wsid = _ws(pg_session_factory)
    svc = CitationLLMService(session_factory=pg_session_factory, llm=_BoomLLM(TimeoutError()),
                             settings=_settings())
    with pytest.raises(TimeoutError):
        svc.complete_json(workspace_id=wsid, op="metadata_extract", system="s", user="u")
    rows = _rows(pg_session_factory, wsid)
    assert len(rows) == 1 and rows[0].status == "timeout"


def test_cap_exceeded_returns_typed_result_without_calling(pg_session_factory):
    wsid = _ws(pg_session_factory)
    llm = _OkLLM()
    svc = CitationLLMService(session_factory=pg_session_factory, llm=llm,
                             settings=_settings(citation_llm_daily_token_cap_input=20))
    # Pre-load usage to exceed the workspace cap.
    with pg_session_factory() as s:
        s.add(CitationLlmUsage(workspace_id=wsid, op="metadata_extract", status="ok",
                               tokens_in=25, tokens_out=0))
        s.commit()
    res = svc.complete_json(workspace_id=wsid, op="metadata_extract", system="s", user="u")
    assert res.cap_exceeded is True and res.cap_scope == "workspace"
    assert res.data is None
    assert llm.called == 0  # client NOT invoked when capped
    # No new ledger row was written for the blocked call.
    assert len(_rows(pg_session_factory, wsid)) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/proposal_assist/citations/test_service.py -v`
Expected: FAIL (`ModuleNotFoundError: ...citations.service` / `...usage`).

- [ ] **Step 3: Implement the usage helpers**

Create `src/call_manager/proposal_assist/citations/usage.py`:

```python
"""citation_llm_usage ledger writes + daily-cap checks (internal to the
citation subsystem; call via CitationLLMService). Mirrors turns/caps.py's
Stockholm-day window but a SEPARATE ledger so audit/extraction spend never
silently rides on the drafting turn budget."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from call_manager.config import Settings
from call_manager.db.models import CitationLlmUsage

_STOCKHOLM = ZoneInfo("Europe/Stockholm")
_UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class CapStatus:
    exceeded: bool
    scope: str | None  # "workspace" | "global" | None


def _stockholm_day_start_utc() -> datetime:
    now_se = datetime.now(_STOCKHOLM)
    midnight = now_se.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(_UTC)


def record_usage(session, *, workspace_id, op, status, tokens_in=0, tokens_out=0, model=None):
    """Append one usage row. Caller commits."""
    session.add(CitationLlmUsage(
        workspace_id=workspace_id, op=op, status=status,
        tokens_in=tokens_in, tokens_out=tokens_out, model=model,
    ))


def _sum_today(session: Session, *, workspace_id: uuid.UUID | None) -> tuple[int, int]:
    stmt = select(
        func.coalesce(func.sum(CitationLlmUsage.tokens_in), 0),
        func.coalesce(func.sum(CitationLlmUsage.tokens_out), 0),
    ).where(CitationLlmUsage.created_at >= _stockholm_day_start_utc())
    if workspace_id is not None:
        stmt = stmt.where(CitationLlmUsage.workspace_id == workspace_id)
    row = session.execute(stmt).one()
    return int(row[0]), int(row[1])


def check_citation_caps(session, workspace_id, *, settings: Settings) -> CapStatus:
    """Read-only; `>=` hard ceiling (matches turns/caps.py). Workspace first."""
    ws_in, ws_out = _sum_today(session, workspace_id=workspace_id)
    if (ws_in >= settings.citation_llm_daily_token_cap_input
            or ws_out >= settings.citation_llm_daily_token_cap_output):
        return CapStatus(True, "workspace")
    g_in, g_out = _sum_today(session, workspace_id=None)
    if (g_in >= settings.global_citation_llm_daily_token_cap_input
            or g_out >= settings.global_citation_llm_daily_token_cap_output):
        return CapStatus(True, "global")
    return CapStatus(False, None)
```

- [ ] **Step 4: Implement the service**

Create `src/call_manager/proposal_assist/citations/service.py`:

```python
"""The ONLY supported citation-LLM call path.

Every call is cap-checked before and ledger-recorded after (ok/failed/timeout),
so no caller can spend un-accounted tokens or skip the cap. Returns a typed
cap-exceeded result WITHOUT invoking the client when over budget.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from call_manager.config import Settings
from call_manager.proposal_assist.citations.llm import LLMUsage, StructuredLLM
from call_manager.proposal_assist.citations.usage import check_citation_caps, record_usage


@dataclass
class CitationLLMResult:
    data: dict | None
    usage: LLMUsage | None
    model: str | None
    cap_exceeded: bool = False
    cap_scope: str | None = None


class CitationLLMService:
    def __init__(self, *, session_factory, llm: StructuredLLM, settings: Settings) -> None:
        self._session_factory = session_factory
        self._llm = llm
        self._settings = settings

    def complete_json(
        self, *, workspace_id: uuid.UUID, op: str, system: str, user: str
    ) -> CitationLLMResult:
        with self._session_factory() as s:
            cap = check_citation_caps(s, workspace_id, settings=self._settings)
        if cap.exceeded:
            return CitationLLMResult(
                data=None, usage=None, model=None, cap_exceeded=True, cap_scope=cap.scope
            )

        try:
            result = self._llm.complete_json(system=system, user=user)
        except TimeoutError:
            self._record(workspace_id, op, "timeout", 0, 0, None)
            raise
        except Exception:
            self._record(workspace_id, op, "failed", 0, 0, None)
            raise

        self._record(
            workspace_id, op, "ok", result.usage.tokens_in, result.usage.tokens_out, result.model
        )
        return CitationLLMResult(data=result.data, usage=result.usage, model=result.model)

    def _record(self, workspace_id, op, status, tokens_in, tokens_out, model) -> None:
        with self._session_factory() as s:
            record_usage(
                s, workspace_id=workspace_id, op=op, status=status,
                tokens_in=tokens_in, tokens_out=tokens_out, model=model,
            )
            s.commit()
```

> Note: real ranking clients surface timeouts as `LLMError`, not `TimeoutError`, so in production most failures record `failed`; the `timeout` branch covers callers/clients that raise a `TimeoutError` (and keeps the contract explicit). Slice 3's audit service can refine timeout classification.

- [ ] **Step 5: Run the service tests**

Run: `uv run pytest tests/proposal_assist/citations/test_service.py -v`
Expected: PASS (ok/failed/timeout recorded; capped returns typed result, client not called, no row).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/citations/usage.py src/call_manager/proposal_assist/citations/service.py tests/proposal_assist/citations/test_service.py
git commit -m "feat(citations): CitationLLMService — enforced caps + usage ledger on every call"
```

---

### Task 7: Full-suite green + lint

- [ ] **Step 1: Lint + format**

Run: `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/`
Expected: "All checks passed!" / "… already formatted". Fix with `uv run ruff format <files>` if needed and re-stage.

- [ ] **Step 2: Full suite**

Run: `uv run pytest -q`
Expected: all pass (prior count + new tests; 1 pre-existing skip).

- [ ] **Step 3: Commit any fixups**

```bash
git add -A && git commit -m "chore(citations): ruff fixups for metadata foundation" || echo "nothing to format"
```

---

## Deferred (explicit responsibility hand-off)

- **Lazy `original_sha256` backfill for pre-existing references** (Codex M5): references created before 0036 have `original_sha256=NULL`, so they can't satisfy the future `source_content_hash` freeze guarantee. **Slice 1B** owns this: when a reference is first cited (or when its metadata is first extracted), if `original_sha256 IS NULL`, read the stored original bytes, hash them, and persist before writing any ledger row. No bulk backfill — lazy, on first use.
- **Wiring `CitationLLMService` into extraction** is Slice 1B; this plan only builds and unit-tests the service.

---

## Self-Review

**Spec coverage (Slice 1A):** CSL columns + `citekey` + `metadata_status` (5-state CHECK) + `metadata_source` + `original_sha256` (hex CHECK) + per-workspace unique citekey → Tasks 1–2. `citation_llm_usage` (nonneg CHECK, composite + standalone `created_at` indexes) → Tasks 1–2. `original_sha256` at ingest → Task 3. Explicit citation-LLM settings (local-default provider) → Task 4. `StructuredLLM` with cache-token accounting → Task 5. `CitationLLMService` as the single enforced caps+ledger path → Task 6. Deferred items explicitly assigned.

**Placeholder scan:** every step has complete code; no TBD/TODO.

**Type consistency:** `StructuredLLM.complete_json -> LLMResult{data, usage:LLMUsage{tokens_in,tokens_out}, model}`; `CitationLLMService.complete_json -> CitationLLMResult{data, usage, model, cap_exceeded, cap_scope}`; `check_citation_caps -> CapStatus{exceeded, scope}`; `record_usage(session, *, workspace_id, op, status, tokens_in, tokens_out, model)`. `CitationLlmUsage` columns (`op,status,tokens_in,tokens_out,model,created_at`) and index names (`ix_citation_llm_usage_ws_created`, `ix_citation_llm_usage_created`) are identical across migration (Task 1) and model (Task 2). `metadata_status` value set identical in migration and model CHECKs.
