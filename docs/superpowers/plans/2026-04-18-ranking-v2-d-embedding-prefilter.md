# Ranking v2 — Plan D: Embedding Pre-filter Implementation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** [2026-04-18-ranking-v2-d-embedding-prefilter-design.md](../specs/2026-04-18-ranking-v2-d-embedding-prefilter-design.md)
**Depends on:** Plans A + B + C of v2 (all merged to master). v2 schema, scheme-aware queue, `rank_pair_v2`, worker dispatch, scheduler, admin UI all already in place.

**Goal:** Add BGE-M3 embeddings as a 4th rule-based gate in the v2 ranking pipeline so clearly-off-topic `(call × profile)` pairs short-circuit to score=0 without burning an LLM call. Embeddings stored on `calls.embedding`, `profiles.embedding`, and `call_rankings.embed_similarity`.

**Architecture:** BGE-M3 served via the existing Ollama instance (extends `OllamaClient` with an `embed()` method). pgvector stores 1024-dim vectors natively. Embedding refresh is synchronous on ingest + on profile-edit; a daily cron tick backfills NULLs. The new gate slots in as the 4th field on `Gates`, evaluated last (after cheap pure-Python checks). `aggregate.py` gets one new short-circuit branch. `rank_pair_v2` fetches similarity, persists it, and skips the LLM on FAIL.

**Tech Stack:** Python 3.12 · `pgvector` (Python package + Postgres extension) · BGE-M3 via Ollama · existing FastAPI / SQLAlchemy 2.x / Alembic / APScheduler / pytest+testcontainers stack (with the testcontainers image switched to `pgvector/pgvector:pg16`)

---

## Design decisions (fix-now; push back if you disagree)

- **One model: BGE-M3.** 1024-dim, multilingual. Pulled via `ollama pull bge-m3`. No SPECTER2/SciNCL ensemble (deferred).
- **Storage: `pgvector vector(1024)`.** Native cosine, future-proofs reranker shortlist. Adds `CREATE EXTENSION vector` migration; testcontainers must use the `pgvector/pgvector:pg16` image.
- **Embedding text:** call = `title + summary + full_text[:8000]`; profile = `description` only (NOT the structured rubric — those are exact-keyword mechanisms).
- **Threshold = 0.30**, configurable via `EMBEDDING_PREGATE_THRESHOLD` env. Defensive default — biased toward keeping borderline calls in the LLM path.
- **Embedding gate runs LAST** among rule-based gates. Cheap timeline/budget/TRL checks fire first; if any FAIL, no embedding lookup happens.
- **`must_include` keyword bypass** applies to the embedding gate the same way it bypasses the timeline gate.
- **Sync refresh** (no queue): embedding compute is ~50ms; not worth queueing infrastructure.
- **Versioning: `embedding_model` columns on both `calls` and `profiles`.** Cheap to add now; expensive to add later when we need to migrate.
  - Daily refresh tick re-embeds rows where `embedding IS NULL` **OR** `embedding_model != settings.embedding_model`.
  - `rank_pair_v2` cosine returns `None` (UNKNOWN gate → falls through to LLM) when `profile.embedding_model != call.embedding_model`. Logged as a warning so an operator knows to run the backfill CLI after a model change.
- **Ordering: embeddings ready BEFORE rankings can be enqueued (Codex H1).** Both ingest and profile-flip paths now serialize as: (1) commit data change → (2) embed (best-effort) → (3) enqueue ranking jobs. This means the v2 worker never picks up a job whose call/profile has no embedding yet, except in the rare case where step (2) failed (then the gate is UNKNOWN and the LLM runs as the safe fallback).
- **`embed_similarity` written to every CallRanking row**, even on clean LLM-completion paths and even on bypasses — for tuning and disagreement detection.
- **Embedding service down → UNKNOWN** (not FAIL). LLM still runs. Same fallback semantics as budget/TRL UNKNOWN.
- **Historical CallRanking rows stay NULL on embed_similarity.** No re-rank; new rankings populate the column.
- **No new ranking_scheme value.** Still `'v2'`; behavior changes, output shape doesn't.
- **Initial backfill is a CLI tool, NOT an Alembic migration (Codex H3).** `python -m call_monitor.embeddings.cli backfill` walks NULL/mismatched rows in batches via Ollama. Required in deploy procedure: `alembic upgrade head` → `embeddings cli backfill` → start app. Daily refresh-missing tick handles drift after that.
- **Always re-embed touched calls (Codex Open-Q2).** Drop the "unchanged call" optimization — `upsert.py` always UPDATEs on conflict, so distinguishing "actually changed" requires a content hash we don't have. Re-embedding every touched call costs ~50 ms × ~10 new calls/hour ≈ negligible.

---

## File structure created or modified by this plan

```
call-monitor/
├── alembic/versions/
│   └── 0007_pgvector_and_embeddings.py            # NEW: extension + 3 columns + 2 NULL indexes + embedding_model columns
├── src/call_monitor/
│   ├── config.py                                  # extend: EMBEDDING_PREGATE_THRESHOLD, EMBEDDING_MODEL
│   ├── db/models.py                               # extend: Call.embedding, Profile.embedding, CallRanking.embed_similarity
│   ├── embeddings/                                # NEW package
│   │   ├── __init__.py
│   │   ├── cli.py                                 # NEW: `python -m call_monitor.embeddings.cli backfill` — historical backfill
│   │   ├── client.py                              # OllamaEmbedClient — POST /api/embed
│   │   ├── refresh.py                             # refresh_call_embedding, refresh_profile_embedding, refresh_missing_embeddings_tick
│   │   └── similarity.py                          # cosine, build_call_text, build_profile_text
│   ├── ingestion/orchestrator.py                  # extend: per-new-call embedding refresh
│   ├── main.py                                    # extend: build OllamaEmbedClient and pass to scheduler
│   ├── ranking/
│   │   ├── criteria.py                            # extend: embedding gate
│   │   ├── aggregate.py                           # extend: embedding short-circuit
│   │   ├── rank_pair.py                           # extend: fetch similarity, threade through, store
│   │   └── worker.py                              # extend: _v2_summary handles "embedding" reason; persist embed_similarity
│   ├── scheduler.py                               # extend: register embeddings:refresh-missing daily 03:30
│   ├── web/
│   │   ├── routes/
│   │   │   ├── admin.py                           # extend: ranking_compare reads embed_similarity into items dict
│   │   │   └── profiles.py                        # extend: profile_create + profile_edit trigger refresh on description change
│   │   └── templates/
│   │       ├── call_detail.html                   # extend: show cosine alongside score
│   │       └── ranking_compare.html               # extend: cosine column for v2
└── tests/
    ├── conftest.py                                # extend: switch to pgvector/pgvector:pg16 image
    ├── test_embeddings_client.py                  # NEW
    ├── test_embeddings_similarity.py              # NEW
    ├── test_embeddings_refresh.py                 # NEW
    ├── test_ranking_criteria.py                   # extend: embedding gate cases
    ├── test_ranking_aggregate.py                  # extend: embedding short-circuit cases
    ├── test_ranking_rank_pair_v2.py               # extend: end-to-end with embedding similarity
    ├── test_ranking_worker_v2.py                  # extend: embed_similarity persisted; "embedding" reason in summary
    ├── test_scheduler_v2_jobs.py                  # extend: embeddings:refresh-missing registered
    ├── test_alembic_v2_round_trip.py              # extend: 0007 columns present after head
    ├── test_embeddings_cli.py                     # NEW: backfill CLI — embeds NULL/mismatched, exits 0
    ├── test_ingestion_orchestrator_embeds.py      # NEW
    ├── test_web_profiles.py                       # extend: profile create + edit trigger embedding refresh
    └── test_web_admin_compare.py                  # extend: cosine column rendered
```

---

## Task 1: Schema migration + pgvector + testcontainers switch

**Files:**
- Create: `alembic/versions/0007_pgvector_and_embeddings.py`
- Modify: `src/call_monitor/db/models.py`
- Modify: `tests/conftest.py` (switch testcontainers image)
- Modify: `pyproject.toml` (add `pgvector` Python package)
- Modify: `tests/test_alembic_v2_round_trip.py` (assert columns + indexes exist after head)

This task ONLY adds the extension, columns, indexes, and the `pgvector` Python type wiring. No behavior changes — the columns are nullable and unused until later tasks.

- [ ] **Step 1: Add `pgvector` to dependencies**

In `pyproject.toml` under `[project.dependencies]`, add:

```toml
"pgvector>=0.3.0",
```

Then `uv sync`.

- [ ] **Step 2: Switch testcontainers image to pgvector**

In `tests/conftest.py`, change the line:

```python
with PostgresContainer("postgres:16", driver="psycopg") as pg:
```

to:

```python
# pgvector/pgvector:pg16 is Postgres 16 with the vector extension preinstalled.
# Required for the embedding columns added in migration 0008.
with PostgresContainer("pgvector/pgvector:pg16", driver="psycopg") as pg:
```

This is a one-line change. The first test run after this will pull the new image (~200MB, takes ~1 min once).

- [ ] **Step 3: Write the migration**

Create `alembic/versions/0007_pgvector_and_embeddings.py`:

```python
"""pgvector + embedding columns

Revision ID: 0007_pgvector_and_embeddings
Revises: 0006_ranking_v2_schema
Create Date: 2026-04-18

Adds the vector extension and embedding storage:
- calls.embedding vector(1024) — BGE-M3 of title+summary+full_text[:8000]
- calls.embedding_model text — model identifier for cache invalidation
- profiles.embedding vector(1024) — BGE-M3 of description
- profiles.embedding_model text
- call_rankings.embed_similarity float — cosine; persisted on every rank
- partial NULL-only indexes for the daily backfill tick

Per spec §4. No data backfill here — historical embeddings are populated
by `python -m call_monitor.embeddings.cli backfill` (Task 9), which is
part of the deploy procedure between `alembic upgrade head` and app start.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision = "0007_pgvector_and_embeddings"
down_revision = "0006_ranking_v2_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.add_column("calls", sa.Column("embedding", Vector(1024), nullable=True))
    op.add_column("calls", sa.Column("embedding_model", sa.Text(), nullable=True))

    op.add_column("profiles", sa.Column("embedding", Vector(1024), nullable=True))
    op.add_column("profiles", sa.Column("embedding_model", sa.Text(), nullable=True))

    op.add_column("call_rankings", sa.Column("embed_similarity", sa.Float(), nullable=True))

    # Partial indexes for the daily refresh-missing tick. Both NULL filters
    # are heavily selective until backfill completes, then become very small
    # — partial index keeps them cheap.
    op.create_index(
        "ix_calls_embedding_null", "calls", ["id"],
        postgresql_where=sa.text("embedding IS NULL"),
    )
    op.create_index(
        "ix_profiles_embedding_null", "profiles", ["id"],
        postgresql_where=sa.text("embedding IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_profiles_embedding_null", table_name="profiles")
    op.drop_index("ix_calls_embedding_null", table_name="calls")
    op.drop_column("call_rankings", "embed_similarity")
    op.drop_column("profiles", "embedding_model")
    op.drop_column("profiles", "embedding")
    op.drop_column("calls", "embedding_model")
    op.drop_column("calls", "embedding")
    # Don't drop the extension — could be used by other future migrations.
```

