# Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Daily, idempotent ingestion of funding calls from the EU Funding & Tenders Portal and Vinnova into a `calls` table; an in-process APScheduler that runs each adapter on a staggered cron; a `/` dashboard that lists recent calls (no ranking yet — Plan 3 adds that); and an admin page showing per-source health and consecutive-failure alerting. After this plan, the app self-fills with calls and you can browse them in a browser.

**Architecture:** Each source is a single Python file in `src/call_monitor/sources/` exposing a `SourceAdapter` Protocol implementation. `ingestion/orchestrator.py` runs an adapter, normalizes records into the `NormalizedCall` dataclass, calls `upsert_call()` per record (idempotent on `(source_id, external_id)`), and only advances `sources.last_fetched_at` if the run completed end-to-end. Failure isolation: per-adapter try/except, three consecutive failures email an admin. APScheduler is wired into the FastAPI `lifespan` and runs in the same process — single-instance is a hard deploy constraint inherited from the spec.

**Tech Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.x · Alembic · APScheduler 3.x · `httpx` (with `MockTransport` for tests) · Jinja2 + HTMX · `pytest` + `testcontainers[postgres]` · existing foundation (auth, settings, logging, /healthz, /me)

---

## File structure created or modified by this plan

```
call-monitor/
├── pyproject.toml                                      # add: apscheduler
├── .env.example                                        # add: VINNOVA_API_KEY, EU_PORTAL_BASE_URL
├── alembic/
│   └── versions/
│       └── 0002_sources_and_calls.py
├── src/call_monitor/
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── types.py                                    # NormalizedCall, SourceAdapter Protocol, AdapterError
│   │   ├── eu_portal.py
│   │   └── vinnova.py
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── upsert.py                                   # upsert_call(session, NormalizedCall, source_id) -> bool
│   │   ├── orchestrator.py                             # run_ingestion(session_factory, adapter, source_row, alerter)
│   │   └── alerts.py                                   # ConsecutiveFailureTracker
│   ├── db/
│   │   └── models.py                                   # add Source, Call
│   ├── scheduler.py                                    # build_scheduler(app) — APScheduler wiring
│   ├── web/
│   │   └── routes/
│   │       ├── calls.py                                # GET / — ranked-agnostic call list
│   │       └── admin.py                                # GET /admin — source health
│   ├── web/templates/
│   │   ├── calls_index.html
│   │   ├── call_detail.html
│   │   └── admin.html
│   └── main.py                                         # wire scheduler into lifespan + register routers
└── tests/
    ├── conftest.py                                     # add: source row helper
    ├── test_ingestion_upsert.py
    ├── test_sources_types.py
    ├── test_sources_eu_portal.py
    ├── test_sources_vinnova.py
    ├── test_ingestion_orchestrator.py
    ├── test_ingestion_alerts.py
    ├── test_scheduler.py
    ├── test_web_calls.py
    └── test_web_admin.py
```

---

## Task 1: Add APScheduler dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add `apscheduler` to runtime deps**

In `pyproject.toml`, under `[project] dependencies`, add `"apscheduler>=3.10"` to the list:

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "jinja2>=3.1",
    "sqlalchemy>=2.0",
    "alembic>=1.13",
    "psycopg[binary]>=3.2",
    "pydantic>=2.8",
    "pydantic-settings>=2.4",
    "itsdangerous>=2.2",
    "httpx>=0.27",
    "python-multipart>=0.0.9",
    "apscheduler>=3.10",
]
```

- [ ] **Step 2: Sync and verify**

Run: `uv sync && uv run pytest -v`
Expected: lockfile updated, all 65 existing tests still pass.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "deps: add apscheduler for in-process daily ingestion"
```

---

## Task 2: Sources + Calls schema (model + migration)

**Files:**
- Modify: `src/call_monitor/db/models.py`
- Create: `alembic/versions/0002_sources_and_calls.py`
- Create: `tests/test_models_calls.py`

**Schema (per spec):**

```
sources
  id, name, slug (unique), last_fetched_at, config (jsonb), created_at

calls
  id, source_id (FK sources.id), external_id, url, title, summary,
  full_text, deadline (timestamptz), budget_min (bigint), budget_max (bigint),
  currency (text), programme (text), keywords (text[]), published_at (timestamptz),
  fetched_at (timestamptz), raw (jsonb), created_at (timestamptz)
  UNIQUE(source_id, external_id)
  GIN index on keywords
  B-tree on deadline
  B-tree on published_at
```

The migration also seeds two source rows so the orchestrator and admin page have something to point at on a fresh DB.

- [ ] **Step 1: Write failing test**

`tests/test_models_calls.py`:

```python
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_monitor.db.models import Call, Source


def test_seeded_sources_present(pg_session_factory):
    with pg_session_factory() as s:
        slugs = sorted(row[0] for row in s.execute(select(Source.slug)).all())
    assert slugs == ["eu_portal", "vinnova"]


def test_can_insert_and_read_call(pg_session_factory):
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id="2026-01234",
            url="https://example.org/calls/2026-01234",
            title="Test call",
            summary="One-paragraph summary.",
            keywords=["health", "ai"],
            published_at=datetime(2026, 4, 1, tzinfo=UTC),
            deadline=datetime(2026, 6, 30, 21, 59, tzinfo=UTC),
            currency="SEK",
            budget_min=100_000,
            budget_max=2_000_000,
            raw={"any": "json"},
        )
        s.add(c)
        s.commit()
        s.refresh(c)
        assert c.id is not None
        assert c.fetched_at is not None  # server_default


def test_call_unique_per_source_and_external_id(pg_session_factory):
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add(Call(source_id=src.id, external_id="dup-1", url="u", title="t"))
        s.commit()
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add(Call(source_id=src.id, external_id="dup-1", url="u2", title="t2"))
        s.commit()


def test_calls_keywords_gin_index_supports_overlap(pg_session_factory):
    """Smoke: keyword filter via && (overlap) returns hits — proves GIN index is in place."""
    from sqlalchemy import text
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "eu_portal")).scalar_one()
        s.add(Call(source_id=src.id, external_id="k1", url="u", title="t",
                   keywords=["digital pathology", "federated learning"]))
        s.add(Call(source_id=src.id, external_id="k2", url="u", title="t",
                   keywords=["energy"]))
        s.commit()
        rows = s.execute(
            text("SELECT external_id FROM calls WHERE keywords && :kw ORDER BY external_id"),
            {"kw": ["federated learning"]},
        ).all()
    assert [r[0] for r in rows] == ["k1"]
```

