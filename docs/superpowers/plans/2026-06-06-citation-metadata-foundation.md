# Citation Metadata Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lay the data + LLM-infrastructure foundation for citation provenance — CSL bibliographic metadata columns on `references`, original-bytes hashing at ingest, a shared structured-completion LLM client, and a per-workspace/global usage ledger with caps.

**Architecture:** This is **Slice 1A** of the citation-provenance spec ([`docs/superpowers/specs/2026-06-06-citation-provenance-evidence-ledger-design.md`](../specs/2026-06-06-citation-provenance-evidence-ledger-design.md)). It builds the pieces every later slice depends on but adds no user-visible behavior yet: schema (migration 0036), the `original_sha256` freeze anchor, a `StructuredLLM` wrapper over the existing ranking clients (reused by metadata extraction now and the audit service later), and the `citation_llm_usage` ledger + cap helper. Slice 1B (extended fetchers, heuristic+LLM extraction wired into ingest, citekey generation, owner approval UI) and Slices 2–4 follow in separate plans.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x + Alembic, PostgreSQL 16 (JSONB), pytest with the session-scoped pgvector testcontainer (`pg_session_factory` / `migrated_engine` fixtures from `tests/conftest.py`). LLM clients: `call_manager.ranking.anthropic_client.AnthropicClient`, `call_manager.ranking.ollama_client.OllamaClient` (both already return an OpenAI-shaped envelope with a `usage` block).

---

## File Structure

- `alembic/versions/0036_citation_metadata_foundation.py` — **new** migration (references columns + `citation_llm_usage` table). Down-revision `0035`.
- `src/call_manager/db/models.py` — **modify**: add metadata columns to `Reference`; add `CitationLlmUsage` model.
- `src/call_manager/proposal_assist/references/ingest.py` — **modify**: compute + store `original_sha256` at ingest.
- `src/call_manager/config.py` — **modify**: add citation-LLM daily cap settings.
- `src/call_manager/proposal_assist/citations/__init__.py` — **new** (empty package marker).
- `src/call_manager/proposal_assist/citations/llm.py` — **new**: `StructuredLLM` wrapper + `build_citation_llm(settings)` factory.
- `src/call_manager/proposal_assist/citations/usage.py` — **new**: `record_usage(...)` + `check_citation_caps(...)`.
- Tests: `tests/test_migration_0036_citation_metadata.py`, `tests/proposal_assist/references/test_ingest.py` (append), `tests/proposal_assist/citations/test_llm.py` (new), `tests/proposal_assist/citations/test_usage.py` (new), plus a `Reference`/`CitationLlmUsage` model test in `tests/proposal_assist/references/test_models.py` (append).

---

### Task 1: Migration 0036 — references metadata columns + `citation_llm_usage`

**Files:**
- Create: `alembic/versions/0036_citation_metadata_foundation.py`
- Test: `tests/test_migration_0036_citation_metadata.py`

- [ ] **Step 1: Write the migration**