Verify with `ls -1 alembic/versions/` that 0006 is the current head (no other 0007 file should exist).

- [ ] **Step 4: Update SQLAlchemy models**

In `src/call_monitor/db/models.py`, add to imports:

```python
from pgvector.sqlalchemy import Vector
```

In `class Call(Base)`, add:

```python
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(1024), nullable=True,
    )
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
```

In `class Profile(Base)`, add the same two fields (`embedding`, `embedding_model`).

In `class CallRanking(Base)`, add:

```python
    embed_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
```

(Verify `Float` is imported from sqlalchemy at the top — it likely is. If not, add it.)

- [ ] **Step 5: Test 0007 columns + indexes exist — extend `tests/test_alembic_v2_round_trip.py`**

`pg_session_factory` already upgrades the DB to `head` before yielding (per `conftest.py`). So testing "after upgrade columns exist" only requires the existing migrated engine. To validate the down/up round-trip we need to bypass `pg_session_factory` and drive `alembic` directly against a fresh container — that's overkill given Step 6 already round-trips the schema by re-using the same engine across the test suite. Instead, just assert the columns + partial indexes are present in the migrated DB.

```python
def test_0007_adds_pgvector_columns_and_partial_indexes(pg_url):
    """After alembic upgrade head, pgvector columns and partial NULL indexes exist."""
    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(pg_url, future=True)
    insp = inspect(engine)

    cols_calls = {c["name"] for c in insp.get_columns("calls")}
    cols_profiles = {c["name"] for c in insp.get_columns("profiles")}
    cols_rankings = {c["name"] for c in insp.get_columns("call_rankings")}
    assert "embedding" in cols_calls
    assert "embedding_model" in cols_calls
    assert "embedding" in cols_profiles
    assert "embedding_model" in cols_profiles
    assert "embed_similarity" in cols_rankings

    # Partial indexes: querying pg_indexes is the cleanest check.
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE indexname IN ('ix_calls_embedding_null', 'ix_profiles_embedding_null')"
        )).all()
    assert {r[0] for r in rows} == {"ix_calls_embedding_null", "ix_profiles_embedding_null"}


def test_0007_extension_vector_installed(pg_url):
    """pgvector extension is installed and the vector type usable."""
    from sqlalchemy import create_engine, text

    engine = create_engine(pg_url, future=True)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT extname FROM pg_extension WHERE extname='vector'")).all()
    assert len(rows) == 1
```