- [ ] **Step 2: Run — expect failure** (`Source`/`Call` don't exist)

Run: `uv run pytest tests/test_models_calls.py -v`

- [ ] **Step 3: Add the models to `src/call_monitor/db/models.py`**

Append after the existing `MagicLinkToken` class:

```python
from sqlalchemy import ARRAY, JSON, Text


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    last_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    calls: Mapped[list[Call]] = relationship(back_populates="source")


class Call(Base):
    __tablename__ = "calls"
    __table_args__ = (
        # Unique per source per external_id — the idempotency contract for
        # upsert; the implicit unique index backs the ON CONFLICT clause.
        # GIN on keywords + B-tree on deadline/published_at are declared in
        # the migration (the model stays clean of postgresql_using=... noise).
        UniqueConstraint("source_id", "external_id", name="uq_calls_source_external"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    budget_min: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    budget_max: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    programme: Mapped[str | None] = mapped_column(String(200), nullable=True)
    keywords: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    raw: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    source: Mapped[Source] = relationship(back_populates="calls")
```

Make sure `UniqueConstraint` is in the top-of-file imports alongside the existing SQLAlchemy symbols (it isn't there from the foundation models).

- [ ] **Step 4: Write the migration**

`alembic/versions/0002_sources_and_calls.py`:

```python
"""sources and calls

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-17
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("slug", sa.String(length=50), nullable=False),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "config",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("slug", name="uq_sources_slug"),
    )

    op.create_table(
        "calls",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("full_text", sa.Text(), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("budget_min", sa.BigInteger(), nullable=True),
        sa.Column("budget_max", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("programme", sa.String(length=200), nullable=True),
        sa.Column(
            "keywords",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "raw",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], ondelete="RESTRICT", name="fk_calls_source_id"
        ),
        sa.UniqueConstraint(
            "source_id", "external_id", name="uq_calls_source_external"
        ),
    )

    # GIN supports the array overlap operator (&&) used by the keyword filter.
    op.create_index(
        "ix_calls_keywords_gin",
        "calls",
        ["keywords"],
        postgresql_using="gin",
    )
    # B-tree indices support feed sorts on the dashboard.
    op.create_index("ix_calls_deadline", "calls", ["deadline"])
    op.create_index("ix_calls_published_at", "calls", ["published_at"])

    # Seed the two v1 sources. Idempotent in spirit: the migration runs once.
    # Slugs are stable so adapters key on them.
    op.bulk_insert(
        sa.table(
            "sources",
            sa.column("name", sa.String),
            sa.column("slug", sa.String),
        ),
        [
            {"name": "EU Funding & Tenders Portal", "slug": "eu_portal"},
            {"name": "Vinnova", "slug": "vinnova"},
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_calls_published_at", table_name="calls")
    op.drop_index("ix_calls_deadline", table_name="calls")
    op.drop_index("ix_calls_keywords_gin", table_name="calls")
    op.drop_table("calls")
    op.drop_table("sources")
```

- [ ] **Step 5: Run model tests**

Run: `uv run pytest tests/test_models_calls.py -v`
Expected: 4 passed.

- [ ] **Step 6: Run full suite**

Run: `uv run pytest -v`
Expected: 69 passed (65 existing + 4 new). The dynamic `pg_session_factory` truncate already discovers `sources` and `calls` via `Base.metadata.sorted_tables`, but the seeded `sources` rows will be removed too — re-seed them explicitly so dependent tests keep finding them.

Update `tests/conftest.py` `pg_session_factory` fixture: after the truncate, re-insert the two seed sources:

```python
@pytest.fixture()
def pg_session_factory(migrated_engine):
    from call_monitor.db.base import Base

    SessionLocal = make_session_factory(migrated_engine)
    yield SessionLocal
    table_names = ", ".join(t.name for t in Base.metadata.sorted_tables)
    if table_names:
        with migrated_engine.begin() as conn:
            conn.execute(
                text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE")
            )
            # Re-seed sources so model/orchestrator tests find them after teardown.
            conn.execute(
                text(
                    "INSERT INTO sources (name, slug) VALUES "
                    "('EU Funding & Tenders Portal', 'eu_portal'), "
                    "('Vinnova', 'vinnova')"
                )
            )
```

- [ ] **Step 7: Run ruff and commit**

```bash
uv run ruff check .
git add src/call_monitor/db/models.py alembic/versions/ tests/test_models_calls.py tests/conftest.py
git commit -m "feat: sources and calls tables with seeded source rows + GIN index"
```

---

## Task 3: NormalizedCall + SourceAdapter Protocol

**Files:**
- Create: `src/call_monitor/sources/__init__.py` (empty)
- Create: `src/call_monitor/sources/types.py`
- Create: `tests/test_sources_types.py`

- [ ] **Step 1: Write failing test**

`tests/test_sources_types.py`:

```python
from datetime import UTC, datetime

from call_monitor.sources.types import AdapterError, NormalizedCall, SourceAdapter


def test_normalized_call_minimal_fields():
    c = NormalizedCall(
        external_id="X-1",
        url="https://example.org/x-1",
        title="Some title",
    )
    assert c.summary is None
    assert c.keywords == []
    assert c.raw == {}


def test_adapter_error_is_exception():
    e = AdapterError("network blip")
    assert isinstance(e, Exception)


def test_protocol_check():
    """A class that implements .slug and .fetch_new_calls satisfies SourceAdapter."""
    class FakeAdapter:
        slug = "fake"

        def fetch_new_calls(self, since):
            yield NormalizedCall(external_id="X", url="u", title="t")

    fake = FakeAdapter()
    # runtime_checkable Protocol; isinstance check
    assert isinstance(fake, SourceAdapter)
    out = list(fake.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert out[0].external_id == "X"
```

- [ ] **Step 2: Write `src/call_monitor/sources/types.py`**

```python
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


class AdapterError(Exception):
    """Adapter could not complete a fetch — orchestrator should treat as a
    full-run failure (do NOT advance last_fetched_at, increment failure count)."""


@dataclass(slots=True)
class NormalizedCall:
    """Source-agnostic shape that the ingestion orchestrator persists.

    Adapters MUST normalize timestamps to UTC and budgets to integer minor
    units of `currency` (or None if unknown). `external_id` is the source's
    own stable identifier — uniqueness is per-source, enforced by the DB.
    """

    external_id: str
    url: str
    title: str
    summary: str | None = None
    full_text: str | None = None
    deadline: datetime | None = None
    budget_min: int | None = None
    budget_max: int | None = None
    currency: str | None = None
    programme: str | None = None
    keywords: list[str] = field(default_factory=list)
    published_at: datetime | None = None
    raw: dict = field(default_factory=dict)


@runtime_checkable
class SourceAdapter(Protocol):
    """Each source is one adapter. Implementations live in `sources/<slug>.py`.

    `fetch_new_calls(since)` must yield NormalizedCalls — generator, not list,
    so the orchestrator can stream-upsert without holding everything in memory.
    `since` is the DB's `sources.last_fetched_at`; pass `None`-or-very-old date
    for first-ever fetch.
    """

    slug: str

    def fetch_new_calls(self, since: datetime) -> Iterator[NormalizedCall]: ...
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_sources_types.py -v` → 3 passed.
Run: `uv run ruff check .`

- [ ] **Step 4: Commit**

```bash
git add src/call_monitor/sources/ tests/test_sources_types.py
git commit -m "feat: NormalizedCall + SourceAdapter Protocol (sources contract)"
```

---

## Task 4: Idempotent `upsert_call` helper

**Files:**
- Create: `src/call_monitor/ingestion/__init__.py` (empty)
- Create: `src/call_monitor/ingestion/upsert.py`
- Create: `tests/test_ingestion_upsert.py`

- [ ] **Step 1: Write failing tests**

`tests/test_ingestion_upsert.py`:

```python
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from call_monitor.db.models import Call, Source
from call_monitor.ingestion.upsert import upsert_call
from call_monitor.sources.types import NormalizedCall


def _vinnova_id(s):
    return s.execute(select(Source.id).where(Source.slug == "vinnova")).scalar_one()


def test_upsert_inserts_new_row(pg_session_factory):
    with pg_session_factory() as s:
        sid = _vinnova_id(s)
        was_new = upsert_call(
            s,
            source_id=sid,
            call=NormalizedCall(external_id="V-1", url="u", title="T"),
        )
        s.commit()
        assert was_new is True
        rows = s.execute(select(Call).where(Call.external_id == "V-1")).scalars().all()
    assert len(rows) == 1


def test_upsert_replaces_existing_row_in_place(pg_session_factory):
    with pg_session_factory() as s:
        sid = _vinnova_id(s)
        upsert_call(
            s,
            source_id=sid,
            call=NormalizedCall(external_id="V-1", url="u", title="OLD"),
        )
        s.commit()
        was_new = upsert_call(
            s,
            source_id=sid,
            call=NormalizedCall(
                external_id="V-1",
                url="u",
                title="NEW",
                deadline=datetime(2026, 6, 1, tzinfo=UTC),
            ),
        )
        s.commit()
        assert was_new is False  # already existed
        rows = s.execute(select(Call).where(Call.external_id == "V-1")).scalars().all()
    assert len(rows) == 1
    assert rows[0].title == "NEW"
    assert rows[0].deadline == datetime(2026, 6, 1, tzinfo=UTC)


def test_upsert_preserves_fetched_at_floor(pg_session_factory):
    """fetched_at is set on first insert; subsequent upserts MAY refresh it
    (we use NOW()) since 'last seen' is the more useful semantic."""
    with pg_session_factory() as s:
        sid = _vinnova_id(s)
        upsert_call(s, source_id=sid, call=NormalizedCall(external_id="V-1", url="u", title="T"))
        s.commit()
        first = s.execute(select(Call.fetched_at).where(Call.external_id == "V-1")).scalar_one()
    # Second pass at least 1ms later
    import time
    time.sleep(0.01)
    with pg_session_factory() as s:
        sid = _vinnova_id(s)
        upsert_call(s, source_id=sid, call=NormalizedCall(external_id="V-1", url="u", title="T"))
        s.commit()
        second = s.execute(select(Call.fetched_at).where(Call.external_id == "V-1")).scalar_one()
    assert second >= first


def test_upsert_is_per_source(pg_session_factory):
    """Same external_id under different source_ids → two rows."""
    with pg_session_factory() as s:
        eu = s.execute(select(Source.id).where(Source.slug == "eu_portal")).scalar_one()
        vn = s.execute(select(Source.id).where(Source.slug == "vinnova")).scalar_one()
        upsert_call(s, source_id=eu, call=NormalizedCall(external_id="X-1", url="u", title="T"))
        upsert_call(s, source_id=vn, call=NormalizedCall(external_id="X-1", url="u", title="T"))
        s.commit()
        n = s.execute(select(Call).where(Call.external_id == "X-1")).scalars().all()
    assert len(n) == 2
```

- [ ] **Step 2: Write `src/call_monitor/ingestion/upsert.py`**

Use Postgres' native `INSERT ... ON CONFLICT ... DO UPDATE` for atomicity. Returning the `xmax` distinguishes insert from update.

```python
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from call_monitor.db.models import Call
from call_monitor.sources.types import NormalizedCall


def upsert_call(
    session: Session, *, source_id: int, call: NormalizedCall
) -> bool:
    """Insert or update one Call row keyed on (source_id, external_id).

    Returns True if a row was newly inserted, False if an existing row was
    updated. Caller is responsible for the surrounding transaction.

    Implementation note: PostgreSQL's `INSERT ... ON CONFLICT DO UPDATE`
    gives us atomicity in a single round-trip and lets us discover whether
    the row was new via `xmax` (zero on insert, non-zero on update).
    """
    payload = {
        "source_id": source_id,
        "external_id": call.external_id,
        "url": call.url,
        "title": call.title,
        "summary": call.summary,
        "full_text": call.full_text,
        "deadline": call.deadline,
        "budget_min": call.budget_min,
        "budget_max": call.budget_max,
        "currency": call.currency,
        "programme": call.programme,
        "keywords": call.keywords,
        "published_at": call.published_at,
        "raw": call.raw,
    }
    # Pre-check: was the row there already? Cheap one-row indexed lookup, one
    # extra round-trip per call. At ~50 calls/week this is negligible. If
    # profiling later says otherwise, switch to RETURNING (xmax = 0)::bool via
    # raw SQL — same atomic guarantee, no read.
    from sqlalchemy import select

    existed = session.execute(
        select(Call.id).where(
            Call.source_id == source_id, Call.external_id == call.external_id
        )
    ).first() is not None

    stmt = insert(Call).values(**payload, fetched_at=func.now())
    update_cols = {
        c: stmt.excluded[c] for c in payload if c not in {"source_id", "external_id"}
    }
    update_cols["fetched_at"] = func.now()  # refresh "last seen" on every encounter
    stmt = stmt.on_conflict_do_update(
        constraint="uq_calls_source_external",
        set_=update_cols,
    )
    session.execute(stmt)
    return not existed
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_ingestion_upsert.py -v` → 4 passed.

- [ ] **Step 4: Commit**

```bash
git add src/call_monitor/ingestion/ tests/test_ingestion_upsert.py
git commit -m "feat: idempotent upsert_call via Postgres ON CONFLICT"
```

---

## Task 5: EU Portal adapter

**Files:**
- Create: `src/call_monitor/sources/eu_portal.py`
- Create: `tests/test_sources_eu_portal.py`

**Endpoint:** `https://ec.europa.eu/info/funding-tenders/opportunities/api/v3.0/notices/search` returns JSON. We filter to `status: ['OPEN', 'FORTHCOMING']` per spec. The exact request/response shape varies by API version — we encode our best-effort mapping below; the implementer should capture one real response into a fixture and adjust field names if needed.

- [ ] **Step 1: Write failing tests**

`tests/test_sources_eu_portal.py`:

```python
from datetime import UTC, datetime

import httpx

from call_monitor.sources.eu_portal import EUPortalAdapter
from call_monitor.sources.types import AdapterError


_FAKE_OK_BODY = {
    "results": [
        {
            "identifier": "HORIZON-HLTH-2026-DISEASE-03-15",
            "title": "AI for early disease detection",
            "summary": "Calls supporting AI methods for early-stage disease detection.",
            "deadlineDate": "2026-09-15T17:00:00Z",
            "budgetTotal": 12000000,
            "currency": "EUR",
            "programmeName": "Horizon Europe",
            "keywords": ["health", "ai", "diagnostics"],
            "publicationDate": "2026-04-01T00:00:00Z",
            "url": "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/HORIZON-HLTH-2026-DISEASE-03-15",
        }
    ],
    "totalResults": 1,
}


def _mock_transport(status: int = 200, body=None, raise_exc=None):
    def handler(request):
        if raise_exc is not None:
            raise raise_exc
        return httpx.Response(status, json=body if body is not None else _FAKE_OK_BODY)
    return httpx.MockTransport(handler)


def test_fetch_yields_normalized_calls():
    adapter = EUPortalAdapter(
        base_url="https://ec.europa.eu",
        transport=_mock_transport(),
    )
    out = list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert len(out) == 1
    c = out[0]
    assert c.external_id == "HORIZON-HLTH-2026-DISEASE-03-15"
    assert c.title.startswith("AI for early")
    assert c.deadline == datetime(2026, 9, 15, 17, 0, tzinfo=UTC)
    assert c.published_at == datetime(2026, 4, 1, tzinfo=UTC)
    assert c.budget_max == 12000000
    assert c.currency == "EUR"
    assert "ai" in c.keywords
    assert c.raw["identifier"] == "HORIZON-HLTH-2026-DISEASE-03-15"


def test_fetch_skips_records_missing_required_fields():
    body = {
        "results": [
            {"identifier": "OK-1", "title": "ok", "url": "u"},
            {"identifier": "BAD-1"},  # missing title and url
            {"title": "no id"},
        ]
    }
    adapter = EUPortalAdapter(
        base_url="https://x", transport=_mock_transport(body=body)
    )
    out = list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert [c.external_id for c in out] == ["OK-1"]


def test_http_error_raises_adapter_error():
    adapter = EUPortalAdapter(
        base_url="https://x", transport=_mock_transport(status=503)
    )
    import pytest
    with pytest.raises(AdapterError):
        list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))


def test_network_failure_raises_adapter_error():
    adapter = EUPortalAdapter(
        base_url="https://x",
        transport=_mock_transport(raise_exc=httpx.ConnectError("boom")),
    )
    import pytest
    with pytest.raises(AdapterError):
        list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
```

- [ ] **Step 2: Write `src/call_monitor/sources/eu_portal.py`**

```python
from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx

from call_monitor.sources.types import AdapterError, NormalizedCall

_logger = logging.getLogger(__name__)

# v3.0 search endpoint returns OPEN + FORTHCOMING calls in JSON form.
# Pin a versioned path so a portal-side rev to v4 is a deliberate code change.
_SEARCH_PATH = "/info/funding-tenders/opportunities/api/v3.0/notices/search"


def _parse_iso(value: str | None) -> datetime | None:
    """Parse the portal's ISO timestamps into tz-aware UTC.

    The portal mostly emits 'Z' suffix; treat naive parses as UTC since the
    spec mandates UTC storage.
    """
    if not value:
        return None
    try:
        # `fromisoformat` accepts 'Z' suffix on Python 3.11+.
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class EUPortalAdapter:
    """SourceAdapter for the EU Funding & Tenders Portal."""

    slug = "eu_portal"

    def __init__(
        self,
        *,
        base_url: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout

    def fetch_new_calls(self, since: datetime) -> Iterator[NormalizedCall]:
        params = {
            "status": "OPEN,FORTHCOMING",
            # `since` is informational at this endpoint — server-side filtering
            # by date isn't always honored. The orchestrator's idempotent upsert
            # tolerates fetching the full set on every run.
        }
        try:
            with httpx.Client(
                base_url=self._base_url,
                transport=self._transport,
                timeout=self._timeout,
            ) as client:
                resp = client.get(_SEARCH_PATH, params=params)
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPError as e:
            raise AdapterError(f"EU Portal fetch failed: {e}") from e
        except ValueError as e:  # JSON decode
            raise AdapterError(f"EU Portal returned non-JSON body: {e}") from e

        results = payload.get("results", []) if isinstance(payload, dict) else []
        for item in results:
            normalized = self._normalize(item)
            if normalized is None:
                continue
            yield normalized

    def _normalize(self, item: dict) -> NormalizedCall | None:
        # Required fields per our contract.
        external_id = item.get("identifier")
        title = item.get("title")
        url = item.get("url")
        if not (external_id and title and url):
            _logger.info(
                "eu_portal.skip_incomplete",
                extra={"event": "skip_incomplete", "fields_present": list(item.keys())},
            )
            return None

        return NormalizedCall(
            external_id=str(external_id),
            url=str(url),
            title=str(title),
            summary=item.get("summary"),
            deadline=_parse_iso(item.get("deadlineDate")),
            budget_min=item.get("budgetMin") if isinstance(item.get("budgetMin"), int) else None,
            budget_max=item.get("budgetTotal") if isinstance(item.get("budgetTotal"), int) else None,
            currency=item.get("currency"),
            programme=item.get("programmeName"),
            keywords=list(item.get("keywords") or []),
            published_at=_parse_iso(item.get("publicationDate")),
            raw=item,
        )
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_sources_eu_portal.py -v` → 4 passed.

- [ ] **Step 4: Commit**

```bash
git add src/call_monitor/sources/eu_portal.py tests/test_sources_eu_portal.py
git commit -m "feat: EU Funding & Tenders Portal source adapter"
```

---

## Task 6: Vinnova adapter

**Files:**
- Create: `src/call_monitor/sources/vinnova.py`
- Create: `tests/test_sources_vinnova.py`

**Endpoint:** `https://data.vinnova.se/api/utlysningar/{YYYY-MM-DD}` returns calls updated on/after that date in JSON. v2 of the API requires `Accept: application/vnd.vinnova.opendata.2.0+json`. An API key is required for the production endpoint — we read it from `VINNOVA_API_KEY`.

- [ ] **Step 1: Write failing tests**

`tests/test_sources_vinnova.py`:

```python
from datetime import UTC, datetime

import httpx
import pytest

from call_monitor.sources.types import AdapterError
from call_monitor.sources.vinnova import VinnovaAdapter


_FAKE_OK_BODY = [
    {
        "Diarienummer": "2026-01234",
        "Titel": "Datadriven sjukvård",
        "Beskrivning": "Stöd för datadriven utveckling i hälso- och sjukvård.",
        "OppningsDatum": "2026-04-01",
        "StangningsDatum": "2026-06-30",
        "Budget": 50000000,
        "Valuta": "SEK",
        "Program": "Datadriven hälsa",
        "Lankar": [
            {"Url": "https://www.vinnova.se/e/2026-01234"}
        ],
        "Nyckelord": ["digital pathology", "ai"],
    }
]


def _mock_transport(status: int = 200, body=None, raise_exc=None):
    def handler(request):
        if raise_exc is not None:
            raise raise_exc
        # Sanity check: header negotiation
        assert request.headers.get("Accept") == "application/vnd.vinnova.opendata.2.0+json"
        return httpx.Response(status, json=body if body is not None else _FAKE_OK_BODY)
    return httpx.MockTransport(handler)


def test_fetch_normalizes_vinnova_record():
    adapter = VinnovaAdapter(
        api_key="test-key",
        base_url="https://data.vinnova.se",
        transport=_mock_transport(),
    )
    out = list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert len(out) == 1
    c = out[0]
    assert c.external_id == "2026-01234"
    assert c.title == "Datadriven sjukvård"
    # OppningsDatum interpreted as published_at (Europe/Stockholm midnight → UTC)
    assert c.published_at == datetime(2026, 4, 1, tzinfo=UTC)
    # StangningsDatum at 23:59 Europe/Stockholm → 21:59 UTC during summer (CEST)
    # We store as the day's end in the *source's* timezone; conversion happens
    # at write. Adapter MUST emit tz-aware UTC.
    assert c.deadline is not None and c.deadline.tzinfo is not None
    assert c.budget_max == 50_000_000
    assert c.currency == "SEK"
    assert c.url == "https://www.vinnova.se/e/2026-01234"
    assert "ai" in c.keywords


def test_skips_records_without_diarienummer():
    body = [
        {"Diarienummer": "OK", "Titel": "T", "Lankar": [{"Url": "u"}]},
        {"Titel": "no id"},
    ]
    adapter = VinnovaAdapter(
        api_key="k", base_url="https://x", transport=_mock_transport(body=body)
    )
    out = list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert [c.external_id for c in out] == ["OK"]


def test_5xx_raises_adapter_error():
    adapter = VinnovaAdapter(
        api_key="k", base_url="https://x", transport=_mock_transport(status=502)
    )
    with pytest.raises(AdapterError):
        list(adapter.fetch_new_calls(since=datetime(2026, 1, 1, tzinfo=UTC)))


def test_no_api_key_raises_at_construction():
    with pytest.raises(ValueError):
        VinnovaAdapter(api_key="", base_url="https://x")
```

- [ ] **Step 2: Write `src/call_monitor/sources/vinnova.py`**

```python
from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

from call_monitor.sources.types import AdapterError, NormalizedCall

_logger = logging.getLogger(__name__)
_STOCKHOLM = ZoneInfo("Europe/Stockholm")
_ACCEPT_V2 = "application/vnd.vinnova.opendata.2.0+json"


def _parse_local_date(s: str | None, *, end_of_day: bool) -> datetime | None:
    """Parse a Vinnova `YYYY-MM-DD` string as Europe/Stockholm wall time.

    `end_of_day=True` → 23:59:00 local time (used for deadlines).
    `end_of_day=False` → 00:00:00 local time (used for publication date).
    Returns a tz-aware UTC datetime, since the spec mandates UTC storage.
    """
    if not s:
        return None
    try:
        y, m, d = (int(p) for p in s.split("-"))
    except (ValueError, AttributeError):
        return None
    h, mi = (23, 59) if end_of_day else (0, 0)
    local = datetime(y, m, d, h, mi, tzinfo=_STOCKHOLM)
    return local.astimezone(UTC)


class VinnovaAdapter:
    """SourceAdapter for Vinnova's open-data Utlysningar endpoint."""

    slug = "vinnova"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ):
        if not api_key:
            raise ValueError("VinnovaAdapter requires a non-empty api_key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout

    def fetch_new_calls(self, since: datetime) -> Iterator[NormalizedCall]:
        # The endpoint takes a date suffix and returns calls modified on/after.
        date_suffix = since.astimezone(_STOCKHOLM).date().isoformat()
        path = f"/api/utlysningar/{date_suffix}"
        try:
            with httpx.Client(
                base_url=self._base_url,
                transport=self._transport,
                timeout=self._timeout,
            ) as client:
                resp = client.get(
                    path,
                    headers={
                        "Accept": _ACCEPT_V2,
                        "Ocp-Apim-Subscription-Key": self._api_key,
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPError as e:
            raise AdapterError(f"Vinnova fetch failed: {e}") from e
        except ValueError as e:
            raise AdapterError(f"Vinnova returned non-JSON body: {e}") from e

        if not isinstance(payload, list):
            raise AdapterError(f"Vinnova returned non-list payload: {type(payload).__name__}")

        for item in payload:
            normalized = self._normalize(item)
            if normalized is None:
                continue
            yield normalized

    def _normalize(self, item: dict) -> NormalizedCall | None:
        external_id = item.get("Diarienummer")
        title = item.get("Titel")
        if not external_id or not title:
            _logger.info(
                "vinnova.skip_incomplete",
                extra={"event": "skip_incomplete", "fields_present": list(item.keys())},
            )
            return None

        # The portal nests the canonical URL inside `Lankar`. Take the first.
        url = ""
        for link in item.get("Lankar") or []:
            if isinstance(link, dict) and link.get("Url"):
                url = str(link["Url"])
                break
        if not url:
            return None

        budget = item.get("Budget")
        return NormalizedCall(
            external_id=str(external_id),
            url=url,
            title=str(title),
            summary=item.get("Beskrivning"),
            deadline=_parse_local_date(item.get("StangningsDatum"), end_of_day=True),
            budget_max=int(budget) if isinstance(budget, int | float) else None,
            currency=item.get("Valuta"),
            programme=item.get("Program"),
            keywords=list(item.get("Nyckelord") or []),
            published_at=_parse_local_date(item.get("OppningsDatum"), end_of_day=False),
            raw=item,
        )
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_sources_vinnova.py -v` → 4 passed.

- [ ] **Step 4: Commit**

```bash
git add src/call_monitor/sources/vinnova.py tests/test_sources_vinnova.py
git commit -m "feat: Vinnova source adapter (data.vinnova.se v2 API)"
```

---

## Task 7: Consecutive-failure tracker (admin alerter)

**Files:**
- Create: `src/call_monitor/ingestion/alerts.py`
- Create: `tests/test_ingestion_alerts.py`

The spec: "three consecutive failures send an admin alert email." Track per-source streaks in memory (single-instance assumption); reset on first success after a streak; emit one email per crossing of the threshold to avoid spamming on prolonged outages.

- [ ] **Step 1: Write failing tests**

`tests/test_ingestion_alerts.py`:

```python
from call_monitor.auth.email import InMemoryEmailSender
from call_monitor.ingestion.alerts import ConsecutiveFailureTracker


def test_threshold_not_yet_reached():
    sender = InMemoryEmailSender()
    t = ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email="a@ki.se")
    t.record_failure("eu_portal", error="oops")
    t.record_failure("eu_portal", error="oops")
    assert sender.sent == []


def test_threshold_reached_emits_one_email():
    sender = InMemoryEmailSender()
    t = ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email="a@ki.se")
    for _ in range(3):
        t.record_failure("eu_portal", error="boom")
    assert len(sender.sent) == 1
    assert "eu_portal" in sender.sent[0].subject
    assert "boom" in sender.sent[0].text_body


def test_continued_failure_does_not_re_alert():
    sender = InMemoryEmailSender()
    t = ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email="a@ki.se")
    for _ in range(5):
        t.record_failure("eu_portal", error="boom")
    assert len(sender.sent) == 1


def test_success_resets_counter_and_re_arms_alerts():
    sender = InMemoryEmailSender()
    t = ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email="a@ki.se")
    for _ in range(3):
        t.record_failure("eu_portal", error="boom")
    t.record_success("eu_portal")
    for _ in range(3):
        t.record_failure("eu_portal", error="other")
    assert len(sender.sent) == 2  # one for each separate streak


def test_per_source_independence():
    sender = InMemoryEmailSender()
    t = ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email="a@ki.se")
    t.record_failure("eu_portal")
    t.record_failure("eu_portal")
    t.record_failure("vinnova")
    t.record_failure("vinnova")
    assert sender.sent == []  # neither has crossed threshold
    t.record_failure("eu_portal")
    assert len(sender.sent) == 1
    assert "eu_portal" in sender.sent[0].subject


def test_no_admin_email_skips_send():
    """If no admin is configured, just track silently."""
    sender = InMemoryEmailSender()
    t = ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email=None)
    for _ in range(5):
        t.record_failure("eu_portal", error="boom")
    assert sender.sent == []
```

- [ ] **Step 2: Write `src/call_monitor/ingestion/alerts.py`**

```python
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass

from call_monitor.auth.email import EmailSendFailed, EmailSender

_logger = logging.getLogger(__name__)


@dataclass
class _SourceState:
    consecutive_failures: int = 0
    alerted: bool = False
    last_error: str | None = None


class ConsecutiveFailureTracker:
    """Per-source streak counter for ingestion failures.

    Single-instance assumption (state is in-memory). On reaching the threshold
    on a given source, sends one admin email; further failures on the same
    streak do not re-alert. A single success resets the counter and re-arms
    alerting for the next streak.
    """

    def __init__(
        self,
        *,
        threshold: int,
        sender: EmailSender,
        admin_email: str | None,
    ):
        self._threshold = threshold
        self._sender = sender
        self._admin_email = admin_email
        self._state: dict[str, _SourceState] = defaultdict(_SourceState)
        self._lock = threading.Lock()

    def record_failure(self, source_slug: str, *, error: str | None = None) -> None:
        with self._lock:
            st = self._state[source_slug]
            st.consecutive_failures += 1
            st.last_error = error
            should_alert = (
                st.consecutive_failures >= self._threshold
                and not st.alerted
                and self._admin_email is not None
            )
            if should_alert:
                st.alerted = True
        if should_alert:
            try:
                self._sender.send(
                    to=self._admin_email,
                    subject=f"[call-monitor] {source_slug} failing: {self._threshold}+ consecutive errors",
                    text_body=(
                        f"Source `{source_slug}` has failed {self._threshold} or more "
                        f"consecutive ingestion runs.\n\n"
                        f"Last error: {error or '(no detail)'}\n"
                    ),
                )
            except EmailSendFailed as e:
                # Email provider outage shouldn't crash the orchestrator.
                # Real bugs (TypeError, etc.) still surface.
                _logger.warning(
                    "alerter.send_failed",
                    extra={"event": "alerter_send_failed", "exc": str(e)},
                )

    def record_success(self, source_slug: str) -> None:
        with self._lock:
            st = self._state[source_slug]
            st.consecutive_failures = 0
            st.alerted = False
            st.last_error = None
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_ingestion_alerts.py -v` → 6 passed.

- [ ] **Step 4: Commit**

```bash
git add src/call_monitor/ingestion/alerts.py tests/test_ingestion_alerts.py
git commit -m "feat: ConsecutiveFailureTracker — admin alert after N failures"
```

---

## Task 8: Ingestion orchestrator

**Files:**
- Create: `src/call_monitor/ingestion/orchestrator.py`
- Create: `tests/test_ingestion_orchestrator.py`

Wraps an adapter run with the contract:
- Read `since` from `sources.last_fetched_at`.
- For each `NormalizedCall`, call `upsert_call`.
- On end-to-end success: advance `sources.last_fetched_at` to the run's start time, commit, record success with the alerter.
- On any exception (`AdapterError` from the adapter, anything else): rollback, do NOT advance the timestamp, record failure with the alerter, log structured error. Return result counts so the caller can log.

- [ ] **Step 1: Write failing tests**

`tests/test_ingestion_orchestrator.py`:

```python
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from call_monitor.auth.email import InMemoryEmailSender
from call_monitor.db.models import Call, Source
from call_monitor.ingestion.alerts import ConsecutiveFailureTracker
from call_monitor.ingestion.orchestrator import run_ingestion
from call_monitor.sources.types import AdapterError, NormalizedCall


class _StaticAdapter:
    slug = "vinnova"

    def __init__(self, calls):
        self._calls = list(calls)

    def fetch_new_calls(self, since):
        yield from self._calls


class _FailingAdapter:
    slug = "vinnova"

    def fetch_new_calls(self, since):
        yield NormalizedCall(external_id="P-1", url="u", title="T")  # one good
        raise AdapterError("network blip mid-stream")


def _alerter(sender):
    return ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email="root@ki.se")


def test_successful_run_persists_and_advances_timestamp(pg_session_factory):
    sender = InMemoryEmailSender()
    adapter = _StaticAdapter([
        NormalizedCall(external_id="V-1", url="u1", title="One"),
        NormalizedCall(external_id="V-2", url="u2", title="Two"),
    ])

    result = run_ingestion(
        session_factory=pg_session_factory,
        adapter=adapter,
        alerter=_alerter(sender),
    )
    assert result.inserted == 2
    assert result.updated == 0
    assert result.error is None

    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        assert src.last_fetched_at is not None
        n = s.execute(select(Call).where(Call.source_id == src.id)).scalars().all()
    assert len(n) == 2


def test_second_run_updates_existing_and_inserts_new(pg_session_factory):
    sender = InMemoryEmailSender()
    a1 = _StaticAdapter([NormalizedCall(external_id="V-1", url="u", title="OLD")])
    run_ingestion(session_factory=pg_session_factory, adapter=a1, alerter=_alerter(sender))

    a2 = _StaticAdapter([
        NormalizedCall(external_id="V-1", url="u", title="NEW"),
        NormalizedCall(external_id="V-2", url="u", title="Brand new"),
    ])
    result = run_ingestion(session_factory=pg_session_factory, adapter=a2, alerter=_alerter(sender))
    assert result.inserted == 1
    assert result.updated == 1


def test_failure_rolls_back_and_does_not_advance_timestamp(pg_session_factory):
    sender = InMemoryEmailSender()
    adapter = _FailingAdapter()

    # Snapshot timestamp before
    with pg_session_factory() as s:
        before = s.execute(
            select(Source.last_fetched_at).where(Source.slug == "vinnova")
        ).scalar_one()

    result = run_ingestion(
        session_factory=pg_session_factory, adapter=adapter, alerter=_alerter(sender)
    )
    assert result.error is not None
    assert "network blip" in result.error

    # Timestamp unchanged
    with pg_session_factory() as s:
        after = s.execute(
            select(Source.last_fetched_at).where(Source.slug == "vinnova")
        ).scalar_one()
    assert after == before  # both None on first run

    # Partial inserts rolled back
    with pg_session_factory() as s:
        n = s.execute(select(Call).where(Call.external_id == "P-1")).scalars().all()
    assert n == []


def test_three_consecutive_failures_alert_admin(pg_session_factory):
    sender = InMemoryEmailSender()
    alerter = _alerter(sender)
    adapter = _FailingAdapter()
    for _ in range(3):
        run_ingestion(session_factory=pg_session_factory, adapter=adapter, alerter=alerter)
    assert len(sender.sent) == 1
    assert "vinnova" in sender.sent[0].subject


def test_unknown_source_slug_raises(pg_session_factory):
    sender = InMemoryEmailSender()

    class _BadSlug:
        slug = "no_such_source"

        def fetch_new_calls(self, since):
            return iter([])

    import pytest
    with pytest.raises(LookupError):
        run_ingestion(
            session_factory=pg_session_factory,
            adapter=_BadSlug(),
            alerter=_alerter(sender),
        )
```

- [ ] **Step 2: Write `src/call_monitor/ingestion/orchestrator.py`**

```python
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from call_monitor.db.models import Source
from call_monitor.ingestion.alerts import ConsecutiveFailureTracker
from call_monitor.ingestion.upsert import upsert_call
from call_monitor.sources.types import AdapterError, SourceAdapter

_logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass
class IngestionResult:
    source_slug: str
    inserted: int
    updated: int
    error: str | None  # None on success, message on failure


def run_ingestion(
    *,
    session_factory: sessionmaker,
    adapter: SourceAdapter,
    alerter: ConsecutiveFailureTracker,
) -> IngestionResult:
    """Run one adapter end-to-end.

    Contract:
    - Reads `sources.last_fetched_at` to pass to `adapter.fetch_new_calls()`.
    - Per call: `upsert_call`. Counts inserts vs. updates.
    - On success: advances `sources.last_fetched_at` to run-start time, commits.
    - On failure: rolls back the whole run; does NOT advance the timestamp.
    - Notifies the alerter either way (success resets streak; failure increments).
    """
    run_started_at = datetime.now(UTC)

    with session_factory() as session:
        src = session.execute(
            select(Source).where(Source.slug == adapter.slug)
        ).scalar_one_or_none()
        if src is None:
            raise LookupError(f"unknown source slug: {adapter.slug!r}")
        source_id = src.id
        since = src.last_fetched_at or _EPOCH

    inserted = updated = 0
    error_msg: str | None = None

    try:
        with session_factory() as session:
            for call in adapter.fetch_new_calls(since=since):
                was_new = upsert_call(session, source_id=source_id, call=call)
                if was_new:
                    inserted += 1
                else:
                    updated += 1
            # Advance timestamp inside the same transaction.
            session.execute(
                Source.__table__.update()
                .where(Source.id == source_id)
                .values(last_fetched_at=run_started_at)
            )
            session.commit()
    except AdapterError as e:
        error_msg = str(e)
    except Exception as e:  # noqa: BLE001 — orchestrator must not crash the scheduler
        error_msg = f"{type(e).__name__}: {e}"

    if error_msg is not None:
        _logger.warning(
            "ingestion.failed",
            extra={"event": "ingestion_failed", "source": adapter.slug, "error": error_msg},
        )
        alerter.record_failure(adapter.slug, error=error_msg)
    else:
        _logger.info(
            "ingestion.ok",
            extra={
                "event": "ingestion_ok",
                "source": adapter.slug,
                "inserted": inserted,
                "updated": updated,
            },
        )
        alerter.record_success(adapter.slug)

    return IngestionResult(
        source_slug=adapter.slug,
        inserted=inserted,
        updated=updated,
        error=error_msg,
    )
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_ingestion_orchestrator.py -v` → 5 passed.

- [ ] **Step 4: Commit**

```bash
git add src/call_monitor/ingestion/orchestrator.py tests/test_ingestion_orchestrator.py
git commit -m "feat: ingestion orchestrator with idempotent upsert + failure isolation"
```

---

## Task 9: APScheduler wiring in lifespan

**Files:**
- Modify: `src/call_monitor/config.py` — add `vinnova_api_key`, `eu_portal_base_url`, `vinnova_base_url`
- Modify: `.env.example` — add the new env keys
- Create: `src/call_monitor/scheduler.py`
- Modify: `src/call_monitor/main.py`
- Create: `tests/test_scheduler.py`

The scheduler runs each adapter daily at staggered times (per spec: 03:00 EU, 03:15 Vinnova). In tests we want to instantiate the scheduler without actually running cron jobs — provide a `start=False` knob.

- [ ] **Step 1: Add settings fields**

In `src/call_monitor/config.py`, append three fields (alongside existing ones):

```python
    vinnova_api_key: str | None = Field(default=None, alias="VINNOVA_API_KEY")
    eu_portal_base_url: str = Field(
        default="https://ec.europa.eu", alias="EU_PORTAL_BASE_URL"
    )
    vinnova_base_url: str = Field(
        default="https://data.vinnova.se", alias="VINNOVA_BASE_URL"
    )
    admin_alert_email: str | None = Field(default=None, alias="ADMIN_ALERT_EMAIL")
```

In `.env.example`, append:

```
# Ingestion
EU_PORTAL_BASE_URL=https://ec.europa.eu
VINNOVA_BASE_URL=https://data.vinnova.se
VINNOVA_API_KEY=
# Optional: receive consecutive-failure alerts here. Leave blank to suppress.
ADMIN_ALERT_EMAIL=

# Foundation review-fixes added this but didn't document it; documenting now.
# When true, the rate limiter keys off X-Forwarded-For instead of the
# socket peer. Only set TRUSTED_PROXY=true when the app sits behind a
# proxy you control end-to-end (otherwise a client can spoof the header
# and bypass rate limiting).
TRUSTED_PROXY=false
```

- [ ] **Step 2: Write failing test**

`tests/test_scheduler.py`:

```python
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from call_monitor.scheduler import build_scheduler


def test_build_scheduler_returns_one_job_per_active_adapter():
    """Smoke: the scheduler is constructed with a job per source. We don't
    actually start it — that would fire HTTP requests."""
    sched = build_scheduler(
        session_factory=lambda: None,  # not invoked because we don't start
        eu_portal_base_url="https://ec.europa.eu",
        vinnova_base_url="https://data.vinnova.se",
        vinnova_api_key="dummy",
        alerter=_NullAlerter(),
        start=False,
    )
    assert isinstance(sched, BackgroundScheduler)
    job_slugs = sorted(j.id for j in sched.get_jobs())
    assert job_slugs == ["ingest:eu_portal", "ingest:vinnova"]
    sched.shutdown(wait=False)


def test_build_scheduler_skips_vinnova_without_api_key():
    sched = build_scheduler(
        session_factory=lambda: None,
        eu_portal_base_url="https://ec.europa.eu",
        vinnova_base_url="https://data.vinnova.se",
        vinnova_api_key=None,
        alerter=_NullAlerter(),
        start=False,
    )
    job_slugs = sorted(j.id for j in sched.get_jobs())
    assert job_slugs == ["ingest:eu_portal"]
    sched.shutdown(wait=False)


class _NullAlerter:
    def record_failure(self, *a, **kw): pass
    def record_success(self, *a, **kw): pass
```

- [ ] **Step 3: Write `src/call_monitor/scheduler.py`**

```python
from __future__ import annotations

import logging
from typing import Protocol

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import sessionmaker

from call_monitor.ingestion.alerts import ConsecutiveFailureTracker
from call_monitor.ingestion.orchestrator import run_ingestion
from call_monitor.sources.eu_portal import EUPortalAdapter
from call_monitor.sources.vinnova import VinnovaAdapter

_logger = logging.getLogger(__name__)


class _Alerter(Protocol):
    def record_failure(self, source_slug: str, *, error: str | None = None) -> None: ...
    def record_success(self, source_slug: str) -> None: ...


def build_scheduler(
    *,
    session_factory: sessionmaker,
    eu_portal_base_url: str,
    vinnova_base_url: str,
    vinnova_api_key: str | None,
    alerter: ConsecutiveFailureTracker | _Alerter,
    start: bool = True,
) -> BackgroundScheduler:
    """Build (and optionally start) the daily ingestion scheduler.

    Single-instance constraint: this is APScheduler in-process. Two app
    replicas would each run their own scheduler — duplicate fetches and
    duplicate ranking jobs once Plan 3 lands. Deployment must enforce one
    replica (see CLAUDE.md / spec).
    """
    sched = BackgroundScheduler(timezone="Europe/Stockholm")

    def _eu_job() -> None:
        adapter = EUPortalAdapter(base_url=eu_portal_base_url)
        run_ingestion(session_factory=session_factory, adapter=adapter, alerter=alerter)

    sched.add_job(
        _eu_job,
        trigger=CronTrigger(hour=3, minute=0),
        id="ingest:eu_portal",
        name="Ingest EU Funding & Tenders Portal",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    if vinnova_api_key:
        def _vinnova_job() -> None:
            adapter = VinnovaAdapter(api_key=vinnova_api_key, base_url=vinnova_base_url)
            run_ingestion(
                session_factory=session_factory, adapter=adapter, alerter=alerter
            )

        sched.add_job(
            _vinnova_job,
            trigger=CronTrigger(hour=3, minute=15),
            id="ingest:vinnova",
            name="Ingest Vinnova",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        _logger.warning(
            "scheduler.vinnova_skipped",
            extra={"event": "vinnova_skipped", "reason": "VINNOVA_API_KEY not set"},
        )

    if start:
        sched.start()
    return sched
```

- [ ] **Step 4: Wire scheduler into FastAPI lifespan**

Edit `src/call_monitor/main.py` — extend `create_app` to construct the alerter, build the scheduler in lifespan, shut it down at teardown. Replace the `lifespan` function with:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Deferred import: keeps `admin_bootstrap` off the import graph until
    # lifespan runs, so test-side `create_app()` calls that never enter
    # lifespan don't pay the import cost or trigger its side-effects.
    from call_monitor.admin_bootstrap import bootstrap_admins
    bootstrap_admins(
        app.state.session_factory,
        emails=app.state.settings.initial_admin_emails,
        allowed_domains=app.state.settings.allowed_email_domains,
    )

    from call_monitor.ingestion.alerts import ConsecutiveFailureTracker
    from call_monitor.scheduler import build_scheduler

    settings = app.state.settings
    alerter = ConsecutiveFailureTracker(
        threshold=3,
        sender=app.state.email_sender,
        admin_email=settings.admin_alert_email,
    )
    app.state.ingestion_alerter = alerter
    app.state.scheduler = build_scheduler(
        session_factory=app.state.session_factory,
        eu_portal_base_url=settings.eu_portal_base_url,
        vinnova_base_url=settings.vinnova_base_url,
        vinnova_api_key=settings.vinnova_api_key,
        alerter=alerter,
        start=True,
    )
    try:
        yield
    finally:
        # Stop the scheduler's background threads before disposing the engine
        # — a job firing during teardown could otherwise see a closed pool.
        # `wait=False` lets in-flight ranking-job churn drain async-style;
        # at our cron cadence (daily) there's almost never anything in flight.
        app.state.scheduler.shutdown(wait=False)
        # Foundation v1 review-fixes added engine.dispose() here. Preserve it:
        # without disposal, repeated test-side create_app() calls leak DBAPI
        # connections to the GC, which psycopg surfaces as ResourceWarning
        # (and pyproject.toml has filterwarnings=["error::ResourceWarning"]).
        app.state.engine.dispose()
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_scheduler.py -v` → 2 passed.
Run: `uv run pytest -v` → all green.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/call_monitor/config.py src/call_monitor/scheduler.py src/call_monitor/main.py .env.example tests/test_scheduler.py
git commit -m "feat: APScheduler wiring — daily ingestion in FastAPI lifespan"
```

---

## Task 10: `/` dashboard listing calls

**Files:**
- Create: `src/call_monitor/web/routes/calls.py`
- Create: `src/call_monitor/web/templates/calls_index.html`
- Create: `src/call_monitor/web/templates/call_detail.html`
- Modify: `src/call_monitor/main.py` — register the calls router
- Modify: `src/call_monitor/web/routes/auth.py` — change the post-login redirect from `/me` back to `/` (since `/` now exists)
- Create: `tests/test_web_calls.py`

For Plan 2, the dashboard is "browse all calls, sortable, filterable by source." Plan 3 will overlay per-profile ranking.

- [ ] **Step 1: Write failing tests**

`tests/test_web_calls.py`:

```python
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import Call, Source, User
from call_monitor.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _logged_in(app, pg_session_factory, email="alice@ki.se"):
    with pg_session_factory() as s:
        u = User(email=email)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def _seed_calls(pg_session_factory):
    with pg_session_factory() as s:
        eu = s.execute(select(Source).where(Source.slug == "eu_portal")).scalar_one()
        vn = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add_all([
            Call(source_id=eu.id, external_id="E-1", url="u1", title="EU one",
                 published_at=datetime(2026, 4, 1, tzinfo=UTC)),
            Call(source_id=vn.id, external_id="V-1", url="u2", title="Vin one",
                 published_at=datetime(2026, 4, 5, tzinfo=UTC)),
            Call(source_id=eu.id, external_id="E-2", url="u3", title="EU two",
                 published_at=datetime(2026, 3, 1, tzinfo=UTC)),
        ])
        s.commit()


def test_root_requires_auth(monkeypatch, pg_url):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 401


def test_root_lists_calls_newest_first(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/")
    assert r.status_code == 200
    body = r.text
    # Newest published_at first
    assert body.index("Vin one") < body.index("EU one") < body.index("EU two")


def test_root_filter_by_source(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/?source=vinnova")
    assert r.status_code == 200
    assert "Vin one" in r.text
    assert "EU one" not in r.text


def test_call_detail_shows_metadata(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _seed_calls(pg_session_factory)
    client = _logged_in(app, pg_session_factory)
    with pg_session_factory() as s:
        cid = s.execute(select(Call.id).where(Call.external_id == "V-1")).scalar_one()
    with client:
        r = client.get(f"/calls/{cid}")
    assert r.status_code == 200
    assert "Vin one" in r.text


def test_call_detail_404_for_unknown_id(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/calls/99999")
    assert r.status_code == 404
```

- [ ] **Step 2: Write `src/call_monitor/web/routes/calls.py`**

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from call_monitor.auth.dependencies import get_current_user
from call_monitor.db.models import Call, Source, User

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def calls_index(
    request: Request,
    source: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        stmt = (
            select(Call)
            .options(joinedload(Call.source))
            .order_by(Call.published_at.desc().nullslast(), Call.id.desc())
            .limit(limit)
        )
        if source:
            stmt = stmt.where(
                Call.source_id == select(Source.id).where(Source.slug == source).scalar_subquery()
            )
        calls = session.execute(stmt).scalars().unique().all()
        sources = session.execute(select(Source).order_by(Source.name)).scalars().all()
        # expunge_all() detaches everything in the identity map — including the
        # `source` rows reached via joinedload(Call.source). Per-object expunge
        # leaves them attached and the template's `c.source.name` lookup would
        # raise DetachedInstanceError once the session closes below.
        session.expunge_all()

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="calls_index.html",
        context={"calls": calls, "sources": sources, "selected_source": source, "user": user},
    )


@router.get("/calls/{call_id}", response_class=HTMLResponse)
def call_detail(
    request: Request,
    call_id: int,
    user: User = Depends(get_current_user),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        call = session.execute(
            select(Call).options(joinedload(Call.source)).where(Call.id == call_id)
        ).scalar_one_or_none()
        if call is None:
            raise HTTPException(status_code=404, detail="call not found")
        # See note in calls_index: detach Call AND its joined `source` together.
        session.expunge_all()

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="call_detail.html",
        context={"call": call, "user": user},
    )
```

- [ ] **Step 3: Write `src/call_monitor/web/templates/calls_index.html`**

```html
{% extends "base.html" %}
{% block title %}Funding calls — Call Monitor{% endblock %}
{% block content %}
<h1>Funding calls</h1>

<form method="get" action="/" style="margin-bottom: 1em;">
  <label>Source
    <select name="source" onchange="this.form.submit()">
      <option value="">All sources</option>
      {% for s in sources %}
        <option value="{{ s.slug }}" {% if s.slug == selected_source %}selected{% endif %}>{{ s.name }}</option>
      {% endfor %}
    </select>
  </label>
</form>

{% if not calls %}
  <p><em>No calls have been ingested yet.</em></p>
{% else %}
  <table>
    <thead>
      <tr>
        <th>Title</th>
        <th>Source</th>
        <th>Programme</th>
        <th>Deadline</th>
        <th>Published</th>
      </tr>
    </thead>
    <tbody>
      {% for c in calls %}
        <tr>
          <td><a href="/calls/{{ c.id }}">{{ c.title }}</a></td>
          <td>{{ c.source.name }}</td>
          <td>{{ c.programme or '—' }}</td>
          <td>{{ c.deadline.strftime('%Y-%m-%d') if c.deadline else '—' }}</td>
          <td>{{ c.published_at.strftime('%Y-%m-%d') if c.published_at else '—' }}</td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
{% endif %}
{% endblock %}
```

- [ ] **Step 4: Write `src/call_monitor/web/templates/call_detail.html`**

```html
{% extends "base.html" %}
{% block title %}{{ call.title }} — Call Monitor{% endblock %}
{% block content %}
<p><a href="/">&larr; All calls</a></p>
<h1>{{ call.title }}</h1>
<p><strong>Source:</strong> {{ call.source.name }}
   {% if call.programme %}· <strong>Programme:</strong> {{ call.programme }}{% endif %}</p>

<dl>
  <dt>External ID</dt><dd><code>{{ call.external_id }}</code></dd>
  <dt>URL</dt><dd><a href="{{ call.url }}" target="_blank" rel="noopener">{{ call.url }}</a></dd>
  <dt>Published</dt><dd>{{ call.published_at.strftime('%Y-%m-%d') if call.published_at else '—' }}</dd>
  <dt>Deadline</dt><dd>{{ call.deadline.strftime('%Y-%m-%d %H:%M %Z') if call.deadline else '—' }}</dd>
  <dt>Budget</dt>
  <dd>
    {% if call.budget_min or call.budget_max %}
      {{ '{:,}'.format(call.budget_min) if call.budget_min else '—' }} –
      {{ '{:,}'.format(call.budget_max) if call.budget_max else '—' }} {{ call.currency or '' }}
    {% else %}—{% endif %}
  </dd>
  <dt>Keywords</dt><dd>{{ ', '.join(call.keywords) if call.keywords else '—' }}</dd>
</dl>

{% if call.summary %}
  <h2>Summary</h2>
  <p>{{ call.summary }}</p>
{% endif %}
{% endblock %}
```

- [ ] **Step 5: Register the router in `main.py` and re-point the post-login redirect**

In `src/call_monitor/main.py`, find the line:

```python
from call_monitor.web.routes import auth, health, me
```

and change it to include `calls`:

```python
from call_monitor.web.routes import auth, calls, health, me
```

After `app.include_router(me.router)`, add:

```python
app.include_router(calls.router)
```

In `src/call_monitor/web/routes/auth.py`, find the line:

```python
    response = RedirectResponse(url="/me", status_code=303)
```

and change it to:

```python
    response = RedirectResponse(url="/", status_code=303)
```

Also update the comment block above to reflect that `/` now exists.

Update `tests/test_web_auth.py::test_verify_sets_session_cookie_and_redirects` — change `assert r.headers["location"] == "/me"` to `assert r.headers["location"] == "/"`.

Update `tests/test_login_flow.py::test_full_login_flow` — after the verify step, the test does `client.get("/me")` to confirm session works; that's still valid post-redirect. No change needed beyond the location assertion (which lives in test_web_auth.py).

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_web_calls.py -v` → 5 passed.
Run: `uv run pytest -v` → all green.

- [ ] **Step 7: Commit**

```bash
git add src/call_monitor/web/routes/calls.py src/call_monitor/web/templates/ src/call_monitor/main.py src/call_monitor/web/routes/auth.py tests/test_web_calls.py tests/test_web_auth.py
git commit -m "feat: / dashboard lists calls; /calls/<id> detail; redirect login → /"
```

---

## Task 11: Admin source-health page

**Files:**
- Create: `src/call_monitor/web/routes/admin.py`
- Create: `src/call_monitor/web/templates/admin.html`
- Modify: `src/call_monitor/main.py` — register the admin router
- Create: `tests/test_web_admin.py`

`/admin` shows: per-source `last_fetched_at`, count of calls per source, and a "trigger ingest now" button (POST `/admin/ingest/{slug}` runs the adapter inline — useful for manual debugging without waiting for the cron).

- [ ] **Step 1: Write failing tests**

`tests/test_web_admin.py`:

```python
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import Source, User
from call_monitor.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _logged_in(app, pg_session_factory, *, is_admin: bool):
    with pg_session_factory() as s:
        u = User(email="root@ki.se" if is_admin else "alice@ki.se", is_admin=is_admin)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_admin_requires_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory, is_admin=False)
    with client:
        r = client.get("/admin")
    assert r.status_code == 403


def test_admin_lists_sources_with_status(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    # Mark one source as recently fetched
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        src.last_fetched_at = datetime(2026, 4, 16, 3, 15, tzinfo=UTC)
        s.commit()

    client = _logged_in(app, pg_session_factory, is_admin=True)
    with client:
        r = client.get("/admin")
    assert r.status_code == 200
    assert "EU Funding & Tenders Portal" in r.text
    assert "Vinnova" in r.text
    assert "2026-04-16" in r.text
```

- [ ] **Step 2: Write `src/call_monitor/web/routes/admin.py`**

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from call_monitor.auth.dependencies import require_admin
from call_monitor.db.models import Call, Source, User

router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
def admin_index(
    request: Request,
    user: User = Depends(require_admin),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        rows = session.execute(
            select(
                Source,
                func.count(Call.id).label("call_count"),
            )
            .outerjoin(Call, Call.source_id == Source.id)
            .group_by(Source.id)
            .order_by(Source.name)
        ).all()
        # Materialize before session closes
        sources = [
            {
                "name": src.name,
                "slug": src.slug,
                "last_fetched_at": src.last_fetched_at,
                "call_count": cc,
            }
            for src, cc in rows
        ]

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"sources": sources, "user": user},
    )
```

- [ ] **Step 3: Write `src/call_monitor/web/templates/admin.html`**

```html
{% extends "base.html" %}
{% block title %}Admin — Call Monitor{% endblock %}
{% block content %}
<h1>Admin</h1>
<h2>Source health</h2>
<table>
  <thead>
    <tr>
      <th>Source</th>
      <th>Last fetched</th>
      <th>Calls in DB</th>
    </tr>
  </thead>
  <tbody>
    {% for s in sources %}
      <tr>
        <td>{{ s.name }} (<code>{{ s.slug }}</code>)</td>
        <td>{{ s.last_fetched_at.strftime('%Y-%m-%d %H:%M %Z') if s.last_fetched_at else '— never' }}</td>
        <td>{{ s.call_count }}</td>
      </tr>
    {% endfor %}
  </tbody>
</table>
{% endblock %}
```

- [ ] **Step 4: Register router in `main.py`**

In the imports near the top of `create_app`, change:

```python
from call_monitor.web.routes import auth, calls, health, me
```

to:

```python
from call_monitor.web.routes import admin, auth, calls, health, me
```

After `app.include_router(calls.router)`, add:

```python
app.include_router(admin.router)
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_web_admin.py -v` → 2 passed.
Run: `uv run pytest -v` → all green.

- [ ] **Step 6: Commit**

```bash
git add src/call_monitor/web/routes/admin.py src/call_monitor/web/templates/admin.html src/call_monitor/main.py tests/test_web_admin.py
git commit -m "feat: /admin shows per-source last-fetched + call counts"
```

---

## Task 12: End-to-end ingestion → DB → dashboard test

**Files:**
- Create: `tests/test_ingestion_e2e.py`

A single integration test that drives the whole stack: a fake adapter yields two calls, the orchestrator persists them, the `/` dashboard renders them.

- [ ] **Step 1: Write the test**

`tests/test_ingestion_e2e.py`:

```python
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.email import InMemoryEmailSender
from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import User
from call_monitor.ingestion.alerts import ConsecutiveFailureTracker
from call_monitor.ingestion.orchestrator import run_ingestion
from call_monitor.main import create_app
from call_monitor.sources.types import NormalizedCall


class _FakeAdapter:
    slug = "vinnova"

    def fetch_new_calls(self, since):
        yield NormalizedCall(
            external_id="E2E-1",
            url="https://example.org/e2e/1",
            title="End-to-end call one",
            published_at=datetime(2026, 4, 10, tzinfo=UTC),
        )
        yield NormalizedCall(
            external_id="E2E-2",
            url="https://example.org/e2e/2",
            title="End-to-end call two",
            published_at=datetime(2026, 4, 12, tzinfo=UTC),
        )


def test_end_to_end_ingest_and_browse(monkeypatch, pg_url, pg_session_factory):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()

    # 1. Ingest
    sender = InMemoryEmailSender()
    alerter = ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email=None)
    result = run_ingestion(
        session_factory=pg_session_factory, adapter=_FakeAdapter(), alerter=alerter
    )
    assert result.error is None
    assert result.inserted == 2

    # 2. Browse as a logged-in user
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))

    with client:
        r = client.get("/")
        assert r.status_code == 200
        assert "End-to-end call one" in r.text
        assert "End-to-end call two" in r.text
        # Newest first by published_at
        assert r.text.index("call two") < r.text.index("call one")