Create `alembic/versions/0036_citation_metadata_foundation.py`:

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
    # citekey is unique per workspace, only when assigned.
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
    )
    op.create_index(
        "ix_citation_llm_usage_ws_created",
        "citation_llm_usage",
        ["workspace_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_citation_llm_usage_ws_created", table_name="citation_llm_usage")
    op.drop_table("citation_llm_usage")
    op.drop_index("uq_references_workspace_citekey", table_name="references")
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

- [ ] **Step 2: Write the round-trip migration test**

Create `tests/test_migration_0036_citation_metadata.py` (mirrors the 0035 test; `pg_url` is the SHARED container so the test MUST leave the schema at head):

```python
"""Round-trip checks for migration 0036 (citation metadata foundation).

pg_url is the SHARED session container; this test downgrades then upgrades,
so it MUST leave the schema at head for later tests.
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
        # At head: new columns + table present.
        with engine.connect() as conn:
            assert {"csl_json", "csl_json_extracted", "citekey",
                    "metadata_status", "metadata_source", "original_sha256"} <= _columns(conn, "references")
            assert "citation_llm_usage" in inspect(conn).get_table_names()

        # Downgrade to 0035: everything 0036 added is gone.
        command.downgrade(_cfg(pg_url), "0035")
        with engine.connect() as conn:
            assert "original_sha256" not in _columns(conn, "references")
            assert "citation_llm_usage" not in inspect(conn).get_table_names()

        # Upgrade back to head (leave schema as later tests expect).
        command.upgrade(_cfg(pg_url), "head")
        with engine.connect() as conn:
            cols = _columns(conn, "references")
            assert "original_sha256" in cols and "csl_json" in cols
            assert "citation_llm_usage" in inspect(conn).get_table_names()
    finally:
        engine.dispose()
```

(The `metadata_status` CHECK constraint is exercised at the ORM layer in Task 2.)

- [ ] **Step 3: Run the migration test**

Run: `uv run pytest tests/test_migration_0036_citation_metadata.py -v`
Expected: PASS (columns + table appear at head, vanish at 0035, return at head).

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/0036_citation_metadata_foundation.py tests/test_migration_0036_citation_metadata.py
git commit -m "feat(citations): migration 0036 — references metadata columns + citation_llm_usage"
```

---

### Task 2: Models — `Reference` metadata columns + `CitationLlmUsage`

**Files:**
- Modify: `src/call_manager/db/models.py`
- Test: `tests/proposal_assist/references/test_models.py` (append)

- [ ] **Step 1: Write the failing model test**

Append to `tests/proposal_assist/references/test_models.py`:

```python
def test_reference_metadata_columns_round_trip(pg_session_factory):
    import uuid
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from call_manager.db.models import Reference
    from tests.proposal_assist.conftest import make_user, make_workspace

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
            original_sha256="abc123",
        ))
        s.commit()

    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.id == rid))
        assert ref.csl_json == {"title": "T"}
        assert ref.citekey == "smith2021"
        assert ref.metadata_status == "approved"
        assert ref.original_sha256 == "abc123"

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


def test_citation_llm_usage_insert(pg_session_factory):
    import uuid
    from sqlalchemy import select
    from call_manager.db.models import CitationLlmUsage
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s))
        s.commit()
        wsid = ws.id

    with pg_session_factory() as s:
        s.add(CitationLlmUsage(
            workspace_id=wsid, op="metadata_extract", status="ok",
            tokens_in=100, tokens_out=20, model="gemma",
        ))
        s.commit()

    with pg_session_factory() as s:
        row = s.scalar(select(CitationLlmUsage).where(CitationLlmUsage.workspace_id == wsid))
        assert row.op == "metadata_extract" and row.tokens_in == 100
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/proposal_assist/references/test_models.py -v -k "metadata_columns or citation_llm_usage"`
Expected: FAIL (`TypeError: 'csl_json' is an invalid keyword argument` / `ImportError: cannot import name 'CitationLlmUsage'`).

- [ ] **Step 3: Add the columns to `Reference`**

In `src/call_manager/db/models.py`, inside `class Reference(Base)`: add the new columns after `created_at` (any position among the mapped columns works) and add the CHECK to `__table_args__`:

```python
    csl_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    csl_json_extracted: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    citekey: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    metadata_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
```

Add to `Reference.__table_args__` (it is a tuple of constraints):

```python
        CheckConstraint(
            "metadata_status IN ('pending','extracted','verified_external','approved','failed')",
            name="ck_references_metadata_status",
        ),
```

- [ ] **Step 4: Add the `CitationLlmUsage` model**

In `src/call_manager/db/models.py`, after `class ReferenceChunk(Base)`:

```python
class CitationLlmUsage(Base):
    """Append-only ledger of every LLM call the citation subsystem makes
    (metadata extraction now; validate/list audit later). Basis for the
    per-workspace + global daily caps. See the citation-provenance spec."""

    __tablename__ = "citation_llm_usage"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ok','failed','timeout')", name="ck_citation_llm_usage_status"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
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

(`BigInteger`, `Integer`, `Text`, `DateTime`, `ForeignKey`, `CheckConstraint`, `func`, `UUID`, `Mapped`, `mapped_column`, `uuid`, `datetime` are all already imported in `models.py`.)

- [ ] **Step 5: Run the model tests**