(The down/up cycle for 0007 is exercised in CI when the engine is recreated between sessions; an explicit downgrade-test would require a separately-scoped pg container which we don't currently have. If the round-trip becomes critical later, add a session-scoped `fresh_pg_container` fixture and test there.)

- [ ] **Step 6: Run tests + commit**

```bash
uv run pytest tests/test_alembic_v2_round_trip.py -v
uv run pytest -q
uv run ruff check .
git add pyproject.toml uv.lock alembic/versions/0007_pgvector_and_embeddings.py src/call_monitor/db/models.py tests/conftest.py tests/test_alembic_v2_round_trip.py
git commit -m "feat: pgvector extension + embedding columns (no behavior change)"
```

The full suite should still be 437 passing plus the 2 new schema tests = 439 — this task adds nullable columns only.

---

## Task 2: `OllamaEmbedClient` — wraps Ollama's /api/embed

**Files:**
- Create: `src/call_monitor/embeddings/__init__.py` (empty)
- Create: `src/call_monitor/embeddings/client.py`
- Create: `tests/test_embeddings_client.py`

Ollama exposes `POST /api/embed` taking `{"model": "...", "input": "..." or [...]}` and returning `{"embeddings": [[float, ...], ...]}`. The client mirrors `OllamaClient`'s ergonomics: same base_url, same transport (so `httpx.MockTransport` works the same way), `LLMError` on failures.

- [ ] **Step 1: Test `tests/test_embeddings_client.py`**

```python
from __future__ import annotations

import httpx
import pytest

from call_monitor.embeddings.client import OllamaEmbedClient
from call_monitor.ranking.ollama_client import LLMError


def _client_returning(payload: dict, *, status: int = 200) -> OllamaEmbedClient:
    def handler(_req):
        return httpx.Response(status, json=payload)
    return OllamaEmbedClient(
        base_url="http://x", model="bge-m3",
        transport=httpx.MockTransport(handler),
    )


def test_embed_returns_vector_for_single_input():
    vec = [0.1] * 1024
    client = _client_returning({"embeddings": [vec]})
    out = client.embed("hello world")
    assert out == vec
    assert len(out) == 1024


def test_embed_returns_first_vector_when_multiple_returned():
    """Ollama always returns a list-of-lists even for single input;
    the helper unwraps the first element."""
    a = [0.1] * 1024
    b = [0.2] * 1024
    client = _client_returning({"embeddings": [a, b]})
    out = client.embed("anything")
    assert out == a


def test_embed_raises_llm_error_on_http_failure():
    client = _client_returning({"error": "model not loaded"}, status=500)
    with pytest.raises(LLMError):
        client.embed("anything")


def test_embed_raises_llm_error_when_response_lacks_embeddings_key():
    client = _client_returning({"unexpected": "shape"})
    with pytest.raises(LLMError):
        client.embed("anything")


def test_embed_raises_llm_error_on_empty_embeddings_list():
    client = _client_returning({"embeddings": []})
    with pytest.raises(LLMError):
        client.embed("anything")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_embeddings_client.py -v
```

Expected: ImportError (`call_monitor.embeddings.client` doesn't exist yet).

- [ ] **Step 3: Implement `src/call_monitor/embeddings/client.py`**

```python
"""Ollama embedding client — mirrors OllamaClient's ergonomics for the
/api/embed endpoint.

Reuses the shared `LLMError` exception so callers can catch a single
exception type for both chat and embed failures.
"""
from __future__ import annotations

import logging

import httpx

from call_monitor.ranking.ollama_client import LLMError

_logger = logging.getLogger(__name__)


class OllamaEmbedClient:
    """POST /api/embed wrapper.

    Ollama returns `{"embeddings": [[...]]}` — always a list-of-lists,
    even for single-string input. `embed()` unwraps the first vector.

    Vendor swap: Ollama's /api/embed is also HTTP+JSON like OpenAI's
    /v1/embeddings, so swapping endpoint URLs + payload key names is
    a small adapter change.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
    ):
        self._base_url = base_url.rstrip("/")
        self.model = model
        self._transport = transport
        self._timeout = timeout

    def embed(self, text: str) -> list[float]:
        """Embed a single string. Returns the 1024-dim float vector."""
        try:
            with httpx.Client(
                base_url=self._base_url,
                transport=self._transport,
                timeout=self._timeout,
            ) as client:
                resp = client.post("/api/embed", json={
                    "model": self.model,
                    "input": text,
                })
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise LLMError(f"ollama embed failed: {e}") from e

        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or not embeddings:
            raise LLMError(f"ollama embed response missing 'embeddings': {data!r}")
        first = embeddings[0]
        if not isinstance(first, list) or not all(isinstance(x, (int, float)) for x in first):
            raise LLMError(f"ollama embed response shape unexpected: {first!r:.200}")
        return [float(x) for x in first]
```

- [ ] **Step 4: Run tests + commit**

```bash
uv run pytest tests/test_embeddings_client.py -v
uv run pytest -q
uv run ruff check .
git add src/call_monitor/embeddings/__init__.py src/call_monitor/embeddings/client.py tests/test_embeddings_client.py
git commit -m "feat: OllamaEmbedClient for /api/embed"
```

---

## Task 3: Similarity helpers + refresh module

**Files:**
- Create: `src/call_monitor/embeddings/similarity.py`
- Create: `src/call_monitor/embeddings/refresh.py`
- Create: `tests/test_embeddings_similarity.py`
- Create: `tests/test_embeddings_refresh.py`

`similarity.py` has pure functions: `cosine(a, b) -> float | None`, `build_call_text(call) -> str`, `build_profile_text(profile) -> str`. `refresh.py` has the side-effecting helpers: `refresh_call_embedding(session, embed_client, call_id) -> bool`, `refresh_profile_embedding(...)`, `refresh_missing_embeddings_tick(session_factory, embed_client, *, batch_size=200) -> dict[str, int]`.

- [ ] **Step 1: Test `tests/test_embeddings_similarity.py`**

```python
from __future__ import annotations

import math
from types import SimpleNamespace

from call_monitor.embeddings.similarity import (
    build_call_text,
    build_profile_text,
    cosine,
)


def test_cosine_orthogonal_vectors_zero():
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert cosine(a, b) == 0.0


def test_cosine_identical_vectors_one():
    a = [0.5, 0.5, 0.7]
    assert math.isclose(cosine(a, a), 1.0, abs_tol=1e-9)


def test_cosine_anti_parallel_minus_one():
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert math.isclose(cosine(a, b), -1.0, abs_tol=1e-9)


def test_cosine_returns_none_when_either_input_is_none():
    assert cosine(None, [1.0, 0.0]) is None
    assert cosine([1.0, 0.0], None) is None


def test_cosine_returns_none_when_either_vector_is_zero():
    """Zero vector → undefined cosine. Return None, don't divide-by-zero."""
    assert cosine([0.0, 0.0, 0.0], [1.0, 0.0, 0.0]) is None
    assert cosine([1.0, 0.0, 0.0], [0.0, 0.0, 0.0]) is None


def test_cosine_returns_none_when_dimensions_differ():
    """Mismatched dims is a programming error; surface as None rather
    than silently returning a meaningless number."""
    assert cosine([1.0, 0.0], [1.0, 0.0, 0.0]) is None


def test_build_call_text_concatenates_title_summary_truncated_full_text():
    call = SimpleNamespace(
        title="A Title",
        summary="A summary.",
        full_text="x" * 9000,  # > 8000 char cap
    )
    text = build_call_text(call)
    assert text.startswith("A Title")
    assert "A summary." in text
    # full_text truncated to 8000 chars; total length should be ~8020 (with separators)
    assert "x" * 8000 in text
    assert "x" * 8001 not in text


def test_build_call_text_handles_null_summary_and_full_text():
    call = SimpleNamespace(title="Just a title", summary=None, full_text=None)
    text = build_call_text(call)
    assert text.strip() == "Just a title"


def test_build_profile_text_uses_description_only():
    profile = SimpleNamespace(
        description="Pathology research focus.",
        structured={"areas": ["pathology"], "must_include": ["xyz"]},
    )
    assert build_profile_text(profile) == "Pathology research focus."


def test_build_profile_text_empty_description_returns_empty_string():
    profile = SimpleNamespace(description="", structured={})
    assert build_profile_text(profile) == ""
```

- [ ] **Step 2: Implement `src/call_monitor/embeddings/similarity.py`**

```python
"""Pure helpers for embedding similarity + text construction.

No I/O. No DB. Easy to unit-test.
"""
from __future__ import annotations

import math

CALL_TEXT_FULL_TEXT_LIMIT = 8000  # BGE-M3 max input is 8192 tokens; 8000 chars is safe


def cosine(a: list[float] | None, b: list[float] | None) -> float | None:
    """Cosine similarity. None when either input is None, either vector
    is all-zeros, or dimensions don't match — caller treats None as
    UNKNOWN (not FAIL).
    """
    if a is None or b is None:
        return None
    if len(a) != len(b):
        return None
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return None
    return dot / math.sqrt(na * nb)


def build_call_text(call) -> str:
    """Embedding input for a call: title + summary + full_text[:8000].

    Newlines separate the parts so the model gets clear segment boundaries.
    """
    parts: list[str] = []
    if call.title:
        parts.append(call.title)
    if call.summary:
        parts.append(call.summary)
    if call.full_text:
        parts.append(call.full_text[:CALL_TEXT_FULL_TEXT_LIMIT])
    return "\n\n".join(parts)


def build_profile_text(profile) -> str:
    """Embedding input for a profile: description ONLY.

    The structured rubric (areas, must_include, hard_exclusions) is
    keyword-exact, not semantic — embedding it would dilute the
    description's signal.
    """
    return profile.description or ""
```

- [ ] **Step 3: Test `tests/test_embeddings_refresh.py`**

```python
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from call_monitor.db.models import Call, Profile, ProfileKind, Source, User
from call_monitor.embeddings.refresh import (
    refresh_call_embedding,
    refresh_missing_embeddings_tick,
    refresh_profile_embedding,
)
from call_monitor.ranking.ollama_client import LLMError


def _seed_call(s, *, source_slug: str = "vinnova", title: str = "T") -> int:
    src = s.execute(select(Source).where(Source.slug == source_slug)).scalar_one()
    c = Call(
        source_id=src.id, external_id=f"{title}-1", url="https://x",
        title=title, summary="S", full_text="F",
        published_at=datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    return c.id


def _seed_profile(s, *, name: str = "P") -> int:
    u = User(email=f"{name}@ki.se")
    s.add(u)
    s.flush()
    p = Profile(
        name=name, kind=ProfileKind.GROUP, description=f"{name} description",
        structured={"areas": [], "hard_exclusions": []},
        created_by=u.id, ranking_scheme="v2",
    )
    s.add(p)
    s.flush()
    return p.id


def _fake_client(vec: list[float] | None = None, *, raises: Exception | None = None):
    client = MagicMock()
    client.model = "bge-m3"
    if raises is not None:
        client.embed.side_effect = raises
    else:
        client.embed.return_value = vec or [0.1] * 1024
    return client


def test_refresh_call_embedding_writes_vector_and_model(pg_session_factory):
    with pg_session_factory() as s:
        cid = _seed_call(s, title="C1")
        s.commit()
    client = _fake_client(vec=[0.5] * 1024)

    with pg_session_factory() as s:
        ok = refresh_call_embedding(s, client, cid)
        s.commit()
    assert ok is True

    with pg_session_factory() as s:
        c = s.get(Call, cid)
        assert c.embedding is not None
        assert len(c.embedding) == 1024
        assert c.embedding_model == "bge-m3"


def test_refresh_call_embedding_returns_false_on_llm_error(pg_session_factory, caplog):
    import logging
    with pg_session_factory() as s:
        cid = _seed_call(s, title="C2")
        s.commit()
    client = _fake_client(raises=LLMError("ollama down"))

    with pg_session_factory() as s, caplog.at_level(logging.WARNING, logger="call_monitor.embeddings.refresh"):
        ok = refresh_call_embedding(s, client, cid)
        s.commit()
    assert ok is False
    assert any("embedding_compute_failed" in r.getMessage() or "embedding_compute_failed" in r.message
               for r in caplog.records)

    with pg_session_factory() as s:
        c = s.get(Call, cid)
        assert c.embedding is None


def test_refresh_call_embedding_returns_false_for_missing_call(pg_session_factory):
    client = _fake_client()
    with pg_session_factory() as s:
        ok = refresh_call_embedding(s, client, 999999)
    assert ok is False


def test_refresh_profile_embedding_writes_vector_and_model(pg_session_factory):
    with pg_session_factory() as s:
        pid = _seed_profile(s, name="PE1")
        s.commit()
    client = _fake_client(vec=[0.3] * 1024)

    with pg_session_factory() as s:
        ok = refresh_profile_embedding(s, client, pid)
        s.commit()
    assert ok is True

    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding is not None
        assert p.embedding_model == "bge-m3"


def test_refresh_missing_embeddings_tick_processes_only_nulls(pg_session_factory):
    """Tick batches NULL-embedding rows. Already-embedded rows are skipped."""
    client = _fake_client(vec=[0.1] * 1024)
    with pg_session_factory() as s:
        c1 = _seed_call(s, title="MissTick1")
        c2 = _seed_call(s, title="MissTick2")
        # c2 already has embedding
        s.get(Call, c2).embedding = [0.9] * 1024
        s.get(Call, c2).embedding_model = "preexisting"
        p1 = _seed_profile(s, name="MissTick3")
        s.commit()

    summary = refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=client, batch_size=200,
    )
    assert summary["calls_embedded"] == 1     # only c1 was NULL
    assert summary["profiles_embedded"] == 1  # p1 was NULL

    with pg_session_factory() as s:
        assert s.get(Call, c1).embedding is not None
        # c2 untouched
        assert s.get(Call, c2).embedding == [0.9] * 1024
        assert s.get(Profile, p1).embedding is not None


def test_refresh_missing_embeddings_tick_continues_after_one_failure(pg_session_factory):
    """If embed_client raises for one row, tick logs and continues with next."""
    with pg_session_factory() as s:
        c1 = _seed_call(s, title="MTickFail1")
        c2 = _seed_call(s, title="MTickFail2")
        s.commit()

    client = MagicMock()
    client.model = "bge-m3"
    client.embed.side_effect = [LLMError("first one fails"), [0.5] * 1024]

    summary = refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=client, batch_size=200,
    )
    assert summary["calls_embedded"] == 1
    assert summary["calls_failed"] == 1


def test_refresh_missing_tick_re_embeds_rows_with_mismatched_model(pg_session_factory):
    """Codex H2 regression: a row stored under model 'old-model' is re-embedded
    when the configured model is 'bge-m3'. NULL rows still also picked up."""
    with pg_session_factory() as s:
        cid_old = _seed_call(s, title="OldModel")
        s.flush()
        s.get(Call, cid_old).embedding = [0.9] * 1024
        s.get(Call, cid_old).embedding_model = "old-model"
        cid_null = _seed_call(s, title="NullModel")
        s.commit()

    client = _fake_client(vec=[0.1] * 1024)
    summary = refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=client, batch_size=200,
    )
    # Both got re-embedded (the old-model one + the NULL one)
    assert summary["calls_embedded"] == 2

    with pg_session_factory() as s:
        # Both now stamped with current model
        assert s.get(Call, cid_old).embedding_model == "bge-m3"
        assert s.get(Call, cid_null).embedding_model == "bge-m3"


def test_refresh_profile_embedding_clears_when_description_blank(pg_session_factory):
    """Codex M2 regression: clearing description removes the stale embedding."""
    with pg_session_factory() as s:
        pid = _seed_profile(s, name="ClearMe")
        s.flush()
        s.get(Profile, pid).embedding = [0.9] * 1024
        s.get(Profile, pid).embedding_model = "bge-m3"
        s.get(Profile, pid).description = ""  # cleared
        s.commit()

    client = _fake_client(vec=[0.5] * 1024)  # would NOT be invoked
    client.embed.side_effect = AssertionError("embed should not be called for blank text")

    with pg_session_factory() as s:
        ok = refresh_profile_embedding(s, client, pid)
        s.commit()

    assert ok is True  # successful refresh = state-consistent
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.embedding is None
        assert p.embedding_model is None
```

- [ ] **Step 4: Implement `src/call_monitor/embeddings/refresh.py`**

```python
"""Embedding refresh — write paths for call/profile embeddings.

Used by:
- ingestion orchestrator (per new call)
- profile create/edit handler (sync re-embed on description change)
- daily refresh-missing tick (catches what slipped through)
- one-time data migration (0009)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select

from call_monitor.db.models import Call, Profile
from call_monitor.embeddings.similarity import build_call_text, build_profile_text
from call_monitor.ranking.ollama_client import LLMError

if TYPE_CHECKING:
    from call_monitor.embeddings.client import OllamaEmbedClient

_logger = logging.getLogger(__name__)


def refresh_call_embedding(session, embed_client: "OllamaEmbedClient", call_id: int) -> bool:
    """Embed a single call. Returns True on success, False on LLM error
    or missing call. Caller commits the session.
    """
    call = session.get(Call, call_id)
    if call is None:
        return False
    text = build_call_text(call)
    if not text.strip():
        # Nothing to embed; leave embedding NULL.
        return False
    try:
        vec = embed_client.embed(text)
    except LLMError as e:
        _logger.warning(
            "embeddings.embedding_compute_failed",
            extra={
                "event": "embedding_compute_failed",
                "kind": "call", "id": call_id, "error": str(e),
            },
        )
        return False
    call.embedding = vec
    call.embedding_model = embed_client.model
    return True


def refresh_profile_embedding(session, embed_client: "OllamaEmbedClient", profile_id: int) -> bool:
    """Embed a single profile (description only). Returns True on success.

    Codex M2 fix: when the description is blank or whitespace, NULL out
    any existing embedding + embedding_model. Otherwise a profile that
    used to have description='Cancer research' but was edited to '' would
    keep its stale 'cancer' vector and be silently gated against future
    on-topic calls.
    """
    profile = session.get(Profile, profile_id)
    if profile is None:
        return False
    text = build_profile_text(profile)
    if not text.strip():
        # Description cleared → invalidate the embedding. Counts as a
        # successful refresh (we're keeping state consistent), so return True.
        if profile.embedding is not None or profile.embedding_model is not None:
            profile.embedding = None
            profile.embedding_model = None
            _logger.info(
                "embeddings.profile_embedding_cleared",
                extra={"event": "profile_embedding_cleared", "profile_id": profile_id},
            )
        return True
    try:
        vec = embed_client.embed(text)
    except LLMError as e:
        _logger.warning(
            "embeddings.embedding_compute_failed",
            extra={
                "event": "embedding_compute_failed",
                "kind": "profile", "id": profile_id, "error": str(e),
            },
        )
        return False
    profile.embedding = vec
    profile.embedding_model = embed_client.model
    return True


def refresh_missing_embeddings_tick(
    *,
    session_factory,
    embed_client: "OllamaEmbedClient",
    batch_size: int = 200,
) -> dict[str, int]:
    """Daily tick: scan for NULL-embedding rows AND rows whose embedding_model
    differs from the currently-configured model — embed both. Per-row
    try/except so one bad row doesn't kill the batch. Returns a summary
    dict for logging.

    Codex H2 fix: scoping by `embedding IS NULL OR embedding_model != current`
    means a model swap (env change of EMBEDDING_MODEL) is recovered by the
    daily tick within at most 24 h. Operators can run the backfill CLI
    (`python -m call_monitor.embeddings.cli backfill`) for immediate refresh.
    """
    from sqlalchemy import or_
    current_model = embed_client.model
    summary = {"calls_embedded": 0, "calls_failed": 0,
               "profiles_embedded": 0, "profiles_failed": 0}

    with session_factory() as session:
        call_ids = session.execute(
            select(Call.id)
            .where(or_(Call.embedding.is_(None), Call.embedding_model != current_model))
            .limit(batch_size)
        ).scalars().all()
    for cid in call_ids:
        with session_factory() as session:
            ok = refresh_call_embedding(session, embed_client, cid)
            session.commit()
        if ok:
            summary["calls_embedded"] += 1
        else:
            summary["calls_failed"] += 1

    with session_factory() as session:
        profile_ids = session.execute(
            select(Profile.id)
            .where(or_(Profile.embedding.is_(None), Profile.embedding_model != current_model))
            .limit(batch_size)
        ).scalars().all()
    for pid in profile_ids:
        with session_factory() as session:
            ok = refresh_profile_embedding(session, embed_client, pid)
            session.commit()
        if ok:
            summary["profiles_embedded"] += 1
        else:
            summary["profiles_failed"] += 1

    _logger.info(
        "embeddings.refresh_missing_tick.summary",
        extra={"event": "embedding_refresh_tick_summary", **summary},
    )
    return summary
```

(Add `or_` import alongside `select` at the top: `from sqlalchemy import or_, select`.)

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/test_embeddings_similarity.py tests/test_embeddings_refresh.py -v
uv run pytest -q
uv run ruff check .
git add src/call_monitor/embeddings/similarity.py src/call_monitor/embeddings/refresh.py tests/test_embeddings_similarity.py tests/test_embeddings_refresh.py
git commit -m "feat: embedding similarity + refresh helpers"
```

---

## Task 4: Embedding gate in `criteria.py` + `aggregate.py`

**Files:**
- Modify: `src/call_monitor/ranking/criteria.py`
- Modify: `src/call_monitor/ranking/aggregate.py`
- Modify: `src/call_monitor/config.py` (add `EMBEDDING_PREGATE_THRESHOLD`, `EMBEDDING_MODEL`)
- Modify: `tests/test_ranking_criteria.py`
- Modify: `tests/test_ranking_aggregate.py`

The new gate is the 4th field on `Gates`. It's evaluated from a `embed_similarity: float | None` parameter (computed by the caller, not by `criteria.py` itself — keeps `criteria.py` pure).

- [ ] **Step 1: Extend `src/call_monitor/config.py`**

In the settings model, add two fields:

```python
class Settings(BaseSettings):
    # ... existing fields ...
    embedding_model: str = "bge-m3"
    embedding_pregate_threshold: float = 0.30
```

(Match the existing field-naming style — `model_config` env_prefix etc. The values come from env as `EMBEDDING_MODEL` and `EMBEDDING_PREGATE_THRESHOLD`.)

- [ ] **Step 2: Failing test — extend `tests/test_ranking_criteria.py`**

Append:

```python
def test_embedding_gate_passes_above_threshold():
    from call_monitor.ranking.criteria import _embedding, GateStatus, EMBEDDING_PREGATE_THRESHOLD
    g = _embedding(EMBEDDING_PREGATE_THRESHOLD + 0.01)
    assert g.status == GateStatus.PASS


def test_embedding_gate_fails_below_threshold():
    from call_monitor.ranking.criteria import _embedding, GateStatus, EMBEDDING_PREGATE_THRESHOLD
    g = _embedding(EMBEDDING_PREGATE_THRESHOLD - 0.01)
    assert g.status == GateStatus.FAIL
    assert g.reason == "topic_dissimilar"


def test_embedding_gate_passes_at_exact_threshold():
    """Spec: >= threshold passes. Exactly-equal is PASS."""
    from call_monitor.ranking.criteria import _embedding, GateStatus, EMBEDDING_PREGATE_THRESHOLD
    g = _embedding(EMBEDDING_PREGATE_THRESHOLD)
    assert g.status == GateStatus.PASS


def test_embedding_gate_unknown_when_similarity_is_none():
    """Missing embedding (model down or backfill incomplete) → UNKNOWN,
    not FAIL. Caller falls through to LLM."""
    from call_monitor.ranking.criteria import _embedding, GateStatus
    g = _embedding(None)
    assert g.status == GateStatus.UNKNOWN


def test_evaluate_gates_includes_embedding_field():
    """Gates dataclass now has 4 fields, embedding being the new one."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from call_monitor.ranking.criteria import Gates, evaluate_gates

    profile = SimpleNamespace(structured={}, budget_currency=None)
    call = SimpleNamespace(
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None, budget_max=None, currency=None,
        structured={},
    )
    gates = evaluate_gates(profile, call, now=datetime.now(UTC), embed_similarity=0.5)
    assert isinstance(gates, Gates)
    assert hasattr(gates, "embedding")
    assert gates.embedding.status.value == "pass"


def test_evaluate_gates_default_embed_similarity_none_yields_unknown():
    """Backwards-compat: callers that don't pass embed_similarity get UNKNOWN."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from call_monitor.ranking.criteria import GateStatus, evaluate_gates

    profile = SimpleNamespace(structured={}, budget_currency=None)
    call = SimpleNamespace(
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None, budget_max=None, currency=None, structured={},
    )
    gates = evaluate_gates(profile, call, now=datetime.now(UTC))
    assert gates.embedding.status == GateStatus.UNKNOWN
```

- [ ] **Step 3: Update `src/call_monitor/ranking/criteria.py`**

Add to imports:

```python
from call_monitor.config import settings
```

(Match the project's existing pattern for accessing config — the settings singleton is probably `settings = get_settings()` or similar; check `config.py`.)

Add the threshold constant:

```python
EMBEDDING_PREGATE_THRESHOLD = settings.embedding_pregate_threshold
```

(If config is structured differently, use `EMBEDDING_PREGATE_THRESHOLD = 0.30` as a fallback module constant and read from settings inside `_embedding` instead — pick whichever fits the codebase pattern.)

Extend the `Gates` dataclass:

```python
@dataclass(frozen=True)
class Gates:
    timeline: GateResult
    budget: GateResult
    trl: GateResult
    embedding: GateResult     # NEW
```

Add the new gate function:

```python
def _embedding(sim: float | None) -> GateResult:
    if sim is None:
        return GateResult(GateStatus.UNKNOWN)
    if sim < EMBEDDING_PREGATE_THRESHOLD:
        return GateResult(GateStatus.FAIL, reason="topic_dissimilar")
    return GateResult(GateStatus.PASS)
```

Update `evaluate_gates` signature and body:

```python
def evaluate_gates(profile, call, *, now: datetime, embed_similarity: float | None = None) -> Gates:
    return Gates(
        timeline=_timeline(call.deadline, now),
        budget=_budget(profile, call),
        trl=_trl(profile, call),
        embedding=_embedding(embed_similarity),
    )
```

- [ ] **Step 4: Failing test — extend `tests/test_ranking_aggregate.py`**

Append:

```python
def test_aggregate_embedding_fail_short_circuits_to_zero():
    from call_monitor.ranking.aggregate import aggregate, LLMResult
    from call_monitor.ranking.criteria import GateResult, Gates, GateStatus

    gates = Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.PASS),
        trl=GateResult(GateStatus.PASS),
        embedding=GateResult(GateStatus.FAIL, reason="topic_dissimilar"),
    )
    llm = LLMResult(topic_expected=4.0, topic_argmax=4, eligibility="pass", exclusions="pass")
    r = aggregate(gates, llm)
    assert r.score == 0
    assert r.reason == "embedding"


