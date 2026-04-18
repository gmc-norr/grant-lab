# Ranking v2 — Plan A: Schema + Scheme-Aware Infrastructure

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the database schema and the read/write/enqueue plumbing for ranking v2's dual-scheme coexistence, while keeping v1 as the only active scheme. After this plan ships, the system structurally supports v1 + v2 rows side-by-side in `call_rankings`, a scheme-aware queue, and a `profiles.ranking_scheme` flag — but the v2 ranking path itself is not implemented (Plan B).

**Architecture:** Single Alembic migration adds `scheme` to `call_rankings` and `ranking_jobs` (joined into PKs and partial-unique indexes), creates `call_ranking_criteria`, and adds `profiles.ranking_scheme` + `profiles.budget_currency`. Every read query against `call_rankings` filters by scheme; every enqueue path passes the profile's current scheme; the worker dispatches by `job.scheme` (raising `NotImplementedError` for `'v2'` since the v2 path doesn't exist yet). v1 end-to-end behavior is preserved.

**Tech Stack:** Python 3.12 · SQLAlchemy 2.x · Alembic · Postgres 16 · FastAPI + Jinja2 (form changes) · `pytest` + `testcontainers[postgres]`

---

## Design decisions (fix-now; push back if you disagree)

- **Scheme is part of `ranking_jobs` identity, not just `call_rankings`.** Per the spec's H1-fix: a queued job's intent must be frozen at enqueue time. Otherwise an admin flipping `profiles.ranking_scheme` mid-flight would silently change what the worker scores. The worker reads `scheme` from the job, never from the profile.
- **Default to `'v1'` everywhere.** Existing rows backfill to `'v1'`. New profiles default to `'v1'` (Plan C will flip the default to `'v2'` after rollout week 4 when v2 ships and is validated).
- **`profiles.ranking_scheme` toggle is admin-only.** Operational/deployment decision, not a per-researcher knob. Edit form gates the radio behind `user.is_admin`.
- **`profiles.budget_currency` is a freeform-but-validated text** column with a small dropdown in the UI: `""` (unknown) / `SEK` / `EUR` / `USD` / `NOK` / `DKK`. Defaults to `NULL` for existing profiles (the v2 budget gate treats NULL as unknown, no penalty — but the column is added in this plan because the gate will exist in Plan B and the migration must precede it).
- **Worker raises `NotImplementedError` on `scheme='v2'`** rather than silently routing to v1. Surfaces the operational mistake immediately as a FAILED job in the dead-letter queue, with a clear "rank_pair_v2 not implemented; flip profile back to v1 or wait for Plan B" message. This guards against admin accident.
- **Read-path filters use `profile.ranking_scheme`.** Each query that currently joins `call_rankings` on `(call_id, profile_id)` adds `AND scheme = :profile_scheme` where `:profile_scheme` is fetched once per request. The `/admin/ranking-compare` view (Plan C) is the only consumer that selects both schemes; it will be implemented later.
- **Single migration revision, single commit per task.** No partial-state intermediates with the schema in a half-applied state.
- **No `call_ranking_criteria` writes in this plan.** The model and table are created so Plan B can land without a second migration, but no code in Plan A reads or writes it. A small smoke test confirms the table accepts inserts.

---

## File structure created or modified by this plan

```
call-monitor/
├── alembic/versions/
│   └── 0006_ranking_v2_schema.py                    # NEW
├── src/call_monitor/
│   ├── db/
│   │   └── models.py                                # extend: scheme on CallRanking + RankingJob;
│   │                                                  new CallRankingCriterion; new Profile fields
│   ├── ranking/
│   │   ├── queue.py                                 # enqueue/claim accept scheme
│   │   ├── backfill.py                              # backfill passes profile.ranking_scheme
│   │   └── worker.py                                # claim returns scheme; dispatch by scheme
│   ├── ingestion/
│   │   └── orchestrator.py                          # per-call enqueue passes profile.ranking_scheme
│   ├── web/
│   │   ├── routes/
│   │   │   ├── calls.py                             # dashboard scheme filter on ranked feed
│   │   │   └── profiles.py                          # edit POST accepts budget_currency + ranking_scheme
│   │   └── templates/
│   │       ├── profile_edit.html                    # currency dropdown + admin-only scheme radio
│   │       └── profile_detail.html                  # render currency next to budget
│   └── digest/
│       └── assemble.py                              # scheme filter on all CallRanking queries
└── tests/
    ├── test_models_ranking_v2_schema.py             # NEW
    ├── test_ranking_queue_scheme.py                 # NEW
    ├── test_ranking_worker_scheme.py                # NEW
    ├── test_ingestion_orchestrator.py               # extend: scheme propagation
    ├── test_web_calls.py (or appropriate)           # extend: scheme filter
    ├── test_web_profiles.py                         # extend: currency + scheme fields
    ├── test_digest_assemble.py                      # extend: scheme filter
    └── test_alembic_v2_round_trip.py                # NEW: migration up + down
```

---

## Task 1: Migration 0006 + SQLAlchemy model updates

**Goal:** apply all schema changes in a single Alembic revision, with matching `models.py` updates so `alembic check` reports no drift.

**Files:**
- Create: `alembic/versions/0006_ranking_v2_schema.py`
- Modify: `src/call_monitor/db/models.py` (extend `CallRanking`, `RankingJob`, `Profile`; add `CallRankingCriterion`)
- Create: `tests/test_models_ranking_v2_schema.py`

- [ ] **Step 1: Failing test `tests/test_models_ranking_v2_schema.py`**