Run: `uv run pytest tests/proposal_assist/references/test_models.py -v -k "metadata_columns or citation_llm_usage"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/db/models.py tests/proposal_assist/references/test_models.py
git commit -m "feat(citations): Reference metadata columns + CitationLlmUsage model"
```

---

### Task 3: Capture `original_sha256` at ingest

**Files:**
- Modify: `src/call_manager/proposal_assist/references/ingest.py`
- Test: `tests/proposal_assist/references/test_ingest.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/references/test_ingest.py`:

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

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/proposal_assist/references/test_ingest.py::test_ingest_records_original_sha256 -v`
Expected: FAIL (`assert None == '<hash>'` — column exists but is never set).

- [ ] **Step 3: Set `original_sha256` in `ingest_bytes`**

In `src/call_manager/proposal_assist/references/ingest.py`: add `import hashlib` to the imports at the top, then in `ingest_bytes`, before the `ref = Reference(...)` construction add:

```python
    original_sha256 = hashlib.sha256(data).hexdigest()
```

and pass it into the `Reference(...)` call (add the keyword alongside `bytes_total=len(data),`):

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

### Task 4: Shared structured-completion LLM client

**Files:**
- Create: `src/call_manager/proposal_assist/citations/__init__.py` (empty)
- Create: `src/call_manager/proposal_assist/citations/llm.py`
- Test: `tests/proposal_assist/citations/__init__.py` (empty), `tests/proposal_assist/citations/test_llm.py`

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/citations/__init__.py` (empty) and `tests/proposal_assist/citations/test_llm.py`:

```python
from call_manager.proposal_assist.citations.llm import StructuredLLM


class _FakeClient:
    """Mimics the ranking clients' OpenAI-shaped envelope (incl. usage)."""

    def __init__(self, content, usage=None):
        self._content = content
        self._usage = usage or {"prompt_tokens": 10, "completion_tokens": 4}
        self.calls = []

    def chat(self, *, messages, response_format=None):
        self.calls.append(messages)
        return {
            "model": "fake-model",
            "choices": [{"message": {"role": "assistant", "content": self._content}}],
            "usage": self._usage,
        }


def test_complete_json_parses_and_reports_usage():
    client = _FakeClient('{"title": "X", "year": 2021}')
    llm = StructuredLLM(client, "fake-model")
    result = llm.complete_json(system="extract", user="paper text")
    assert result.data == {"title": "X", "year": 2021}
    assert result.usage.tokens_in == 10
    assert result.usage.tokens_out == 4
    assert result.model == "fake-model"
    # system + user passed through in OpenAI shape.
    assert client.calls[0][0]["role"] == "system"
    assert client.calls[0][1]["role"] == "user"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/proposal_assist/citations/test_llm.py -v`
Expected: FAIL (`ModuleNotFoundError: ...citations.llm`).

- [ ] **Step 3: Implement the client**

Create `src/call_manager/proposal_assist/citations/__init__.py` (empty file). Create `src/call_manager/proposal_assist/citations/llm.py`:

```python
"""Structured-JSON LLM wrapper for the citation subsystem.

Deliberately built on the ranking clients (AnthropicClient / OllamaClient),
NOT the streaming drafting SDK: metadata extraction and (later) the citation
audit run as discrete prompt-in/JSON-out calls whose token usage must be
accounted in the citation_llm_usage ledger. Both ranking clients already
return an OpenAI-shaped envelope with a `usage` block.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from call_manager.config import Settings


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
        # `client` is any object exposing
        # `.chat(messages=[{role,content}, ...]) -> {choices:[{message:{content}}], usage:{...}, model}`.
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
        return LLMResult(
            data=json.loads(content),
            usage=LLMUsage(
                tokens_in=int(usage.get("prompt_tokens", 0)),
                tokens_out=int(usage.get("completion_tokens", 0)),
            ),
            model=resp.get("model", self.model),
        )


def build_citation_llm(settings: Settings) -> StructuredLLM:
    """Anthropic when ANTHROPIC_API_KEY is set, else local Ollama/Gemma."""
    if settings.anthropic_api_key:
        from call_manager.ranking.anthropic_client import AnthropicClient

        return StructuredLLM(
            AnthropicClient(api_key=settings.anthropic_api_key, model=settings.remote_rank_model),
            settings.remote_rank_model,
        )
    from call_manager.ranking.ollama_client import OllamaClient

    return StructuredLLM(
        OllamaClient(base_url=settings.ollama_url, model=settings.gemma_model),
        settings.gemma_model,
    )
```

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/proposal_assist/citations/test_llm.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/citations/__init__.py src/call_manager/proposal_assist/citations/llm.py tests/proposal_assist/citations/__init__.py tests/proposal_assist/citations/test_llm.py
git commit -m "feat(citations): shared StructuredLLM client over ranking clients"
```

---

### Task 5: Config caps + usage ledger & cap check

**Files:**
- Modify: `src/call_manager/config.py`
- Create: `src/call_manager/proposal_assist/citations/usage.py`
- Test: `tests/proposal_assist/citations/test_usage.py`

- [ ] **Step 1: Add the cap settings to `Settings`**

In `src/call_manager/config.py`, add these fields to `class Settings` (next to the other `*_daily_token_cap_*` fields):

```python
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