def test_aggregate_embedding_fail_bypassed_by_must_include():
    """Like timeline, must_include_override skips the embedding short-circuit."""
    from call_monitor.ranking.aggregate import aggregate, LLMResult
    from call_monitor.ranking.criteria import GateResult, Gates, GateStatus

    gates = Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.PASS),
        trl=GateResult(GateStatus.PASS),
        embedding=GateResult(GateStatus.FAIL, reason="topic_dissimilar"),
    )
    llm = LLMResult(topic_expected=4.0, topic_argmax=4, eligibility="pass", exclusions="pass")
    r = aggregate(gates, llm, must_include_override=True)
    # Bypass → fall through to clean LLM path: 4.0 * 20 = 80
    assert r.score == 80
    assert r.reason is None


def test_aggregate_embedding_unknown_does_not_short_circuit():
    """UNKNOWN means missing embedding — fall through to LLM as if no gate."""
    from call_monitor.ranking.aggregate import aggregate, LLMResult
    from call_monitor.ranking.criteria import GateResult, Gates, GateStatus

    gates = Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.PASS),
        trl=GateResult(GateStatus.PASS),
        embedding=GateResult(GateStatus.UNKNOWN),
    )
    llm = LLMResult(topic_expected=3.0, topic_argmax=3, eligibility="pass", exclusions="pass")
    r = aggregate(gates, llm)
    assert r.score == 60
    assert r.reason is None
```

- [ ] **Step 5: Update `src/call_monitor/ranking/aggregate.py`**

Add the new short-circuit AFTER timeline/budget/TRL but BEFORE the LLM gates:

```python
    if gates.budget.status == GateStatus.FAIL:
        return AggregateResult(0, "budget")
    if gates.trl.status == GateStatus.FAIL:
        return AggregateResult(0, "trl")

    # Embedding pre-gate (NEW): short-circuit on FAIL unless must_include override.
    # Like timeline, embedding is "topic" — a force-include keyword bypasses it.
    if not must_include_override and gates.embedding.status == GateStatus.FAIL:
        return AggregateResult(0, "embedding")

    # ... existing LLM gates (eligibility, exclusions) follow unchanged.