```python
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_monitor.db.models import (
    Call,
    CallRanking,
    CallRankingCriterion,
    Profile,
    ProfileKind,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)


def _seed_pair(s, *, email="u@ki.se"):
    """Seed a (call, profile) pair and return their ids."""
    u = User(email=email)
    s.add(u)
    s.flush()
    p = Profile(
        name=f"P-{email}", kind=ProfileKind.GROUP, description="", structured={
            "areas": [], "budget_min": None, "budget_max": None,
            "trl_min": None, "trl_max": None,
            "must_include": [], "hard_exclusions": [],
        }, created_by=u.id,
    )
    s.add(p)
    s.flush()
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id, external_id=f"E-{email}", url="https://x", title="T",
        published_at=datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    return c.id, p.id


def test_call_rankings_dual_scheme_coexist(pg_session_factory):
    """A (call, profile) pair can have both v1 and v2 rows simultaneously."""
    with pg_session_factory() as s:
        cid, pid = _seed_pair(s)
        s.add(CallRanking(
            call_id=cid, profile_id=pid, scheme="v1",
            aggregate_score=70, rationale="v1 says 70",
            model="gemma4:e4b-it-q4_K_M", prompt_version="v1",
            ranked_at=datetime.now(UTC),
        ))
        s.add(CallRanking(
            call_id=cid, profile_id=pid, scheme="v2",
            aggregate_score=82, rationale="v2 says 82",
            model="gemma4:e4b-it-q4_K_M", prompt_version="v2.0",
            ranked_at=datetime.now(UTC),
        ))
        s.commit()
    with pg_session_factory() as s:
        rows = s.execute(
            select(CallRanking).where(CallRanking.call_id == cid)
        ).scalars().all()
    assert sorted(r.scheme for r in rows) == ["v1", "v2"]


def test_call_rankings_same_scheme_duplicate_rejected(pg_session_factory):
    """The new PK is (call_id, profile_id, scheme) — duplicate within same scheme is still rejected."""
    with pg_session_factory() as s:
        cid, pid = _seed_pair(s)
        s.add(CallRanking(
            call_id=cid, profile_id=pid, scheme="v1",
            aggregate_score=70, rationale="r",
            model="m", prompt_version="v1", ranked_at=datetime.now(UTC),
        ))
        s.commit()
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        s.add(CallRanking(
            call_id=cid, profile_id=pid, scheme="v1",
            aggregate_score=80, rationale="other",
            model="m", prompt_version="v1", ranked_at=datetime.now(UTC),
        ))
        s.commit()


def test_call_rankings_aggregate_reason_and_setwise_offset_default(pg_session_factory):
    with pg_session_factory() as s:
        cid, pid = _seed_pair(s)
        s.add(CallRanking(
            call_id=cid, profile_id=pid, scheme="v1",
            aggregate_score=0, rationale="",
            model="m", prompt_version="v1", ranked_at=datetime.now(UTC),
        ))
        s.commit()
    with pg_session_factory() as s:
        r = s.execute(select(CallRanking)).scalar_one()
    assert r.aggregate_reason is None
    assert r.setwise_offset == 0


def test_ranking_jobs_dual_scheme_active_simultaneously(pg_session_factory):
    """Partial unique index allows v1 + v2 active for the same (call, profile)."""
    with pg_session_factory() as s:
        cid, pid = _seed_pair(s)
        s.add(RankingJob(
            call_id=cid, profile_id=pid, scheme="v1",
            status=RankingJobStatus.PENDING,
        ))
        s.add(RankingJob(
            call_id=cid, profile_id=pid, scheme="v2",
            status=RankingJobStatus.PENDING,
        ))
        s.commit()
    with pg_session_factory() as s:
        jobs = s.execute(
            select(RankingJob).where(RankingJob.call_id == cid)
        ).scalars().all()
    assert sorted(j.scheme for j in jobs) == ["v1", "v2"]


def test_ranking_jobs_same_scheme_active_duplicate_rejected(pg_session_factory):
    """Within the same scheme, only one active job per (call, profile)."""
    with pg_session_factory() as s:
        cid, pid = _seed_pair(s)
        s.add(RankingJob(
            call_id=cid, profile_id=pid, scheme="v1",
            status=RankingJobStatus.PENDING,
        ))
        s.commit()
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        s.add(RankingJob(
            call_id=cid, profile_id=pid, scheme="v1",
            status=RankingJobStatus.PENDING,
        ))
        s.commit()


def test_profile_ranking_scheme_default_v1(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="prof@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="P", kind=ProfileKind.GROUP, description="", structured={},
            created_by=u.id,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
    assert p.ranking_scheme == "v1"
    assert p.budget_currency is None


def test_call_ranking_criteria_round_trip(pg_session_factory):
    """The new table accepts inserts with the documented PK shape."""
    with pg_session_factory() as s:
        cid, pid = _seed_pair(s)
        s.add(CallRankingCriterion(
            call_id=cid, profile_id=pid, scheme="v2",
            criterion="topic_fit", rating="4",
            rating_expected=4.2,
            rationale="aligned",
            quoted_evidence="from the call text",
            ranked_at=datetime.now(UTC),
        ))
        s.commit()
    with pg_session_factory() as s:
        row = s.execute(select(CallRankingCriterion)).scalar_one()
    assert row.criterion == "topic_fit"
    assert row.rating == "4"
    assert row.rating_expected == pytest.approx(4.2)


def test_call_ranking_criteria_pk_includes_criterion(pg_session_factory):
    """Same (call, profile, scheme) can have multiple criteria rows — one per criterion."""
    with pg_session_factory() as s:
        cid, pid = _seed_pair(s)
        s.add(CallRankingCriterion(
            call_id=cid, profile_id=pid, scheme="v2",
            criterion="topic_fit", rating="3",
            ranked_at=datetime.now(UTC),
        ))
        s.add(CallRankingCriterion(
            call_id=cid, profile_id=pid, scheme="v2",
            criterion="eligibility", rating="pass",
            ranked_at=datetime.now(UTC),
        ))
        s.commit()
    with pg_session_factory() as s:
        rows = s.execute(select(CallRankingCriterion)).scalars().all()
    assert {r.criterion for r in rows} == {"topic_fit", "eligibility"}
```

- [ ] **Step 2: Run — expect ImportError**

```bash
uv run pytest tests/test_models_ranking_v2_schema.py -v
```

Expected: `ImportError` on `CallRankingCriterion` (model doesn't exist yet).

- [ ] **Step 3: Update `src/call_monitor/db/models.py`**

In the `CallRanking` class, change the PK and add the new columns. The class currently looks like:

```python
class CallRanking(Base):
    __tablename__ = "call_rankings"
    __table_args__ = (
        Index(
            "ix_call_rankings_profile_score",
            "profile_id",
            text("score DESC NULLS LAST"),
            postgresql_using="btree",
        ),
    )

    call_id: Mapped[int] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), primary_key=True
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True
    )
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(20), nullable=False)
    raw_response: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sample_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    ranked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

Replace it with:

```python
class CallRanking(Base):
    __tablename__ = "call_rankings"
    __table_args__ = (
        # New composite index covers the dominant access pattern
        # WHERE profile_id=? AND scheme=? ORDER BY aggregate_score DESC.
        # Without scheme in the key, dual-scheme storage forces the planner
        # to scan both schemes for every dashboard or digest query.
        Index(
            "ix_call_rankings_profile_scheme_score",
            "profile_id",
            "scheme",
            text("aggregate_score DESC NULLS LAST"),
            postgresql_using="btree",
        ),
    )

    call_id: Mapped[int] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), primary_key=True
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True
    )
    # 'v1' | 'v2' — joins the PK so dual-scheme rows coexist for the same pair.
    scheme: Mapped[str] = mapped_column(
        String(8), primary_key=True, nullable=False, server_default=text("'v1'")
    )
    # Renamed from `score` to `aggregate_score` to align with v2 vocabulary.
    # Same semantics: 0-100 final ranking number.
    aggregate_score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # Why the score is what it is, when a gate short-circuited.
    # Values: 'timeline' | 'budget' | 'trl' | 'eligibility' | 'exclusion' | NULL.
    aggregate_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Setwise offset (Plan C phase 2). 0 by default; final displayed score
    # is aggregate_score + setwise_offset clamped to [0, 100].
    setwise_offset: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default=text("0")
    )
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(20), nullable=False)
    raw_response: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sample_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    ranked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

In the `RankingJob` class, add the `scheme` column (joining the partial unique index):

```python
class RankingJob(Base):
    __tablename__ = "ranking_jobs"
    __table_args__ = (
        Index(
            "uq_ranking_jobs_active_pair",
            "call_id",
            "profile_id",
            "scheme",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[int] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    # 'v1' | 'v2' — frozen at enqueue time; worker reads this, NOT the profile.
    scheme: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default=text("'v1'")
    )
    status: Mapped[RankingJobStatus] = mapped_column(
        Enum(
            RankingJobStatus,
            name="ranking_job_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        server_default=text("'pending'"),
    )
    attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )
```

In the `Profile` class, append two columns:

```python
    ranking_scheme: Mapped[str] = mapped_column(
        String(8), nullable=False, default="v1", server_default=text("'v1'")
    )
    # ISO 4217 (e.g. 'SEK', 'EUR', 'USD'). NULL = unknown — the v2 budget
    # gate treats NULL as 'unknown' (no penalty).
    budget_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
```

Append a new model class at the bottom of the models file (after `Subscription` from Plan 4):