- [ ] **Step 2: Write the failing test**

Create `tests/proposal_assist/citations/test_usage.py`:

```python
from call_manager.proposal_assist.citations.usage import (
    CapStatus,
    check_citation_caps,
    record_usage,
)


def _settings(**over):
    import os
    os.environ.setdefault("DATABASE_URL", "postgresql://x")
    os.environ.setdefault("SESSION_SECRET", "test-secret-at-least-16-chars")
    os.environ.setdefault("ALLOWED_EMAIL_DOMAINS", "ki.se")
    os.environ.setdefault("APP_BASE_URL", "http://localhost:8000")
    from call_manager.config import Settings
    s = Settings()
    for k, v in over.items():
        object.__setattr__(s, k, v)
    return s


def _ws(pg_session_factory):
    from tests.proposal_assist.conftest import make_user, make_workspace
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s))
        s.commit()
        return ws.id


def test_record_usage_writes_row(pg_session_factory):
    from sqlalchemy import select
    from call_manager.db.models import CitationLlmUsage
    wsid = _ws(pg_session_factory)
    with pg_session_factory() as s:
        record_usage(s, workspace_id=wsid, op="metadata_extract", status="ok",
                     tokens_in=50, tokens_out=10, model="gemma")
        s.commit()
    with pg_session_factory() as s:
        rows = list(s.scalars(select(CitationLlmUsage).where(CitationLlmUsage.workspace_id == wsid)))
    assert len(rows) == 1 and rows[0].status == "ok"


def test_workspace_cap_trips_at_threshold(pg_session_factory):
    wsid = _ws(pg_session_factory)
    settings = _settings(citation_llm_daily_token_cap_input=100,
                         global_citation_llm_daily_token_cap_input=10_000_000)
    with pg_session_factory() as s:
        assert check_citation_caps(s, wsid, settings=settings) == CapStatus(False, None)
        record_usage(s, workspace_id=wsid, op="metadata_extract", status="ok",
                     tokens_in=100, tokens_out=0)
        s.commit()
    with pg_session_factory() as s:
        assert check_citation_caps(s, wsid, settings=settings) == CapStatus(True, "workspace")


def test_global_cap_trips_independently(pg_session_factory):
    wsid = _ws(pg_session_factory)
    settings = _settings(citation_llm_daily_token_cap_input=10_000_000,
                         global_citation_llm_daily_token_cap_input=100)
    with pg_session_factory() as s:
        record_usage(s, workspace_id=wsid, op="validate_citation", status="ok",
                     tokens_in=100, tokens_out=0)
        s.commit()
    with pg_session_factory() as s:
        assert check_citation_caps(s, wsid, settings=settings) == CapStatus(True, "global")
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run pytest tests/proposal_assist/citations/test_usage.py -v`
Expected: FAIL (`ModuleNotFoundError: ...citations.usage`).

- [ ] **Step 4: Implement the usage ledger + cap check**

Create `src/call_manager/proposal_assist/citations/usage.py`:

```python
"""citation_llm_usage ledger writes + daily-cap checks.

Mirrors turns/caps.py's Stockholm-day window, but a SEPARATE ledger so audit /
extraction spend never silently rides on the drafting turn budget.
"""

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


def record_usage(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    op: str,
    status: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    model: str | None = None,
) -> None:
    """Append one usage row. Caller commits."""
    session.add(
        CitationLlmUsage(
            workspace_id=workspace_id,
            op=op,
            status=status,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
        )
    )


def _sum_today(session: Session, *, workspace_id: uuid.UUID | None) -> tuple[int, int]:
    day_start = _stockholm_day_start_utc()
    stmt = select(
        func.coalesce(func.sum(CitationLlmUsage.tokens_in), 0),
        func.coalesce(func.sum(CitationLlmUsage.tokens_out), 0),
    ).where(CitationLlmUsage.created_at >= day_start)
    if workspace_id is not None:
        stmt = stmt.where(CitationLlmUsage.workspace_id == workspace_id)
    row = session.execute(stmt).one()
    return int(row[0]), int(row[1])


def check_citation_caps(
    session: Session, workspace_id: uuid.UUID, *, settings: Settings
) -> CapStatus:
    """Read-only. `>=` (hard ceiling, matches turns/caps.py). Workspace cap
    checked first, then the org-wide cap."""
    ws_in, ws_out = _sum_today(session, workspace_id=workspace_id)
    if (
        ws_in >= settings.citation_llm_daily_token_cap_input
        or ws_out >= settings.citation_llm_daily_token_cap_output
    ):
        return CapStatus(True, "workspace")

    g_in, g_out = _sum_today(session, workspace_id=None)
    if (
        g_in >= settings.global_citation_llm_daily_token_cap_input
        or g_out >= settings.global_citation_llm_daily_token_cap_output
    ):
        return CapStatus(True, "global")

    return CapStatus(False, None)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/proposal_assist/citations/test_usage.py -v`
Expected: PASS (all three).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/config.py src/call_manager/proposal_assist/citations/usage.py tests/proposal_assist/citations/test_usage.py
git commit -m "feat(citations): citation_llm_usage ledger + per-workspace/global daily caps"
```

---

### Task 6: Full-suite green + lint

- [ ] **Step 1: Run lint + format**

Run: `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/`
Expected: "All checks passed!" and "… already formatted". If format flags files, run `uv run ruff format <files>` and re-stage.

- [ ] **Step 2: Run the full suite**

Run: `uv run pytest -q`
Expected: all tests pass (prior count + the new tests; 1 pre-existing skip).

- [ ] **Step 3: Commit any formatting fixups**

```bash
git add -A
git commit -m "chore(citations): ruff format fixups for metadata foundation" || echo "nothing to format"
```

---

## Self-Review

**Spec coverage (Slice 1A scope):**
- CSL columns (`csl_json`, `csl_json_extracted`), `citekey`, `metadata_status` (5-state CHECK), `metadata_source`, `original_sha256`, per-workspace unique citekey → Task 1 + Task 2. ✓
- `citation_llm_usage` ledger table + model → Task 1 + Task 2. ✓
- `original_sha256` captured at ingest (the "source replaced" freeze anchor) → Task 3. ✓
- Shared structured-completion client (reused by extraction now, audit later) → Task 4. ✓
- Per-workspace + global daily caps over the usage ledger → Task 5. ✓
- **Deferred to Slice 1B (not this plan):** fetcher CSL extension, heuristic+LLM extraction wired into ingest, citekey *generation*, owner approval route/UI, `verified_external` transitions. Called out in the header.

**Placeholder scan:** every code/test step contains complete code; the only "if False" line in Task 1 Step 2 is annotated as a deletable no-op (the CHECK is exercised via the ORM in Task 2). No TBD/TODO.

**Type consistency:** `StructuredLLM.complete_json` returns `LLMResult{data, usage:LLMUsage{tokens_in,tokens_out}, model}` — used consistently in Task 4's test. `check_citation_caps(...) -> CapStatus(exceeded, scope)` and `record_usage(session, *, workspace_id, op, status, tokens_in, tokens_out, model)` match Task 5's tests. `CitationLlmUsage` column names (`op`, `status`, `tokens_in`, `tokens_out`, `model`) match across migration (Task 1), model (Task 2), and ledger writes (Task 5). `metadata_status` value set is identical in the migration CHECK and the model CHECK.