```

- [ ] **Step 6: Verify all existing aggregate/criteria tests still pass**

```bash
uv run pytest tests/test_ranking_criteria.py tests/test_ranking_aggregate.py -v
```

Existing tests don't pass `embed_similarity` → defaults to None → UNKNOWN → no behavior change. Should all stay green.

- [ ] **Step 7: Verify and commit**

```bash
uv run pytest -q
uv run ruff check .
git add src/call_monitor/config.py src/call_monitor/ranking/criteria.py src/call_monitor/ranking/aggregate.py tests/test_ranking_criteria.py tests/test_ranking_aggregate.py
git commit -m "feat: embedding pre-gate (4th rule-based gate)"
```

---

## Task 5: `rank_pair_v2` integration — fetch similarity, persist, short-circuit

**Files:**
- Modify: `src/call_monitor/ranking/rank_pair.py`
- Modify: `tests/test_ranking_rank_pair_v2.py`

`rank_pair_v2` now reads `profile.embedding` and `call.embedding`, computes cosine, threads it into `evaluate_gates`, returns it on `RankResultV2.embed_similarity`. The early-fail helper writes the similarity even on short-circuits.

- [ ] **Step 1: Failing test — extend `tests/test_ranking_rank_pair_v2.py`**

Append:

```python
def test_rank_pair_v2_short_circuits_on_low_cosine_similarity():
    """Embedding gate FAIL: cosine 0.10 < 0.30 → score 0, reason 'embedding',
    LLM never called."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from call_monitor.ranking.rank_pair import rank_pair_v2

    # Profile and call embeddings that yield ~0.10 cosine
    profile = SimpleNamespace(
        id=1, name="P", description="research desc",
        structured={}, budget_currency=None, ranking_scheme="v2",
        embedding=[1.0, 0.0, 0.0] + [0.0] * 1021,
    )
    call = SimpleNamespace(
        id=1, title="Off-topic call", summary="EV charging", full_text="x",
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None, budget_max=None, currency=None,
        structured={}, keywords=[],
        embedding=[0.1, 0.99, 0.0] + [0.0] * 1021,  # ~0.1 cosine
    )

    client = MagicMock()
    # If the LLM is invoked, fail loudly.
    client.chat.side_effect = AssertionError("LLM should not be called for embedding-FAIL")

    result = rank_pair_v2(client=client, profile=profile, call=call, now=datetime.now(UTC))
    assert result.score == 0
    assert result.reason == "embedding"
    assert result.embed_similarity is not None
    assert result.embed_similarity < 0.30


def test_rank_pair_v2_persists_embed_similarity_on_clean_path():
    """Even on clean LLM-completion paths, embed_similarity is set on the result."""
    # Use the existing fake_client_returning helper from this test module
    # (it returns a happy-path v2 rubric). The similarity should be threaded
    # through to the result.
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from call_monitor.ranking.rank_pair import rank_pair_v2

    profile = SimpleNamespace(
        id=1, name="P", description="cancer research",
        structured={}, budget_currency=None, ranking_scheme="v2",
        embedding=[1.0] + [0.0] * 1023,
    )
    call = SimpleNamespace(
        id=1, title="On-topic call", summary="cancer biomarker", full_text="cancer",
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None, budget_max=None, currency=None,
        structured={}, keywords=[],
        embedding=[0.95, 0.31] + [0.0] * 1022,  # cosine ~0.95
    )

    # Reuse a happy-path mock from the module — adapt to whichever helper exists.
    from tests.test_ranking_rank_pair_v2 import _client_returning_clean_rubric  # noqa
    client = _client_returning_clean_rubric()

    result = rank_pair_v2(client=client, profile=profile, call=call, now=datetime.now(UTC))
    assert result.embed_similarity is not None
    assert result.embed_similarity > 0.9


def test_rank_pair_v2_falls_through_to_llm_when_either_embedding_missing():
    """Either embedding NULL → cosine None → UNKNOWN gate → LLM runs."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from call_monitor.ranking.rank_pair import rank_pair_v2

    profile = SimpleNamespace(
        id=1, name="P", description="desc",
        structured={}, budget_currency=None, ranking_scheme="v2",
        embedding=None,  # MISSING
    )
    call = SimpleNamespace(
        id=1, title="T", summary="S", full_text="F",
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None, budget_max=None, currency=None,
        structured={}, keywords=[],
        embedding=[0.5] * 1024,
    )

    from tests.test_ranking_rank_pair_v2 import _client_returning_clean_rubric
    client = _client_returning_clean_rubric()

    result = rank_pair_v2(client=client, profile=profile, call=call, now=datetime.now(UTC))
    # LLM was called; score reflects the rubric path.
    assert result.score > 0
    assert result.reason is None
    # No similarity recorded.
    assert result.embed_similarity is None


def test_rank_pair_v2_returns_none_similarity_on_model_mismatch(caplog):
    """Codex H2 regression: profile embedded under bge-m3-old, call under bge-m3 →
    cosine is meaningless → return None → UNKNOWN gate → LLM runs. WARN log emitted
    so operator runs the backfill CLI."""
    import logging
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from call_monitor.ranking.rank_pair import rank_pair_v2

    profile = SimpleNamespace(
        id=1, name="P", description="cancer",
        structured={}, budget_currency=None, ranking_scheme="v2",
        embedding=[1.0] + [0.0] * 1023, embedding_model="bge-m3-old",
    )
    call = SimpleNamespace(
        id=42, title="On-topic", summary="cancer", full_text="cancer",
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None, budget_max=None, currency=None,
        structured={}, keywords=[],
        embedding=[1.0] + [0.0] * 1023, embedding_model="bge-m3",
    )

    from tests.test_ranking_rank_pair_v2 import _client_returning_clean_rubric
    client = _client_returning_clean_rubric()

    with caplog.at_level(logging.WARNING, logger="call_monitor.ranking.rank_pair"):
        result = rank_pair_v2(client=client, profile=profile, call=call, now=datetime.now(UTC))

    # LLM ran (gate was UNKNOWN, not FAIL)
    assert result.score > 0
    assert result.reason is None
    # Similarity NOT recorded (we couldn't compute a meaningful one)
    assert result.embed_similarity is None
    # Warning emitted
    assert any("embedding_model_mismatch" in (r.getMessage() or "") or
               "embedding_model_mismatch" in r.message for r in caplog.records)