```python
class CallRankingCriterion(Base):
    __tablename__ = "call_ranking_criteria"

    call_id: Mapped[int] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), primary_key=True
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True
    )
    scheme: Mapped[str] = mapped_column(
        String(8), primary_key=True, nullable=False
    )
    # 'topic_fit' | 'eligibility' | 'exclusions' | 'timeline' | 'budget' | 'trl'
    criterion: Mapped[str] = mapped_column(String(20), primary_key=True)
    # Canonical string. e.g. "5", "pass", "fail", "unclear".
    rating: Mapped[str] = mapped_column(String(20), nullable=False)
    # G-Eval expected value for Likert criteria (float in [0, 5]); NULL otherwise.
    rating_expected: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    quoted_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    ranked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

Imports at the top of `models.py`: `Float` from `sqlalchemy` (the rest — `String`, `Text`, `SmallInteger`, `BigInteger`, `Index`, `Enum`, `text`, `func`, `JSONB`, `Mapped`, `mapped_column`, `ForeignKey`, `DateTime` — are already imported from prior plans).

- [ ] **Step 4: Migration `alembic/versions/0006_ranking_v2_schema.py`**

```python
"""ranking v2: dual-scheme schema, criterion table, profile additions

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-17
"""

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- call_rankings: add scheme + new columns, swap PK + index ---
    op.add_column(
        "call_rankings",
        sa.Column("scheme", sa.String(length=8), nullable=False, server_default=sa.text("'v1'")),
    )
    op.add_column(
        "call_rankings",
        sa.Column("aggregate_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "call_rankings",
        sa.Column(
            "setwise_offset",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    # Rename `score` to `aggregate_score` so the v2 vocabulary applies uniformly.
    op.alter_column("call_rankings", "score", new_column_name="aggregate_score")

    # PK swap: drop the old (call_id, profile_id) and create (call_id, profile_id, scheme).
    op.drop_constraint("pk_call_rankings", "call_rankings", type_="primary")
    op.create_primary_key(
        "pk_call_rankings", "call_rankings", ["call_id", "profile_id", "scheme"]
    )

    # Index swap: drop the old single-column index, create the composite one.
    op.drop_index("ix_call_rankings_profile_score", table_name="call_rankings")
    op.create_index(
        "ix_call_rankings_profile_scheme_score",
        "call_rankings",
        ["profile_id", "scheme", sa.text("aggregate_score DESC NULLS LAST")],
        postgresql_using="btree",
    )

    # --- ranking_jobs: add scheme, swap partial unique index ---
    op.add_column(
        "ranking_jobs",
        sa.Column("scheme", sa.String(length=8), nullable=False, server_default=sa.text("'v1'")),
    )
    op.drop_index("uq_ranking_jobs_active_pair", table_name="ranking_jobs")
    op.create_index(
        "uq_ranking_jobs_active_pair",
        "ranking_jobs",
        ["call_id", "profile_id", "scheme"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )

    # --- profiles: ranking_scheme + budget_currency ---
    op.add_column(
        "profiles",
        sa.Column(
            "ranking_scheme",
            sa.String(length=8),
            nullable=False,
            server_default=sa.text("'v1'"),
        ),
    )
    op.add_column(
        "profiles",
        sa.Column("budget_currency", sa.String(length=3), nullable=True),
    )

    # --- new table: call_ranking_criteria ---
    op.create_table(
        "call_ranking_criteria",
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column("profile_id", sa.BigInteger(), nullable=False),
        sa.Column("scheme", sa.String(length=8), nullable=False),
        sa.Column("criterion", sa.String(length=20), nullable=False),
        sa.Column("rating", sa.String(length=20), nullable=False),
        sa.Column("rating_expected", sa.Float(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("quoted_evidence", sa.Text(), nullable=True),
        sa.Column(
            "ranked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint(
            "call_id", "profile_id", "scheme", "criterion",
            name="pk_call_ranking_criteria",
        ),
        sa.ForeignKeyConstraint(
            ["call_id"], ["calls.id"], ondelete="CASCADE",
            name="fk_call_ranking_criteria_call",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["profiles.id"], ondelete="CASCADE",
            name="fk_call_ranking_criteria_profile",
        ),
    )


def downgrade() -> None:
    """Reverse with documented data loss.

    Once dual-scheme rows have been written, restoring the old PK requires
    deleting one scheme. We delete v2 and keep v1 — the spec's documented
    point of no return for v2 ranking data. Snapshot the table before
    running this if v2 results need to be preserved for post-mortem.
    """
    # Drop the new table first (no dependency complications).
    op.drop_table("call_ranking_criteria")

    # Destroy v2 rows in call_rankings and ranking_jobs.
    op.execute("DELETE FROM call_rankings WHERE scheme = 'v2'")
    op.execute("DELETE FROM ranking_jobs WHERE scheme = 'v2'")

    # ranking_jobs: restore original partial unique index.
    op.drop_index("uq_ranking_jobs_active_pair", table_name="ranking_jobs")
    op.create_index(
        "uq_ranking_jobs_active_pair",
        "ranking_jobs",
        ["call_id", "profile_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    op.drop_column("ranking_jobs", "scheme")

    # profiles: drop new columns.
    op.drop_column("profiles", "budget_currency")
    op.drop_column("profiles", "ranking_scheme")

    # call_rankings: restore index + PK + column name.
    op.drop_index("ix_call_rankings_profile_scheme_score", table_name="call_rankings")
    op.create_index(
        "ix_call_rankings_profile_score",
        "call_rankings",
        ["profile_id", sa.text("score DESC NULLS LAST")],
        postgresql_using="btree",
    )
    op.drop_constraint("pk_call_rankings", "call_rankings", type_="primary")
    op.create_primary_key(
        "pk_call_rankings", "call_rankings", ["call_id", "profile_id"]
    )
    op.alter_column("call_rankings", "aggregate_score", new_column_name="score")
    op.drop_column("call_rankings", "setwise_offset")
    op.drop_column("call_rankings", "aggregate_reason")
    op.drop_column("call_rankings", "scheme")
```

- [ ] **Step 5: Apply, check, run new tests**

```bash
DATABASE_URL="postgresql+psycopg://call_monitor:call_monitor@127.0.0.1:5432/call_monitor" uv run alembic upgrade head
DATABASE_URL="postgresql+psycopg://call_monitor:call_monitor@127.0.0.1:5432/call_monitor" uv run alembic check
uv run pytest tests/test_models_ranking_v2_schema.py -v   # 8 passed
```

If `alembic check` reports drift, the model and migration are out of sync — fix and re-run.

- [ ] **Step 6: Verify existing tests still pass — they will need updates**

```bash
uv run pytest -v 2>&1 | tail -15
```

Many existing tests reference `CallRanking.score` (now `aggregate_score`). Find them:

```bash
uv run python -c "import call_monitor.db.models" 2>&1
```

(if this errors, fix imports first)

Then grep:

```bash
grep -rn "CallRanking.*score" src/call_monitor tests --include="*.py"
```

Update each call site:
- `CallRanking.score` → `CallRanking.aggregate_score`
- `CallRanking(..., score=...)` constructor calls → `CallRanking(..., aggregate_score=...)`
- Any `.score` attribute reads on a CallRanking instance → `.aggregate_score`

Likely files needing updates: `src/call_monitor/digest/assemble.py`, `src/call_monitor/ranking/worker.py`, several test files. Update all of them in this same task — the rename is atomic and the suite must stay green.

- [ ] **Step 7: Commit**

```bash
uv run pytest -v   # all 300+ tests pass
uv run ruff check .
DATABASE_URL="postgresql+psycopg://call_monitor:call_monitor@127.0.0.1:5432/call_monitor" uv run alembic check
git add alembic/versions/0006_ranking_v2_schema.py src/call_monitor/db/models.py src/call_monitor/digest/assemble.py src/call_monitor/ranking/worker.py tests/
git commit -m "feat: ranking v2 schema — dual-scheme storage + criterion table"
```

---

## Task 2: Scheme-aware enqueue (queue + backfill + orchestrator)

**Files:**
- Modify: `src/call_monitor/ranking/queue.py` (`enqueue_for_pair` accepts `scheme`)
- Modify: `src/call_monitor/ranking/backfill.py` (`backfill_profile` reads `profile.ranking_scheme`)
- Modify: `src/call_monitor/ingestion/orchestrator.py` (per-call enqueue passes `profile.ranking_scheme`)
- Create: `tests/test_ranking_queue_scheme.py`
- Modify: `tests/test_ingestion_orchestrator.py` (extend with scheme-propagation test)

- [ ] **Step 1: Failing test `tests/test_ranking_queue_scheme.py`**

```python
from datetime import UTC, datetime

from sqlalchemy import select

from call_monitor.db.models import (
    Call,
    Profile,
    ProfileKind,
    RankingJob,
    Source,
    User,
)
from call_monitor.ranking.queue import enqueue_for_pair


def _seed(s, *, profile_scheme="v1"):
    u = User(email=f"u-{profile_scheme}@ki.se")
    s.add(u)
    s.flush()
    p = Profile(
        name=f"P-{profile_scheme}", kind=ProfileKind.GROUP, description="",
        structured={}, created_by=u.id, ranking_scheme=profile_scheme,
    )
    s.add(p)
    s.flush()
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id, external_id=f"E-{profile_scheme}", url="https://x", title="T",
        published_at=datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    return c.id, p.id


def test_enqueue_for_pair_scheme_v1_creates_v1_job(pg_session_factory):
    with pg_session_factory() as s:
        cid, pid = _seed(s, profile_scheme="v1")
        s.commit()
    with pg_session_factory() as s:
        created = enqueue_for_pair(s, call_id=cid, profile_id=pid, scheme="v1")
        s.commit()
    assert created is True
    with pg_session_factory() as s:
        job = s.execute(select(RankingJob)).scalar_one()
    assert job.scheme == "v1"


def test_enqueue_for_pair_scheme_v2_creates_v2_job(pg_session_factory):
    with pg_session_factory() as s:
        cid, pid = _seed(s, profile_scheme="v2")
        s.commit()
    with pg_session_factory() as s:
        created = enqueue_for_pair(s, call_id=cid, profile_id=pid, scheme="v2")
        s.commit()
    assert created is True
    with pg_session_factory() as s:
        job = s.execute(select(RankingJob)).scalar_one()
    assert job.scheme == "v2"


def test_enqueue_for_pair_idempotent_within_same_scheme(pg_session_factory):
    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.commit()
    with pg_session_factory() as s:
        first = enqueue_for_pair(s, call_id=cid, profile_id=pid, scheme="v1")
        second = enqueue_for_pair(s, call_id=cid, profile_id=pid, scheme="v1")
        s.commit()
    assert first is True
    assert second is False  # ON CONFLICT DO NOTHING
    with pg_session_factory() as s:
        rows = s.execute(select(RankingJob)).scalars().all()
    assert len(rows) == 1


def test_enqueue_for_pair_v1_and_v2_can_coexist_active(pg_session_factory):
    """Same (call, profile) can have both schemes active simultaneously —
    Plan C's compare mode depends on this."""
    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.commit()
    with pg_session_factory() as s:
        a = enqueue_for_pair(s, call_id=cid, profile_id=pid, scheme="v1")
        b = enqueue_for_pair(s, call_id=cid, profile_id=pid, scheme="v2")
        s.commit()
    assert a is True and b is True
    with pg_session_factory() as s:
        schemes = sorted(j.scheme for j in s.execute(select(RankingJob)).scalars().all())
    assert schemes == ["v1", "v2"]
```

- [ ] **Step 2: Run — expect TypeError on missing `scheme` kwarg**

```bash
uv run pytest tests/test_ranking_queue_scheme.py -v
```

Expected: each test fails with `TypeError: enqueue_for_pair() got an unexpected keyword argument 'scheme'`.

- [ ] **Step 3: Update `src/call_monitor/ranking/queue.py`**

Find the existing `enqueue_for_pair` function. The signature was:

```python
def enqueue_for_pair(session: Session, *, call_id: int, profile_id: int) -> bool:
```

Change to:

```python
def enqueue_for_pair(
    session: Session, *, call_id: int, profile_id: int, scheme: str
) -> bool:
    """Enqueue a ranking job for the (call, profile, scheme) triple.

    Idempotent: ON CONFLICT DO NOTHING against the partial unique index
    on (call_id, profile_id, scheme) WHERE status IN ('pending', 'running').
    Returns True if a row was inserted, False if a same-scheme active job
    already existed."""
    result = session.execute(
        pg_insert(RankingJob)
        .values(
            call_id=call_id,
            profile_id=profile_id,
            scheme=scheme,
            status=RankingJobStatus.PENDING,
        )
        .on_conflict_do_nothing(index_elements=["call_id", "profile_id", "scheme"])
    )
    return result.inserted_primary_key is not None
```

- [ ] **Step 4: Update `src/call_monitor/ranking/backfill.py`**

The current `backfill_profile` does an `INSERT ... SELECT` from `calls` into `ranking_jobs`. Update the SELECT to include `profile.ranking_scheme` in the inserted row, and the conflict target to include `scheme`.

Find the existing function (likely a single SQL statement). Change:

```sql
INSERT INTO ranking_jobs (call_id, profile_id, status)
SELECT c.id, :profile_id, 'pending'
FROM calls c
WHERE c.published_at >= :cutoff
ON CONFLICT (call_id, profile_id) WHERE status IN ('pending', 'running') DO NOTHING
```

To:

```sql
INSERT INTO ranking_jobs (call_id, profile_id, scheme, status)
SELECT c.id, :profile_id, :scheme, 'pending'
FROM calls c
WHERE c.published_at >= :cutoff
ON CONFLICT (call_id, profile_id, scheme)
  WHERE status IN ('pending', 'running') DO NOTHING
```

The Python wrapper reads `profile.ranking_scheme` once and binds it as `:scheme`:

```python
def backfill_profile(*, session_factory, profile_id: int) -> int:
    """Enqueue ranking jobs for the last BACKFILL_DAYS of calls against this
    profile, using the profile's currently-active ranking scheme.

    Idempotent against the partial unique index per (call, profile, scheme)."""
    with session_factory() as session:
        profile = session.get(Profile, profile_id)
        if profile is None or not profile.is_active:
            return 0
        scheme = profile.ranking_scheme
        cutoff = datetime.now(UTC) - timedelta(days=BACKFILL_DAYS)
        result = session.execute(
            text(
                """
                INSERT INTO ranking_jobs (call_id, profile_id, scheme, status)
                SELECT c.id, :profile_id, :scheme, 'pending'
                FROM calls c
                WHERE c.published_at >= :cutoff
                ON CONFLICT (call_id, profile_id, scheme)
                  WHERE status IN ('pending', 'running') DO NOTHING
                """
            ),
            {"profile_id": profile_id, "scheme": scheme, "cutoff": cutoff},
        )
        session.commit()
        return result.rowcount or 0
```

(Add the `Profile` import if not present.)

- [ ] **Step 5: Update `src/call_monitor/ingestion/orchestrator.py`**

The orchestrator iterates new Calls and enqueues a ranking job for each (call, active-profile) pair. Find the per-call enqueue loop. It currently looks like:

```python
for profile in active_profiles:
    enqueue_for_pair(session, call_id=call.id, profile_id=profile.id)
```

Change to:

```python
for profile in active_profiles:
    enqueue_for_pair(
        session,
        call_id=call.id,
        profile_id=profile.id,
        scheme=profile.ranking_scheme,
    )
```

Read the orchestrator carefully — `active_profiles` is loaded once before the iteration; verify the `ranking_scheme` field is populated on those instances (it should be, since they come from a fresh SELECT at orchestrator start).

- [ ] **Step 6: Extend `tests/test_ingestion_orchestrator.py`**

Append a test:

```python
def test_orchestrator_enqueues_jobs_with_profile_ranking_scheme(
    pg_session_factory, profile_factory
):
    """Each profile contributes its currently-active scheme to the enqueued job."""
    from datetime import UTC, datetime
    from sqlalchemy import select
    from call_monitor.db.models import Call, Profile, RankingJob, Source
    from call_monitor.ingestion.orchestrator import run_ingestion
    from unittest.mock import MagicMock

    # Two profiles, one on v1 and one on v2.
    pid_v1, _ = profile_factory(name="P-v1")
    pid_v2, _ = profile_factory(name="P-v2")
    with pg_session_factory() as s:
        s.get(Profile, pid_v2).ranking_scheme = "v2"
        s.commit()

    # Fake adapter yielding one call.
    src_slug = "vinnova"
    class _FakeAdapter:
        slug = src_slug
        def fetch_new_calls(self, since):
            from call_monitor.sources.types import NormalizedCall
            yield NormalizedCall(
                external_id="X-1", url="https://x", title="X",
                summary=None, deadline=None, budget_min=None, budget_max=None,
                currency=None, programme=None, keywords=[],
                published_at=datetime.now(UTC), raw={},
            )

    run_ingestion(
        session_factory=pg_session_factory,
        adapter=_FakeAdapter(),
        alerter=MagicMock(),
    )

    with pg_session_factory() as s:
        jobs = s.execute(select(RankingJob)).scalars().all()
    by_scheme = {(j.profile_id, j.scheme) for j in jobs}
    assert (pid_v1, "v1") in by_scheme
    assert (pid_v2, "v2") in by_scheme
```

- [ ] **Step 7: Verify and commit**

```bash
uv run pytest tests/test_ranking_queue_scheme.py tests/test_ingestion_orchestrator.py -v
uv run pytest -v   # all green
uv run ruff check .
git add src/call_monitor/ranking/queue.py src/call_monitor/ranking/backfill.py src/call_monitor/ingestion/orchestrator.py tests/test_ranking_queue_scheme.py tests/test_ingestion_orchestrator.py
git commit -m "feat: scheme-aware enqueue for ranking jobs"
```

---

## Task 3: Worker dispatches by `job.scheme`

**Files:**
- Modify: `src/call_monitor/ranking/worker.py` (read `job.scheme`, dispatch; raise `NotImplementedError` for `'v2'`)
- Create: `tests/test_ranking_worker_scheme.py`

- [ ] **Step 1: Failing test `tests/test_ranking_worker_scheme.py`**

```python
from datetime import UTC, datetime

from sqlalchemy import select

from call_monitor.db.models import (
    Call,
    CallRanking,
    Profile,
    ProfileKind,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from call_monitor.ranking.rank_pair import RankResult
from call_monitor.ranking.worker import drain_queue


def _seed_pair(s, *, profile_scheme="v1"):
    u = User(email=f"u-{profile_scheme}@ki.se")
    s.add(u); s.flush()
    p = Profile(
        name=f"P-{profile_scheme}", kind=ProfileKind.GROUP, description="",
        structured={}, created_by=u.id, ranking_scheme=profile_scheme,
    )
    s.add(p); s.flush()
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id, external_id=f"E-{profile_scheme}", url="https://x", title="T",
        published_at=datetime.now(UTC),
    )
    s.add(c); s.flush()
    return c.id, p.id


def _enqueue(s, cid, pid, scheme):
    s.add(RankingJob(
        call_id=cid, profile_id=pid, scheme=scheme,
        status=RankingJobStatus.PENDING,
    ))


def test_worker_dispatches_v1_job_to_v1_ranker(pg_session_factory):
    """v1 jobs run rank_pair_v1 and produce a CallRanking with scheme='v1'."""
    with pg_session_factory() as s:
        cid, pid = _seed_pair(s, profile_scheme="v1")
        _enqueue(s, cid, pid, "v1")
        s.commit()

    def _fake_ranker(*, client, profile, call):
        return RankResult(
            score=77, rationale="ok", model="m", prompt_version="v1",
            raw_response={},
        )

    drain_queue(
        session_factory=pg_session_factory, client=None,
        ranker=_fake_ranker, batch_size=10,
    )

    with pg_session_factory() as s:
        job = s.execute(select(RankingJob)).scalar_one()
        ranking = s.execute(select(CallRanking)).scalar_one()
    assert job.status == RankingJobStatus.DONE
    assert ranking.scheme == "v1"
    assert ranking.aggregate_score == 77


def test_worker_marks_v2_job_failed_with_helpful_error(pg_session_factory):
    """v2 jobs raise NotImplementedError → job marked FAILED with a clear message,
    so admins see this in the dead-letter queue rather than a silent v1 fallback."""
    with pg_session_factory() as s:
        cid, pid = _seed_pair(s, profile_scheme="v2")
        _enqueue(s, cid, pid, "v2")
        s.commit()

    drain_queue(
        session_factory=pg_session_factory, client=None,
        ranker=lambda **kw: (_ for _ in ()).throw(AssertionError("v1 ranker called for v2 job")),
        batch_size=10,
    )

    with pg_session_factory() as s:
        job = s.execute(select(RankingJob)).scalar_one()
        rankings = s.execute(select(CallRanking)).scalars().all()
    assert job.status == RankingJobStatus.FAILED
    assert "rank_pair_v2 not implemented" in (job.last_error or "")
    assert rankings == []  # no row written


def test_worker_processes_v1_jobs_alongside_v2_failures(pg_session_factory):
    """A v2 failure does not stop subsequent v1 jobs in the same batch."""
    with pg_session_factory() as s:
        cid_a, pid_a = _seed_pair(s, profile_scheme="v1")
        cid_b, pid_b = _seed_pair(s, profile_scheme="v2")
        _enqueue(s, cid_a, pid_a, "v1")
        _enqueue(s, cid_b, pid_b, "v2")
        s.commit()

    drain_queue(
        session_factory=pg_session_factory, client=None,
        ranker=lambda *, client, profile, call: RankResult(
            score=50, rationale="r", model="m", prompt_version="v1", raw_response={},
        ),
        batch_size=10,
    )

    with pg_session_factory() as s:
        rows = {(j.profile_id, j.scheme): j.status for j in s.execute(select(RankingJob)).scalars().all()}
    assert rows[(pid_a, "v1")] == RankingJobStatus.DONE
    assert rows[(pid_b, "v2")] == RankingJobStatus.FAILED
```

- [ ] **Step 2: Run — expect failures**

```bash
uv run pytest tests/test_ranking_worker_scheme.py -v
```

Expected: tests fail because the worker doesn't currently distinguish schemes (it'll either run the v1 ranker on the v2 job, or the dispatch logic will be missing entirely).

- [ ] **Step 3: Update `src/call_monitor/ranking/worker.py`**

Find the `drain_queue` function. It currently iterates claimed jobs and calls `ranker(client=..., profile=..., call=...)` for each. Add scheme dispatch.

Inside the per-job loop, before the existing `ranker(...)` call, insert:

```python
        if job.scheme != "v1":
            # v2 path is not implemented in this plan. Mark FAILED with a
            # clear message so the admin sees this in the dead-letter queue
            # instead of silently routing to v1.
            mark_failed(
                session,
                job_id=job.id,
                error=(
                    f"rank_pair_v2 not implemented (job scheme={job.scheme!r}); "
                    "flip profiles.ranking_scheme back to 'v1' or wait for Plan B"
                ),
                max_attempts=MAX_ATTEMPTS,
            )
            continue
```

Verify `mark_failed` is the existing helper used elsewhere in worker.py — its signature should already exist from Plan 3.

When the v1 ranker DOES succeed, the resulting CallRanking row must be written with `scheme='v1'`. Find the `merge` / insert call that creates the CallRanking and add `scheme="v1"` to the column list. The merge currently looks like:

```python
session.merge(CallRanking(
    call_id=job.call_id,
    profile_id=job.profile_id,
    aggregate_score=result.score,
    rationale=result.rationale,
    model=result.model,
    prompt_version=result.prompt_version,
    raw_response=result.raw_response,
    ranked_at=datetime.now(UTC),
))
```

Change to:

```python
session.merge(CallRanking(
    call_id=job.call_id,
    profile_id=job.profile_id,
    scheme=job.scheme,                 # NEW
    aggregate_score=result.score,
    rationale=result.rationale,
    model=result.model,
    prompt_version=result.prompt_version,
    raw_response=result.raw_response,
    ranked_at=datetime.now(UTC),
))
```

Note: `aggregate_score` was renamed from `score` in Task 1. Worker should already be using the new name after Task 1 — if not, fix it here.

Also: the `claim_pending_jobs` query in `queue.py` already returns full `RankingJob` ORM rows, so `job.scheme` is accessible without query changes.

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/test_ranking_worker_scheme.py -v   # 3 passed
uv run pytest -v
uv run ruff check .
git add src/call_monitor/ranking/worker.py tests/test_ranking_worker_scheme.py
git commit -m "feat: worker dispatches by job.scheme; v2 → NotImplementedError"
```

---

## Task 4: Dashboard scheme filter

**Files:**
- Modify: `src/call_monitor/web/routes/calls.py` (ranked-feed query)
- Modify: `tests/test_web_calls.py` (or whichever test file owns the dashboard) — add scheme-filter regression

- [ ] **Step 1: Failing test**

Find the existing dashboard test file (likely `tests/test_web_calls.py` or `tests/test_dashboard.py`). Append:

```python
def test_dashboard_ranked_feed_filters_by_profile_ranking_scheme(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """If a (call, profile) pair has both v1 and v2 CallRanking rows, the
    dashboard for that profile must show only the row matching the profile's
    active ranking_scheme."""
    from datetime import UTC, datetime
    from sqlalchemy import select
    from fastapi.testclient import TestClient
    from call_monitor.auth.session_cookie import SessionSigner
    from call_monitor.db.models import Call, CallRanking, Profile, Source, User
    from call_monitor.main import create_app

    pid, _ = profile_factory(name="ProfileX")
    with pg_session_factory() as s:
        # Profile is on v1 by default.
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="DUAL", url="https://x", title="DualScheme",
            published_at=datetime.now(UTC),
        )
        s.add(c); s.flush()
        s.add(CallRanking(
            call_id=c.id, profile_id=pid, scheme="v1",
            aggregate_score=70, rationale="v1 says 70",
            model="m", prompt_version="v1", ranked_at=datetime.now(UTC),
        ))
        s.add(CallRanking(
            call_id=c.id, profile_id=pid, scheme="v2",
            aggregate_score=20, rationale="v2 says 20",
            model="m", prompt_version="v2.0", ranked_at=datetime.now(UTC),
        ))
        s.commit()

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()

    with pg_session_factory() as s:
        u = User(email="dashboard@ki.se")
        s.add(u); s.commit(); s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.get(f"/?profile={pid}")
    assert r.status_code == 200
    # v1 row (score 70) wins because profile.ranking_scheme defaults to 'v1'.
    assert "70" in r.text
    # v2 row (score 20) must NOT appear.
    assert "20" not in r.text
```

- [ ] **Step 2: Run — expect failure (both 70 and 20 likely appear)**

```bash
uv run pytest tests/test_web_calls.py::test_dashboard_ranked_feed_filters_by_profile_ranking_scheme -v
```

Expected: assertion failure on `assert "20" not in r.text` (or duplicated rows in the rendered table).

- [ ] **Step 3: Update `src/call_monitor/web/routes/calls.py`**

Find the dashboard handler (`@router.get("/")`). The ranked branch (`?profile=<id>`) currently joins `CallRanking` on `(call_id, profile_id)`. Add a scheme filter.

Read the file to find the exact join. Pattern:

```python
ranked_stmt = (
    select(Call, CallRanking)
    .join(CallRanking, CallRanking.call_id == Call.id)
    .where(CallRanking.profile_id == profile.id)
    ...
)
```

Add:

```python
    .where(CallRanking.scheme == profile.ranking_scheme)
```

The `.order_by(CallRanking.score.desc())` clause must also be updated to `CallRanking.aggregate_score.desc()` (rename from Task 1).

If the unranked branch (no `?profile=`) does NOT touch `CallRanking`, no change needed there.

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/test_web_calls.py -v
uv run pytest -v
uv run ruff check .
git add src/call_monitor/web/routes/calls.py tests/test_web_calls.py
git commit -m "feat: dashboard ranked feed filters by profile.ranking_scheme"
```

---

## Task 5: Digest assembler scheme filter

**Files:**
- Modify: `src/call_monitor/digest/assemble.py` (every CallRanking query gets a scheme filter)
- Modify: `tests/test_digest_assemble.py` (extend with scheme-filter test)

- [ ] **Step 1: Failing test — append to `tests/test_digest_assemble.py`**

```python
def test_assemble_filters_by_profile_ranking_scheme(pg_session_factory, now):
    """A profile on v1 sees only v1 CallRanking rows, even if v2 rows exist
    for the same (call, profile) pair (e.g. left over from compare-mode runs)."""
    with pg_session_factory() as s:
        uid, pid = _seed_user_with_profile(s)
        # Profile defaults to ranking_scheme='v1'.
        cid = _seed_call(s, title="DualSchemeCall", score=80, profile_id=pid)
        # Same call also has a v2 ranking with a different score.
        s.add(CallRanking(
            call_id=cid, profile_id=pid, scheme="v2",
            aggregate_score=10, rationale="v2",
            model="m", prompt_version="v2.0",
            ranked_at=datetime.now(UTC),
        ))
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=now)
    assert payload is not None
    titles_scores = [
        (e.title, e.score) for sec in payload.sections for e in sec.entries
    ]
    assert ("DualSchemeCall", 80) in titles_scores      # v1 row, score 80
    assert ("DualSchemeCall", 10) not in titles_scores  # v2 row excluded
```

You may need to import `CallRanking` and `datetime` if not already imported at the top of the test file.

Also, `_seed_call` currently inserts a CallRanking without specifying `scheme`. After Task 1, the column has `server_default='v1'` so existing test inserts default to `'v1'` — but to be explicit and unambiguous, update `_seed_call`'s `CallRanking(...)` to include `scheme="v1"`. Same for any other test helper that inserts CallRanking rows.

- [ ] **Step 2: Run — expect failure (the v2 row leaks in or duplicates the entry)**

```bash
uv run pytest tests/test_digest_assemble.py -v
```

- [ ] **Step 3: Update `src/call_monitor/digest/assemble.py`**

Find every `select(CallRanking)` and `select(Call, CallRanking)` query in `assemble_for_user`. There are three of them in the existing code (must-include candidate ranking lookup, top-bucket query, borderline query). Each needs `.where(CallRanking.scheme == profile.ranking_scheme)`.

Example for the top-bucket query:

```python
        top_q = (
            select(Call, CallRanking)
            .join(CallRanking, CallRanking.call_id == Call.id)
            .where(
                CallRanking.profile_id == profile.id,
                CallRanking.scheme == profile.ranking_scheme,    # NEW
                CallRanking.aggregate_score >= DIGEST_TOP_THRESHOLD,
                Call.id.notin_(excluded) if excluded else True,
                or_(Call.deadline.is_(None), Call.deadline > now),
            )
            .order_by(CallRanking.aggregate_score.desc())
            .limit(50)
        )
```

Apply the analogous change to the borderline query and to the must-include per-call ranking lookup.

Note: `CallRanking.score` references must be renamed to `CallRanking.aggregate_score` if Task 1 didn't already catch them. Grep:

```bash
grep -n "CallRanking.score\|CallRanking\.aggregate_score" src/call_monitor/digest/assemble.py
```

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/test_digest_assemble.py -v
uv run pytest -v
uv run ruff check .
git add src/call_monitor/digest/assemble.py tests/test_digest_assemble.py
git commit -m "feat: digest assembler filters CallRanking by profile.ranking_scheme"
```

---

## Task 6: Profile model + form: `budget_currency` field

**Files:**
- Modify: `src/call_monitor/web/routes/profiles.py` (edit POST accepts `budget_currency`)
- Modify: `src/call_monitor/web/templates/profile_edit.html` (currency dropdown)
- Modify: `src/call_monitor/web/templates/profile_detail.html` (render currency next to budget)
- Modify: `tests/test_web_profiles.py` (round-trip test)

- [ ] **Step 1: Failing test — append to `tests/test_web_profiles.py`**

```python
def test_profile_edit_round_trips_budget_currency(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="Currency test")
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "x",
                "areas": "",
                "budget_min": "100000",
                "budget_max": "5000000",
                "budget_currency": "SEK",
                "trl_min": "",
                "trl_max": "",
                "must_include": "",
                "hard_exclusions": "",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    from sqlalchemy import select
    from call_monitor.db.models import Profile
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
    assert p.budget_currency == "SEK"


def test_profile_edit_accepts_empty_currency_as_null(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="No currency")
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "x", "areas": "", "budget_min": "", "budget_max": "",
                "budget_currency": "",
                "trl_min": "", "trl_max": "",
                "must_include": "", "hard_exclusions": "",
            },
            follow_redirects=False,
        )
    from sqlalchemy import select
    from call_monitor.db.models import Profile
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
    assert p.budget_currency is None


def test_profile_detail_renders_currency_when_set(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    from sqlalchemy import select
    from call_monitor.db.models import Profile
    pid, _ = profile_factory(
        name="Display currency",
        structured={"budget_min": 100000, "budget_max": 500000},
    )
    with pg_session_factory() as s:
        s.get(Profile, pid).budget_currency = "EUR"
        s.commit()
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get(f"/profiles/{pid}")
    assert r.status_code == 200
    assert "EUR" in r.text
```

- [ ] **Step 2: Run — expect failure (route doesn't read the field; template doesn't render it)**

- [ ] **Step 3: Update `src/call_monitor/web/routes/profiles.py`**

Find `profile_edit_submit`. Add `budget_currency` as a Form parameter:

```python
@router.post("/profiles/{profile_id}/edit")
def profile_edit_submit(
    request: Request,
    profile_id: int,
    description: str = Form(""),
    areas: str = Form(""),
    budget_min: str = Form(""),
    budget_max: str = Form(""),
    budget_currency: str = Form(""),       # NEW
    trl_min: str = Form(""),
    trl_max: str = Form(""),
    must_include: str = Form(""),
    hard_exclusions: str = Form(""),
    user: User = Depends(get_current_user),  # noqa: B008
):
```

In the body, after the existing field assignments, set the column:

```python
        # Validate currency. Allow only the documented set (or empty → NULL).
        normalized = (budget_currency or "").strip().upper()
        if normalized in {"", "SEK", "EUR", "USD", "NOK", "DKK"}:
            p.budget_currency = normalized or None
        # else: silently ignore (form rendered the dropdown; an attacker
        # crafting a different value gets dropped, not stored)
```

- [ ] **Step 4: Update `src/call_monitor/web/templates/profile_edit.html`**

Find the budget min/max inputs. Add a currency selector right after them:

```html
  <p>
    <label>Budget min <input type="text" name="budget_min" value="{{ '' if profile.structured.budget_min is none else profile.structured.budget_min }}"></label>
    <label>Budget max <input type="text" name="budget_max" value="{{ '' if profile.structured.budget_max is none else profile.structured.budget_max }}"></label>
    <label>Currency
      <select name="budget_currency">
        <option value=""    {% if not profile.budget_currency %}selected{% endif %}>(unknown)</option>
        <option value="SEK" {% if profile.budget_currency == 'SEK' %}selected{% endif %}>SEK</option>
        <option value="EUR" {% if profile.budget_currency == 'EUR' %}selected{% endif %}>EUR</option>
        <option value="USD" {% if profile.budget_currency == 'USD' %}selected{% endif %}>USD</option>
        <option value="NOK" {% if profile.budget_currency == 'NOK' %}selected{% endif %}>NOK</option>
        <option value="DKK" {% if profile.budget_currency == 'DKK' %}selected{% endif %}>DKK</option>
      </select>
    </label>
  </p>
```

- [ ] **Step 5: Update `src/call_monitor/web/templates/profile_detail.html`**

Find the budget rendering block. Append the currency:

```html
  <dt>Budget</dt>
  <dd>
    {% if profile.structured.budget_min is not none or profile.structured.budget_max is not none %}
      {{ profile.structured.budget_min if profile.structured.budget_min is not none else '—' }} – {{ profile.structured.budget_max if profile.structured.budget_max is not none else '—' }}{% if profile.budget_currency %} {{ profile.budget_currency }}{% endif %}
    {% else %}—{% endif %}
  </dd>
```

- [ ] **Step 6: Verify and commit**

```bash
uv run pytest tests/test_web_profiles.py -v
uv run pytest -v
uv run ruff check .
git add src/call_monitor/web/routes/profiles.py src/call_monitor/web/templates/profile_edit.html src/call_monitor/web/templates/profile_detail.html tests/test_web_profiles.py
git commit -m "feat: profile budget_currency form field + display"
```

---

## Task 7: Profile model + form: `ranking_scheme` toggle (admin-only)

**Files:**
- Modify: `src/call_monitor/web/routes/profiles.py` (edit POST honors `ranking_scheme` only when `user.is_admin`)
- Modify: `src/call_monitor/web/templates/profile_edit.html` (radio gated by `user.is_admin`)
- Modify: `tests/test_web_profiles.py` (admin can flip; non-admin cannot)

- [ ] **Step 1: Failing tests — append to `tests/test_web_profiles.py`**

```python
def test_profile_edit_admin_can_flip_ranking_scheme(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    pid, _ = profile_factory(name="Scheme flip")
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", "admin@ki.se")
    from call_monitor.main import create_app
    from call_monitor.auth.session_cookie import SessionSigner
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from call_monitor.db.models import User, Profile
    app = create_app()

    # Trigger admin bootstrap by entering the lifespan briefly.
    with TestClient(app):
        pass
    with pg_session_factory() as s:
        admin = s.execute(select(User).where(User.email == "admin@ki.se")).scalar_one()
        uid = admin.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "x", "areas": "", "budget_min": "", "budget_max": "",
                "budget_currency": "", "trl_min": "", "trl_max": "",
                "must_include": "", "hard_exclusions": "",
                "ranking_scheme": "v2",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
    assert p.ranking_scheme == "v2"


def test_profile_edit_non_admin_cannot_flip_ranking_scheme(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """Non-admin POST with ranking_scheme=v2 is silently ignored — scheme stays v1."""
    pid, _ = profile_factory(name="Scheme protect")
    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)  # creates a non-admin user
    with client:
        r = client.post(
            f"/profiles/{pid}/edit",
            data={
                "description": "x", "areas": "", "budget_min": "", "budget_max": "",
                "budget_currency": "", "trl_min": "", "trl_max": "",
                "must_include": "", "hard_exclusions": "",
                "ranking_scheme": "v2",  # attempted
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    from sqlalchemy import select
    from call_monitor.db.models import Profile
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
    assert p.ranking_scheme == "v1"  # unchanged
```

- [ ] **Step 2: Run — expect failure**

- [ ] **Step 3: Update `src/call_monitor/web/routes/profiles.py`**

Add `ranking_scheme` as a Form parameter:

```python
    ranking_scheme: str = Form(""),
```

In the body, after the budget_currency block:

```python
        # Operational toggle — admin-only. Silently ignore non-admin attempts
        # so a researcher can't accidentally flip a profile via crafted form
        # data; admins use the radio surfaced in the edit template.
        if user.is_admin:
            requested = (ranking_scheme or "").strip().lower()
            if requested in {"v1", "v2"}:
                p.ranking_scheme = requested
```

- [ ] **Step 4: Update `src/call_monitor/web/templates/profile_edit.html`**

At the top of the form (before the description textarea is fine), conditionally render the radio:

```html
{% if user.is_admin %}
  <p>
    <label><strong>Ranking scheme (admin)</strong></label><br>
    <label><input type="radio" name="ranking_scheme" value="v1" {% if profile.ranking_scheme == 'v1' %}checked{% endif %}> v1 (monolithic LLM score)</label>
    <label style="margin-left: 1em"><input type="radio" name="ranking_scheme" value="v2" {% if profile.ranking_scheme == 'v2' %}checked{% endif %}> v2 (criterion-rubric — requires Plan B)</label>
  </p>
{% endif %}
```

The route's GET handler that renders the edit template already passes `user` in the context (verify by reading the existing handler). If not, add it.

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/test_web_profiles.py -v
uv run pytest -v
uv run ruff check .
git add src/call_monitor/web/routes/profiles.py src/call_monitor/web/templates/profile_edit.html tests/test_web_profiles.py
git commit -m "feat: profile ranking_scheme toggle (admin-only)"
```

---

## Task 8: Migration round-trip integration test

**Files:**
- Create: `tests/test_alembic_v2_round_trip.py`

The other tests use `pg_session_factory` which depends on `migrated_engine` (which runs migrations once at fixture setup). This test exercises a full upgrade → downgrade → upgrade cycle to prove the down-migration is reversible.

- [ ] **Step 1: Test**

```python
"""Verify that 0006 can be applied, rolled back, and re-applied without
schema corruption — and that v2 row deletion in the down path is explicit."""
from datetime import UTC, datetime
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text


@pytest.fixture()
def alembic_cfg(pg_url, monkeypatch):
    """Build an Alembic config rooted at the repo's alembic.ini."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_round_trip_upgrade_downgrade_upgrade(pg_url, alembic_cfg):
    """Migration is bidirectional. v1 rows survive a round trip; v2 rows
    are deleted on down (documented behavior)."""
    engine = create_engine(pg_url)
    # We start at head (the pg_session_factory fixture already ran upgrade head).
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ranking_jobs"))
        conn.execute(text("DELETE FROM call_rankings"))
        # Seed: one programme, one profile, one call, one v1 ranking, one v2 ranking.
        conn.execute(text("DELETE FROM subscriptions"))
        conn.execute(text("DELETE FROM user_call_dismissals"))
        conn.execute(text("DELETE FROM digest_runs"))
        conn.execute(text("DELETE FROM call_ranking_criteria"))
        conn.execute(text("DELETE FROM profiles"))
        conn.execute(text("DELETE FROM users"))
        conn.execute(text("DELETE FROM calls"))

        conn.execute(text(
            "INSERT INTO users (email, is_admin, created_at) "
            "VALUES ('rt@ki.se', false, now()) RETURNING id"
        ))
        uid = conn.execute(text("SELECT id FROM users WHERE email = 'rt@ki.se'")).scalar_one()
        conn.execute(text(
            "INSERT INTO profiles (name, kind, description, structured, "
            "                       created_by, ranking_scheme, is_active, "
            "                       created_at, updated_at) "
            "VALUES ('RT', 'group', '', '{}'::jsonb, :uid, 'v1', true, now(), now()) "
            "RETURNING id"
        ), {"uid": uid})
        pid = conn.execute(text("SELECT id FROM profiles WHERE name = 'RT'")).scalar_one()
        sid = conn.execute(text("SELECT id FROM sources WHERE slug = 'vinnova'")).scalar_one()
        conn.execute(text(
            "INSERT INTO calls (source_id, external_id, url, title, "
            "                    keywords, raw, published_at, fetched_at) "
            "VALUES (:sid, 'RT-1', 'https://x', 'RT', '{}', '{}'::jsonb, now(), now()) "
            "RETURNING id"
        ), {"sid": sid})
        cid = conn.execute(text("SELECT id FROM calls WHERE external_id = 'RT-1'")).scalar_one()
        conn.execute(text(
            "INSERT INTO call_rankings (call_id, profile_id, scheme, "
            "                            aggregate_score, rationale, "
            "                            model, prompt_version, ranked_at) "
            "VALUES (:cid, :pid, 'v1', 70, 'v1', 'm', 'v1', now()), "
            "       (:cid, :pid, 'v2', 22, 'v2', 'm', 'v2.0', now())"
        ), {"cid": cid, "pid": pid})

    # Down one revision (0006 → 0005). Should drop v2 rows + new schema.
    command.downgrade(alembic_cfg, "0005")
    with engine.begin() as conn:
        # The v2 row must be gone; v1 row must remain.
        rows = conn.execute(text("SELECT scheme FROM call_rankings")).fetchall()
        # After downgrade the column is dropped — query without scheme:
    # Re-read after schema change:
    with engine.begin() as conn:
        n_rows = conn.execute(text("SELECT count(*) FROM call_rankings")).scalar_one()
    assert n_rows == 1, "expected exactly the v1 row to survive the down-migration"

    # Up again (0005 → 0006). Schema restored; the surviving v1 row picks up
    # scheme='v1' from the server_default.
    command.upgrade(alembic_cfg, "0006")
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT scheme, aggregate_score FROM call_rankings")).fetchall()
    assert rows == [("v1", 70)]
```

- [ ] **Step 2: Run**

```bash
uv run pytest tests/test_alembic_v2_round_trip.py -v
```

Expected: passes once the migration is correct. If it fails, the down-migration has a bug — fix and re-run.

- [ ] **Step 3: Commit**

```bash
git add tests/test_alembic_v2_round_trip.py
git commit -m "test: 0006 migration round-trip with v2 row deletion"
```

---

## Verification at end of plan

1. `uv run ruff check .` → clean
2. `uv run pytest -v` → all 320+ tests green
3. `DATABASE_URL=... uv run alembic check` → "No new upgrade operations detected"
4. Hand-fire a single v1 ranking against the existing live DB to prove end-to-end behavior is preserved (model still produces `scheme='v1'` rows).

## What Plan B picks up

- `criteria.py` (rule-based gate evaluation, currency-aware budget gate)
- `ollama_client.py` logprob support
- `aggregate.py` (G-Eval logprob weighting + aggregation rules)
- `prompt.py` extension (criterion-level builder)
- `rank_pair_v2` + worker dispatch by `job.scheme` (Plan A's worker raises `NotImplementedError` for v2; Plan B replaces that with the real path)
- Sparse `call_ranking_criteria` writes
- Dashboard call-detail criterion breakdown rendering
- Digest one-line criterion summary in render templates
- Golden-set expansion stub

## What Plan C picks up

- `/admin/ranking-compare` (week 2 admin tool)
- Per-scheme queue depth + active-scheme display in `/admin`
- `setwise.py` (phase 2 borderline pass)
- Daily setwise APScheduler job
- Golden-set weekly eval against both schemes (Kendall's tau report)
- Profile-flip backfill flow polish (e.g. when admin flips v1→v2, kick off `backfill_profile` automatically)