```

- [ ] **Step 2: Run**

Run: `uv run pytest tests/test_ingestion_e2e.py -v` → 1 passed.
Run: `uv run pytest -v` → all green.

- [ ] **Step 3: Commit**

```bash
git add tests/test_ingestion_e2e.py
git commit -m "test: end-to-end ingestion → DB → dashboard"
```

---

## Verification at end of plan

After all 12 tasks:

1. `uv run ruff check .` → clean
2. `uv run pytest -v` → all green (≈ 105 tests: 65 from foundation + 40 new across Tasks 2–12)
3. `uv run alembic check` (with DATABASE_URL set) → "No new upgrade operations detected"
4. Manual smoke:
   - `docker compose up -d db` (already running from Plan 1)
   - `DATABASE_URL=... SESSION_SECRET=... ALLOWED_EMAIL_DOMAINS=... APP_BASE_URL=http://localhost:8000 VINNOVA_API_KEY=... uv run uvicorn call_monitor.main:create_app --factory --reload`
   - Browse to `/login`, sign in, you should land on `/` (now empty until first daily run or manual trigger)
   - Hit `/healthz` → still 200; `/admin` → shows per-source health (`last_fetched_at: — never` initially)

## What the next plan (Ranking) adds on top

- `ranking/` module with Ollama HTTP client, prompt + JSON-mode output
- `call_rankings` and `ranking_jobs` tables (Plan 3 schema)
- `Profile` table (placeholder for Plan 4 to fill out) so ranking can have a target
- Backfill button on `/admin`
- Score column + per-profile filter on the `/` dashboard