def test_rank_pair_v2_must_include_bypasses_embedding_gate():
    """A must_include keyword match bypasses the embedding gate, just like timeline."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from call_monitor.ranking.rank_pair import rank_pair_v2

    profile = SimpleNamespace(
        id=1, name="P", description="desc",
        structured={"must_include": ["ferroptosis"]},
        budget_currency=None, ranking_scheme="v2",
        embedding=[1.0, 0.0, 0.0] + [0.0] * 1021,
    )
    call = SimpleNamespace(
        id=1, title="Ferroptosis study", summary="x", full_text="x",
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None, budget_max=None, currency=None,
        structured={}, keywords=[],
        embedding=[0.05, 1.0, 0.0] + [0.0] * 1021,  # cosine ~0.05 — would FAIL gate
    )

    from tests.test_ranking_rank_pair_v2 import _client_returning_clean_rubric
    client = _client_returning_clean_rubric()

    result = rank_pair_v2(client=client, profile=profile, call=call, now=datetime.now(UTC))
    # Bypassed: LLM ran, score is from rubric, not 0.
    assert result.score > 0
    assert result.reason is None
    assert result.must_include_override is True  # field on RankResultV2
    # Similarity still recorded.
    assert result.embed_similarity is not None
```

The test file probably doesn't currently have `_client_returning_clean_rubric` — adapt the helper-name to whatever the existing module uses for a happy-path response. Look at the test file's existing helpers and reuse them.

- [ ] **Step 2: Update `src/call_monitor/ranking/rank_pair.py`**

Add to `RankResultV2`:

```python
@dataclass(frozen=True)
class RankResultV2:
    # ... existing fields ...
    embed_similarity: float | None     # NEW
```

In `rank_pair_v2`, near the top:

```python
def rank_pair_v2(*, client, profile, call, now) -> RankResultV2:
    from call_monitor.embeddings.similarity import cosine

    # Embedding pre-gate input: cosine of the two stored vectors.
    #
    # Returns None (→ UNKNOWN gate → LLM runs) when:
    #   - either vector is missing
    #   - the two were embedded under DIFFERENT models (Codex H2). Cosine
    #     between mismatched-model vectors is meaningless; treating as
    #     UNKNOWN is the safe fallback. Operator should run the backfill
    #     CLI to bring everything to the current model.
    embed_sim = _safe_cosine(profile, call)

    must_include_match = _call_matches_must_include(profile, call)
    gates = evaluate_gates(profile, call, now=now, embed_similarity=embed_sim)
    # ... rest of existing function ...
```

Add the `_safe_cosine` helper near the bottom of the module:

```python
def _safe_cosine(profile, call) -> float | None:
    """Compute cosine but return None on missing vectors OR model mismatch.

    A model mismatch (one vector under bge-m3, the other under e.g.
    bge-m3-v2) yields meaningless cosine — treat as UNKNOWN. Logged at
    WARNING level so an operator notices and runs the backfill CLI.
    """
    from call_monitor.embeddings.similarity import cosine
    p_model = getattr(profile, "embedding_model", None)
    c_model = getattr(call, "embedding_model", None)
    if p_model is not None and c_model is not None and p_model != c_model:
        _logger.warning(
            "ranking.embedding_model_mismatch",
            extra={
                "event": "embedding_model_mismatch",
                "profile_id": profile.id, "call_id": call.id,
                "profile_model": p_model, "call_model": c_model,
            },
        )
        return None
    return cosine(profile.embedding, call.embedding)
```

The hard-fail short-circuit chain gets one new branch for embedding (mirroring timeline):

```python
    # Hard-fail gates (budget/TRL unconditional)
    if gates.budget.status == GateStatus.FAIL:
        return _gate_fail_result(gates, "budget", must_include_match,
                                  embed_similarity=embed_sim)
    if gates.trl.status == GateStatus.FAIL:
        return _gate_fail_result(gates, "trl", must_include_match,
                                  embed_similarity=embed_sim)
    # Timeline FAIL only fails when must_include doesn't match
    if gates.timeline.status == GateStatus.FAIL and not must_include_match:
        return _gate_fail_result(gates, "timeline", must_include_match,
                                  embed_similarity=embed_sim)
    # Embedding FAIL only fails when must_include doesn't match
    if gates.embedding.status == GateStatus.FAIL and not must_include_match:
        return _gate_fail_result(gates, "embedding", must_include_match,
                                  embed_similarity=embed_sim)
```

Update `_gate_fail_result` to accept and store `embed_similarity`:

```python
def _gate_fail_result(gates, reason, must_include_match, *, embed_similarity=None):
    return RankResultV2(
        score=0, reason=reason, gates=gates,
        parsed=None,
        topic_expected=None, logprob_fallback=False,
        must_include_override=must_include_match,
        embed_similarity=embed_similarity,
        model="<gate-short-circuit>", prompt_version="v2.0",
        raw_response={},
    )
```

(Match the existing `_gate_fail_result` signature — it may have other params. Add `embed_similarity` keyword-only.)

In the clean LLM-completion path, set `embed_similarity=embed_sim` on the returned `RankResultV2`.

- [ ] **Step 3: Verify and commit**

```bash
uv run pytest tests/test_ranking_rank_pair_v2.py -v
uv run pytest -q
uv run ruff check .
git add src/call_monitor/ranking/rank_pair.py tests/test_ranking_rank_pair_v2.py
git commit -m "feat: rank_pair_v2 — embedding pre-gate + similarity persisted"
```

---

## Task 6: Worker writes `embed_similarity`; summary surfaces "embedding" reason

**Files:**
- Modify: `src/call_monitor/ranking/worker.py`
- Modify: `tests/test_ranking_worker_v2.py`

The worker translates `RankResultV2` into a `CallRanking` row insert. Add `embed_similarity` to the column write. Update `_v2_summary` to render the new gate-FAIL reason.

- [ ] **Step 1: Failing test — extend `tests/test_ranking_worker_v2.py`**

Append:

```python
def test_worker_writes_embed_similarity_on_clean_v2_path(pg_session_factory, ...):
    """When rank_pair_v2 returns a clean result with embed_similarity=0.45,
    the worker persists that value to call_rankings.embed_similarity."""
    # Adapt to the test module's existing fixtures and seed helpers.
    # Stub rank_pair_v2 to return a fixed RankResultV2 with embed_similarity=0.45.
    # Run the worker tick; assert the persisted CallRanking row has embed_similarity == 0.45.
    ...


def test_worker_v2_summary_renders_embedding_fail_reason():
    """When aggregate_reason == 'embedding', the one-line summary mentions
    'topic_dissimilar' and the cosine value."""
    from call_monitor.ranking.criteria import GateResult, Gates, GateStatus
    from call_monitor.ranking.rank_pair import RankResultV2
    from call_monitor.ranking.worker import _v2_summary

    r = RankResultV2(
        score=0, reason="embedding",
        gates=Gates(
            timeline=GateResult(GateStatus.PASS),
            budget=GateResult(GateStatus.PASS),
            trl=GateResult(GateStatus.PASS),
            embedding=GateResult(GateStatus.FAIL, reason="topic_dissimilar"),
        ),
        parsed=None, topic_expected=None, logprob_fallback=False,
        must_include_override=False, embed_similarity=0.18,
        model="m", prompt_version="v2.0", raw_response={},
    )
    summary = _v2_summary(r)
    assert "topic_dissimilar" in summary
    assert "0.18" in summary
```

(Adapt the first test to whatever fixture/seed style the existing test module uses — a worker tick test that exercises `process_v2_job` end-to-end.)

- [ ] **Step 2: Update `src/call_monitor/ranking/worker.py`**

Find where `CallRanking(...)` is constructed in the v2 path and add:

```python
    embed_similarity=r.embed_similarity,
```

Find `_v2_summary` and add a branch:

```python
def _v2_summary(r: RankResultV2) -> str:
    if r.aggregate_reason == "embedding":
        sim_str = f"{r.embed_similarity:.3f}" if r.embed_similarity is not None else "n/a"
        return f"FAIL: topic_dissimilar (cosine {sim_str})"
    # ... existing branches ...
```

(Use the actual attribute name — `r.reason` if that's the field, not `r.aggregate_reason`. Verify against the dataclass.)

- [ ] **Step 3: Verify and commit**

```bash
uv run pytest tests/test_ranking_worker_v2.py -v
uv run pytest -q
uv run ruff check .
git add src/call_monitor/ranking/worker.py tests/test_ranking_worker_v2.py
git commit -m "feat: worker persists embed_similarity + surfaces topic_dissimilar reason"
```

---

## Task 7: Hook embedding refresh into ingestion orchestrator + profile handlers

**Files:**
- Modify: `src/call_monitor/ingestion/orchestrator.py`
- Modify: `src/call_monitor/web/routes/profiles.py`
- Modify: `src/call_monitor/main.py` (build `OllamaEmbedClient` once, pass through)
- Create: `tests/test_ingestion_orchestrator_embeds.py`
- Modify: `tests/test_web_profiles.py`

**Codex H1 fix — ordering matters here.** Today the orchestrator does `upsert calls + enqueue ranking jobs + commit` in ONE transaction. If we just bolt embedding-refresh on as a post-commit step, the v2 worker (running every 60 s) will pick up jobs whose calls have NULL embeddings and silently fall through to LLM with `embed_similarity = NULL` permanently. Same problem with profile-edit calling `backfill_profile()` after the commit.

The fix is to **split into three serialized steps**: (1) data commit → (2) embed (best-effort, separate transactions per row) → (3) enqueue ranking jobs. After step 3 commits, every enqueued job's call/profile already has an embedding (or, in the rare best-effort failure case, has UNKNOWN — and the LLM still runs as the safe fallback).

- [ ] **Step 1: Failing test — `tests/test_ingestion_orchestrator_embeds.py`**

```python
"""Codex H1 ordering tests: the orchestrator must embed touched calls
BEFORE enqueuing ranking jobs."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from call_monitor.db.models import Call, RankingJob


def test_orchestrator_embeds_touched_calls_before_enqueueing_ranking_jobs(
    pg_session_factory, profile_factory, ...
):
    """Order is critical (Codex H1): when the worker picks up a v2 job,
    the call embedding must already be in place."""
    from call_monitor.ingestion.orchestrator import ingest_source  # adapt to real entry point

    pid, _ = profile_factory(name="OrderTest")
    embed_client = MagicMock()
    embed_client.model = "bge-m3"
    embed_client.embed.return_value = [0.5] * 1024

    # A fake adapter producing 2 fresh calls.
    fake_adapter = _FakeAdapter(calls=[
        {"external_id": "ord-1", "title": "T1", "url": "https://x/1"},
        {"external_id": "ord-2", "title": "T2", "url": "https://x/2"},
    ])

    # Run ingestion
    ingest_source(
        adapter=fake_adapter,
        session_factory=pg_session_factory,
        embed_client=embed_client,
        ...
    )

    # Both calls have embeddings AND ranking jobs exist.
    with pg_session_factory() as s:
        calls = s.execute(select(Call).where(Call.external_id.in_(["ord-1", "ord-2"]))).scalars().all()
        for c in calls:
            assert c.embedding is not None, f"call {c.id} should have embedding before any job is enqueued"
        jobs = s.execute(select(RankingJob).where(RankingJob.call_id.in_([c.id for c in calls]))).scalars().all()
        assert len(jobs) == 2 * 1  # 2 calls × 1 active profile


def test_orchestrator_re_embeds_touched_calls_even_when_already_present(
    pg_session_factory, profile_factory, ...
):
    """Codex Open-Q2: upsert always UPDATEs on conflict, so we always re-embed.
    No 'unchanged call' optimization until we have a content-hash mechanism."""
    from call_monitor.ingestion.orchestrator import ingest_source

    profile_factory(name="ReEmbed")
    embed_client = MagicMock()
    embed_client.model = "bge-m3"
    embed_client.embed.return_value = [0.5] * 1024

    fake_adapter = _FakeAdapter(calls=[{"external_id": "re-1", "title": "T", "url": "https://x"}])
    ingest_source(adapter=fake_adapter, session_factory=pg_session_factory, embed_client=embed_client, ...)
    ingest_source(adapter=fake_adapter, session_factory=pg_session_factory, embed_client=embed_client, ...)

    # Embed called twice — once per ingest (no skip-unchanged optimization)
    assert embed_client.embed.call_count == 2


def test_orchestrator_continues_enqueue_when_embed_fails(pg_session_factory, profile_factory, ...):
    """Embedding service down → embed fails → ranking job still enqueued (gate UNKNOWN, LLM runs)."""
    from call_monitor.ingestion.orchestrator import ingest_source
    from call_monitor.ranking.ollama_client import LLMError

    pid, _ = profile_factory(name="EmbedFail")
    embed_client = MagicMock()
    embed_client.model = "bge-m3"
    embed_client.embed.side_effect = LLMError("ollama down")

    fake_adapter = _FakeAdapter(calls=[{"external_id": "ef-1", "title": "T", "url": "https://x"}])
    ingest_source(adapter=fake_adapter, session_factory=pg_session_factory, embed_client=embed_client, ...)

    with pg_session_factory() as s:
        c = s.execute(select(Call).where(Call.external_id == "ef-1")).scalar_one()
        assert c.embedding is None  # embed failed; left NULL
        jobs = s.execute(select(RankingJob).where(RankingJob.call_id == c.id)).scalars().all()
        assert len(jobs) == 1       # but the ranking job still got enqueued
```

(Adapt `_FakeAdapter` and the orchestrator entry-point name to the actual codebase patterns. The existing `tests/test_ingestion*.py` files have helpers you can copy.)

- [ ] **Step 2: Update `src/call_monitor/ingestion/orchestrator.py`**

Read the existing orchestrator's signature. Add a required `embed_client: OllamaEmbedClient` parameter (no `None` default — embeddings are first-class now; tests inject a mock). Then split the existing single transaction into three steps:

```python
from call_monitor.embeddings.refresh import refresh_call_embedding

def ingest_source(*, adapter, session_factory, embed_client, ...):
    # ----- Step 1: upsert calls + advance source timestamp (one transaction) -----
    with session_factory() as session:
        with session.begin():
            touched_call_ids: list[int] = []
            for raw_call in adapter.fetch():
                cid = upsert_call(session, raw_call)
                touched_call_ids.append(cid)
            session.execute(
                Source.__table__.update()
                .where(Source.id == source_id)
                .values(last_fetched_at=run_started_at)
            )
        # commits at end of `session.begin()` block

    # ----- Step 2: embed touched calls (best-effort, per-row transaction) -----
    # Codex H1: must complete BEFORE enqueue so the v2 worker never sees
    # a job whose call has NULL embedding (except in the rare embed-fail
    # case, which falls through to LLM via UNKNOWN gate).
    for cid in touched_call_ids:
        try:
            with session_factory() as embed_session:
                refresh_call_embedding(embed_session, embed_client, cid)
                embed_session.commit()
        except Exception as e:  # noqa: BLE001
            _logger.exception(
                "ingestion.embedding_failed",
                extra={"event": "ingestion_embedding_failed",
                       "call_id": cid, "error": str(e)},
            )
            # Don't abort — enqueue the job anyway. UNKNOWN gate → LLM runs.

    # ----- Step 3: enqueue ranking jobs (one transaction) -----
    with session_factory() as session:
        with session.begin():
            active_profiles = session.execute(
                select(Profile.id, Profile.ranking_scheme).where(
                    Profile.is_active.is_(True)
                )
            ).all()
            for cid in touched_call_ids:
                for pid, pscheme in active_profiles:
                    enqueue_for_pair(session, call_id=cid, profile_id=pid, scheme=pscheme)
```

Atomicity notes:
- Step 1 succeeds, step 2/3 fails → calls inserted, no jobs queued. The next ingest tick won't re-discover the same calls (they exist; upsert no-ops) — but the daily refresh-missing tick + a manual-rerank action would catch this. This matches today's behavior when the original single-commit failed mid-flight.
- Step 1 + 2 succeed, step 3 fails → calls embedded, no jobs queued. Same recovery path.
- Step 2 partial failure (some embeds fail) → step 3 still runs; the failed-embed calls go through the LLM via UNKNOWN gate when their job is drained.

- [ ] **Step 3: Update `src/call_monitor/web/routes/profiles.py`**

**Codex H1 fix for profile-edit ordering.** Today the handler calls `session.commit()` and then `backfill_profile()`. The backfill enqueues 90 days of ranking jobs immediately — the v2 worker can drain them before the (post-commit) profile-embedding refresh lands. Fix: re-order to **commit profile → embed (sync, best-effort) → backfill_profile()**. By the time the backfill enqueues jobs, the profile embedding is in place.

For `profile_create_submit`: same order — commit, embed, then any post-create work that triggers ranking.

```python
from call_monitor.embeddings.refresh import refresh_profile_embedding

# In profile_edit_submit:
embed_client = request.app.state.embed_client       # from main.py

with session_factory() as session:
    p = session.get(Profile, profile_id)
    old_description = p.description                  # capture BEFORE mutating
    # ... existing field updates ...
    p.description = description.strip()
    # ... admin-only ranking_scheme update ...
    session.commit()

# ----- Embed profile if description actually changed (or always if you want
# to keep this simple — embed cost is ~50 ms). Per Codex H1, this MUST
# happen before backfill_profile() so newly-enqueued jobs see the fresh
# embedding (and not the stale one from the pre-edit description).
if old_description.strip() != description.strip():
    try:
        with session_factory() as s:
            refresh_profile_embedding(s, embed_client, profile_id)
            s.commit()
    except Exception as e:  # noqa: BLE001
        _logger.warning(
            "profile.embedding_refresh_failed",
            extra={"event": "profile_embedding_refresh_failed",
                   "profile_id": profile_id, "error": str(e)},
        )
        # Don't abort — backfill still runs, embedding gate becomes UNKNOWN
        # → LLM runs as the safe fallback. Daily tick re-tries.

# Now safe to enqueue ranking jobs. By here the profile embedding reflects
# the new description (or is NULL after a clear, or is a stale-but-harmless
# value if the embed just failed).
backfill_profile(session_factory=session_factory, profile_id=profile_id)
```

For `profile_create_submit`: similar order — commit the new profile, embed it sync, then any ranking-enqueue logic.

The `embed_client` is read from `request.app.state.embed_client` (which `main.py` populates in Step 4).

- [ ] **Step 4: Update `src/call_monitor/main.py`**

Where `OllamaClient` is constructed in the app factory, also build `OllamaEmbedClient`:

```python
from call_monitor.embeddings.client import OllamaEmbedClient

# ... where OllamaClient is instantiated ...
ollama = OllamaClient(base_url=settings.ollama_url, model=settings.gemma_model)
embed_client = OllamaEmbedClient(base_url=settings.ollama_url, model=settings.embedding_model)

app.state.ollama = ollama
app.state.embed_client = embed_client     # NEW
```

Pass `embed_client` through to the scheduler builder (Task 8 will register the daily tick) and to the ingestion orchestrator.

- [ ] **Step 5: Tests for profile handlers — extend `tests/test_web_profiles.py`**

Append:

```python
def test_profile_create_triggers_embedding_refresh(pg_session_factory, monkeypatch, ...):
    """Creating a new profile triggers a sync embedding compute."""
    # Stub OllamaEmbedClient.embed to return a known vector
    # POST to /profiles/new
    # Assert: profile.embedding is non-NULL after the response
    ...


def test_profile_edit_changing_description_triggers_embedding_refresh(...):
    """Editing description triggers re-embed."""
    ...


def test_profile_edit_not_changing_description_skips_embedding_refresh(...):
    """Editing rubric only (areas/must_include) does NOT re-embed —
    only description goes into the embedding."""
    ...
```

Adapt to existing test patterns (admin bootstrap, signed cookie, etc.).

- [ ] **Step 6: Verify and commit**

```bash
uv run pytest tests/test_ingestion_orchestrator_embeds.py tests/test_web_profiles.py -v
uv run pytest -q
uv run ruff check .
git add src/call_monitor/ingestion/orchestrator.py src/call_monitor/web/routes/profiles.py src/call_monitor/main.py tests/test_ingestion_orchestrator_embeds.py tests/test_web_profiles.py
git commit -m "feat: ingest + profile handlers trigger embedding refresh"
```

---

## Task 8: Scheduler — register `embeddings:refresh-missing` daily 03:30

**Files:**
- Modify: `src/call_monitor/scheduler.py`
- Modify: `tests/test_scheduler_v2_jobs.py`

Add a daily APScheduler job that calls `refresh_missing_embeddings_tick`. The 03:30 trigger is before the 04:00 setwise pass and after the 03:00 ingestion window — natural placement in the daily flow.

- [ ] **Step 1: Failing test — extend `tests/test_scheduler_v2_jobs.py`**

```python
def test_scheduler_registers_embeddings_refresh_missing(pg_session_factory):
    sched = _build(pg_session_factory)
    assert sched.get_job("embeddings:refresh-missing") is not None
    sched.shutdown(wait=False)
```

Update `_build` to inject a fake `embed_client` (use `MagicMock`) — the scheduler's `build_scheduler` will need a new parameter for this.

- [ ] **Step 2: Update `src/call_monitor/scheduler.py`**

Add `embed_client: OllamaEmbedClient` parameter to `build_scheduler`. Inside, register the new job:

```python
    def _embeddings_tick() -> None:
        try:
            summary = refresh_missing_embeddings_tick(
                session_factory=session_factory,
                embed_client=embed_client,
                batch_size=200,
            )
        except Exception as e:  # noqa: BLE001
            _logger.exception(
                "embeddings.refresh_missing_tick.failed",
                extra={"event": "embedding_refresh_tick_failed", "error": str(e)},
            )
            return
        # No need to re-log summary — refresh_missing_embeddings_tick already does.

    sched.add_job(
        _embeddings_tick,
        trigger=CronTrigger(hour=3, minute=30),
        id="embeddings:refresh-missing",
        name="Daily backfill of missing embeddings",
        replace_existing=True, max_instances=1, coalesce=True,
    )
```

The import goes at module top (eager — `embeddings.refresh` is light):

```python
from call_monitor.embeddings.refresh import refresh_missing_embeddings_tick
```

Update the existing `tests/test_scheduler.py` `test_build_scheduler_returns_one_job_per_active_adapter` superset assertion to include `embeddings:refresh-missing`.

Update `src/call_monitor/main.py` to pass `embed_client` through to `build_scheduler`.

- [ ] **Step 3: Verify and commit**

```bash
uv run pytest tests/test_scheduler_v2_jobs.py tests/test_scheduler.py -v
uv run pytest -q
uv run ruff check .
git add src/call_monitor/scheduler.py src/call_monitor/main.py tests/test_scheduler_v2_jobs.py tests/test_scheduler.py
git commit -m "feat: embeddings:refresh-missing daily 03:30"
```

---

## Task 9: Backfill CLI — `python -m call_monitor.embeddings.cli backfill`

**Files:**
- Create: `src/call_monitor/embeddings/cli.py`
- Create: `tests/test_embeddings_cli.py`

**Codex H3 fix.** The original plan put historical backfill in an Alembic data migration (`0009_backfill_embeddings.py`). That's wrong: Alembic should be schema-only; depending on Ollama during `alembic upgrade head` makes deploys fragile (model unavailable → silent partial backfill → migration "succeeds"). Replace with an explicit CLI command that operators run as a separate deploy step.

**Updated deploy procedure:**

```bash
alembic upgrade head                                    # schema only — safe + fast
python -m call_monitor.embeddings.cli backfill          # populates embeddings via Ollama
# start the app
```

The CLI internally calls the same `refresh_missing_embeddings_tick` that the daily APScheduler job uses. Difference: CLI prints progress to stdout and runs an unbounded number of batches until everything is embedded; the scheduler tick runs one bounded batch per day.

- [ ] **Step 1: Failing test — `tests/test_embeddings_cli.py`**

```python
"""CLI backfill — wraps refresh_missing_embeddings_tick + prints progress."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from sqlalchemy import select

from call_monitor.db.models import Call, Profile, ProfileKind, Source, User
from call_monitor.embeddings.cli import backfill_main


def _seed(s, *, n_calls: int = 3, n_profiles: int = 2) -> None:
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    for i in range(n_calls):
        s.add(Call(
            source_id=src.id, external_id=f"cli-{i}", url=f"https://x/{i}",
            title=f"Call {i}", summary="x",
            published_at=datetime.now(UTC),
        ))
    for i in range(n_profiles):
        u = User(email=f"cli-{i}@ki.se")
        s.add(u)
        s.flush()
        s.add(Profile(
            name=f"P{i}", kind=ProfileKind.GROUP,
            description=f"Description for P{i}",
            structured={}, created_by=u.id, ranking_scheme="v2",
        ))


def test_cli_backfill_embeds_all_null_rows(pg_session_factory, capsys):
    with pg_session_factory() as s:
        _seed(s, n_calls=3, n_profiles=2)
        s.commit()

    fake_client = MagicMock()
    fake_client.model = "bge-m3"
    fake_client.embed.return_value = [0.5] * 1024

    with patch("call_monitor.embeddings.cli._build_embed_client", return_value=fake_client):
        exit_code = backfill_main(
            argv=["backfill"],
            session_factory=pg_session_factory,
        )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "calls_embedded" in captured.out
    assert "profiles_embedded" in captured.out

    with pg_session_factory() as s:
        n_call_null = s.execute(
            select(Call).where(Call.embedding.is_(None))
        ).scalars().all()
        n_profile_null = s.execute(
            select(Profile).where(Profile.embedding.is_(None))
        ).scalars().all()
    assert len(n_call_null) == 0
    assert len(n_profile_null) == 0


def test_cli_backfill_loops_until_all_done(pg_session_factory):
    """When more rows than batch_size, CLI loops; tick runs >1 time."""
    with pg_session_factory() as s:
        # Seed many calls so a batch_size=2 would require 3+ batches
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        for i in range(5):
            s.add(Call(
                source_id=src.id, external_id=f"loop-{i}", url=f"https://x/{i}",
                title=f"L{i}", summary="x", published_at=datetime.now(UTC),
            ))
        s.commit()

    fake_client = MagicMock()
    fake_client.model = "bge-m3"
    fake_client.embed.return_value = [0.5] * 1024

    with patch("call_monitor.embeddings.cli._build_embed_client", return_value=fake_client):
        exit_code = backfill_main(
            argv=["backfill", "--batch-size", "2"],
            session_factory=pg_session_factory,
        )
    assert exit_code == 0
    # All 5 ended up embedded
    with pg_session_factory() as s:
        nulls = s.execute(select(Call).where(Call.embedding.is_(None))).scalars().all()
    assert len(nulls) == 0
    # embed was called 5 times (once per call)
    assert fake_client.embed.call_count == 5


def test_cli_backfill_returns_nonzero_on_persistent_failures(pg_session_factory):
    """If any row fails to embed (LLM down etc), exit 1 so deploy scripts catch it."""
    from call_monitor.ranking.ollama_client import LLMError

    with pg_session_factory() as s:
        _seed(s, n_calls=1, n_profiles=0)
        s.commit()

    fake_client = MagicMock()
    fake_client.model = "bge-m3"
    fake_client.embed.side_effect = LLMError("down")

    with patch("call_monitor.embeddings.cli._build_embed_client", return_value=fake_client):
        exit_code = backfill_main(
            argv=["backfill"],
            session_factory=pg_session_factory,
        )
    assert exit_code == 1
```

- [ ] **Step 2: Implement `src/call_monitor/embeddings/cli.py`**

```python
"""Embeddings backfill CLI — `python -m call_monitor.embeddings.cli backfill`.

Walks every call + profile with NULL or model-mismatched embedding, calls
Ollama in batches, prints a per-batch summary. Exits 0 on full success,
1 if any row failed to embed.

Use as part of deploy:
    alembic upgrade head
    python -m call_monitor.embeddings.cli backfill
    # start app
"""
from __future__ import annotations

import argparse
import logging
import sys

from call_monitor.config import settings
from call_monitor.db.session import make_session_factory  # adapt to actual factory builder
from call_monitor.embeddings.client import OllamaEmbedClient
from call_monitor.embeddings.refresh import refresh_missing_embeddings_tick

_logger = logging.getLogger(__name__)


def _build_embed_client() -> OllamaEmbedClient:
    """Separate function so tests can monkeypatch."""
    return OllamaEmbedClient(
        base_url=settings.ollama_url,
        model=settings.embedding_model,
    )


def backfill_main(argv: list[str] | None = None, *, session_factory=None) -> int:
    """Run the backfill until all NULL/mismatched rows are embedded, OR
    until a batch makes no progress (means everything left is failing).

    Returns 0 on full success, 1 if anything failed to embed.
    """
    parser = argparse.ArgumentParser(prog="call_monitor.embeddings.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_backfill = sub.add_parser("backfill", help="Embed all NULL or model-mismatched rows.")
    p_backfill.add_argument("--batch-size", type=int, default=200)
    args = parser.parse_args(argv)

    if args.cmd != "backfill":
        parser.error(f"unknown command: {args.cmd}")

    if session_factory is None:  # pragma: no cover — production path
        from sqlalchemy import create_engine
        engine = create_engine(settings.database_url)
        session_factory = make_session_factory(engine)

    embed_client = _build_embed_client()
    print(f"backfill starting — model={embed_client.model} batch_size={args.batch_size}")

    total = {"calls_embedded": 0, "calls_failed": 0,
             "profiles_embedded": 0, "profiles_failed": 0}
    while True:
        summary = refresh_missing_embeddings_tick(
            session_factory=session_factory,
            embed_client=embed_client,
            batch_size=args.batch_size,
        )
        for k, v in summary.items():
            total[k] += v
        progress = summary["calls_embedded"] + summary["profiles_embedded"]
        failed = summary["calls_failed"] + summary["profiles_failed"]
        print(f"  batch: calls_embedded={summary['calls_embedded']} "
              f"profiles_embedded={summary['profiles_embedded']} "
              f"calls_failed={summary['calls_failed']} "
              f"profiles_failed={summary['profiles_failed']}")
        if progress == 0:
            # No progress in this batch — either everything is embedded,
            # or all remaining rows are failing.
            break

    print(f"backfill done — totals: {total}")
    has_failures = total["calls_failed"] > 0 or total["profiles_failed"] > 0
    return 1 if has_failures else 0


# Allow `python -m call_monitor.embeddings.cli ...`
def main() -> None:  # pragma: no cover
    sys.exit(backfill_main(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
```

(Adapt `make_session_factory` import path to whatever the project actually uses — look at `src/call_monitor/db/session.py` or similar.)

- [ ] **Step 3: Verify and commit**

```bash
uv run pytest tests/test_embeddings_cli.py -v
uv run pytest -q
uv run ruff check .
# Smoke test the entry point (no real Ollama needed; will exit 1 on the LLMError)
uv run python -m call_monitor.embeddings.cli --help

git add src/call_monitor/embeddings/cli.py tests/test_embeddings_cli.py
git commit -m "feat: embeddings backfill CLI (replaces deferred data migration)"
```

---

## Task 10: Surface `embed_similarity` in admin compare + call detail

**Files:**
- Modify: `src/call_monitor/web/routes/admin.py`
- Modify: `src/call_monitor/web/templates/ranking_compare.html`
- Modify: `src/call_monitor/web/templates/call_detail.html`
- Modify: `tests/test_web_admin_compare.py`
- Modify: `tests/test_web_call_detail_v2.py`

Cosmetic but operationally important: surface the cosine on call detail and as a column in the admin compare view. Lets the admin spot LLM/embedding disagreements (high cosine + low LLM topic_fit, or vice versa) — the documented miss-detection signal.

- [ ] **Step 1: Failing test — extend `tests/test_web_admin_compare.py`**

```python
def test_compare_view_renders_v2_cosine_column():
    """When v2 ranking has embed_similarity=0.42, the cosine column shows '0.42'."""
    # Seed CallRanking(scheme="v2", aggregate_score=80, embed_similarity=0.42)
    # Hit /admin/ranking-compare?profile=<pid>
    # Assert "0.42" appears in a cell labeled "v2 cosine"
    ...


def test_compare_view_renders_em_dash_for_v1_cosine_column():
    """v1 has no embed_similarity → em-dash in v1 cosine column."""
    ...
```

- [ ] **Step 2: Update `src/call_monitor/web/routes/admin.py`**

Extend the SELECT in `ranking_compare` to also pull `v1.embed_similarity` and `v2.embed_similarity`. Add to the items dict:

```python
"v1_cosine": v1_sim,
"v2_cosine": v2_sim,
```

- [ ] **Step 3: Update `src/call_monitor/web/templates/ranking_compare.html`**

Add two columns to the table header and rows:

```html
<th style="text-align:right">v1 cos</th>
<th style="text-align:right">v2 cos</th>
```

```html
<td style="text-align:right">{% if it.v1_cosine is not none %}{{ '%.2f' % it.v1_cosine }}{% else %}—{% endif %}</td>
<td style="text-align:right">{% if it.v2_cosine is not none %}{{ '%.2f' % it.v2_cosine }}{% else %}—{% endif %}</td>
```

- [ ] **Step 4: Failing test — extend `tests/test_web_call_detail_v2.py`**

```python
def test_call_detail_renders_embed_similarity_when_present():
    """When the v2 ranking has embed_similarity=0.55, the call detail page
    shows 'Cosine: 0.55' (or similar)."""
    ...
```

- [ ] **Step 5: Update `src/call_monitor/web/templates/call_detail.html`**

In the v2 ranking section, add (where the score is shown):

```html
{% if ranking.embed_similarity is not none %}
  <p><strong>Cosine similarity:</strong> {{ '%.3f' % ranking.embed_similarity }}</p>
{% endif %}
```

- [ ] **Step 6: Verify and commit**

```bash
uv run pytest tests/test_web_admin_compare.py tests/test_web_call_detail_v2.py -v
uv run pytest -q
uv run ruff check .
git add src/call_monitor/web/routes/admin.py src/call_monitor/web/templates/ranking_compare.html src/call_monitor/web/templates/call_detail.html tests/test_web_admin_compare.py tests/test_web_call_detail_v2.py
git commit -m "feat: surface embed_similarity in compare view + call detail"
```

---

## Verification at end of plan

1. `uv run ruff check .` → clean
2. `uv run pytest -q` → all green (~460+ tests after Plan D)
3. `DATABASE_URL=... uv run alembic check` → "No new upgrade operations detected"
4. `DATABASE_URL=... uv run alembic upgrade head` → applies 0007 cleanly
5. `DATABASE_URL=... uv run alembic downgrade -1 && uv run alembic upgrade head` → round-trips cleanly
6. **Deploy procedure** (document in deploy notes — NOT automated by Alembic):
   1. `alembic upgrade head` (schema only)
   2. `python -m call_monitor.embeddings.cli backfill` (populates embeddings via Ollama; expect ~50ms × number of calls + ~50ms × number of profiles)
   3. Start the app
7. Manual smoke (requires Ollama with `bge-m3` and `gemma4:e4b-it-q4_K_M`):
   - `ollama pull bge-m3` if not already
   - Run the deploy procedure above against a dev DB
   - Visit `/admin` → unchanged
   - Visit `/admin/ranking-compare?profile=<id>` → v1 cos + v2 cos columns visible
   - Visit `/calls/<id>?profile=<id>` → "Cosine similarity: 0.45" line visible for v2 rankings
   - Hand-fire embedding refresh:
     ```python
     from call_monitor.embeddings.refresh import refresh_missing_embeddings_tick
     summary = refresh_missing_embeddings_tick(session_factory=..., embed_client=...)
     ```
   - Run a v2 rank and confirm `embed_similarity` is populated in `call_rankings`

## After this plan ships

- Embedding pre-gate is operational. The 4-week phased rollout from the v2 spec can proceed with the embedding gate active from day one (just leave threshold at the defensive 0.30 default).
- Historical CallRanking rows have `embed_similarity = NULL`. New ranks populate the column. After ~1 week of production data, tune the threshold based on the joint distribution of `(embed_similarity, llm_topic_fit)`.
- The reranker (Phase 2 of the spec) is now a small follow-up plan if needed — pgvector is in place; just add the BGE-reranker client and shortlist logic.
- **Model swap procedure** (e.g. swap `bge-m3` → `bge-m3-v2` later): change `EMBEDDING_MODEL` env var → run `python -m call_monitor.embeddings.cli backfill` → restart app. The daily refresh-missing tick handles operators who skip the manual backfill (re-embeds drift within 24 h).

## Out of scope (deferred to follow-up plans)

- BGE-reranker-v2-m3 cross-encoder shortlist (Phase 2 of the spec)
- SPECTER2 + SciNCL ensemble (model swap)
- Embedding-similarity-based dashboard sort or filter (let users sort/filter calls by cosine)
- Per-criterion embeddings (separate vectors for areas, hard_exclusions, must_include)
- A `/admin/embedding-disagreements` page showing high-cosine-low-LLM-score and vice versa
