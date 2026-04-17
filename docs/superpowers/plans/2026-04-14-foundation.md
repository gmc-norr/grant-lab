# Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a deployable, authenticated empty FastAPI app with Postgres, Alembic, structured logging, magic-link auth (hashed tokens, rate-limited, single-use, 30-day sessions), domain allowlist, first-admin bootstrap, `/healthz`, and a minimal `/me` page. Everything later plans (ingestion, ranking, profiles, digest) will build on.

**Architecture:** Single FastAPI app + single Postgres DB. Magic-link email auth is the only entry point. The `users` and `magic_link_tokens` tables are created in this plan; other tables land with the plans that first need them. Later plans add APScheduler, source adapters, Ollama, etc. In this plan we don't yet pull in anything that isn't used.

**Tech Stack:** Python 3.12 · `uv` · FastAPI · Jinja2 · SQLAlchemy 2.x · Alembic · PostgreSQL 16 · `itsdangerous` (session signing) · `httpx` (outbound) · `pytest` + `testcontainers[postgres]` · `ruff`

---

## File structure created by this plan

```
call-monitor/
├── pyproject.toml
├── docker-compose.yml
├── .env.example
├── alembic.ini
├── alembic/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       └── 0001_users_and_magic_link_tokens.py
├── src/call_monitor/
│   ├── __init__.py
│   ├── config.py                        # Pydantic settings from env
│   ├── logging.py                       # structured JSON logging
│   ├── main.py                          # FastAPI app factory + lifespan
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py                      # DeclarativeBase
│   │   ├── session.py                   # engine, SessionLocal, get_db
│   │   └── models.py                    # all SQLAlchemy models
│   ├── auth/
│   │   ├── __init__.py
│   │   ├── tokens.py                    # magic-link token generate/hash/consume
│   │   ├── domain.py                    # email-domain allowlist check
│   │   ├── rate_limit.py                # per-email / per-IP LRU limiter
│   │   ├── email.py                     # outbound sender (Resend + test double)
│   │   ├── session_cookie.py            # signed session cookie
│   │   └── dependencies.py              # get_current_user, require_admin
│   ├── web/
│   │   ├── __init__.py
│   │   ├── routes/
│   │   │   ├── __init__.py
│   │   │   ├── health.py
│   │   │   ├── auth.py
│   │   │   └── me.py
│   │   └── templates/
│   │       ├── base.html
│   │       ├── login.html
│   │       ├── login_sent.html
│   │       └── me.html
│   └── admin_bootstrap.py               # INITIAL_ADMIN_EMAIL on startup
├── tests/
│   ├── __init__.py
│   ├── conftest.py                      # testcontainers Postgres fixture
│   ├── test_config.py
│   ├── test_db_session.py
│   ├── test_auth_tokens.py
│   ├── test_auth_domain.py
│   ├── test_auth_rate_limit.py
│   ├── test_auth_email.py
│   ├── test_auth_session_cookie.py
│   ├── test_web_health.py
│   ├── test_web_auth.py
│   ├── test_web_me.py
│   ├── test_admin_bootstrap.py
│   └── test_login_flow.py               # end-to-end integration
└── .github/
    └── workflows/
        └── ci.yml
```

---

## Task 1: Project scaffolding with uv, ruff, pytest

**Files:**
- Create: `pyproject.toml`
- Create: `src/call_monitor/__init__.py` (empty)
- Create: `tests/__init__.py` (empty)
- Create: `tests/test_smoke.py`
- Create: `.env.example`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "call-monitor"
version = "0.1.0"
description = "Internal hospital funding-call monitor"
requires-python = ">=3.12"
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
]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "testcontainers[postgres]>=4.8",
    "ruff>=0.6",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/call_monitor"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
asyncio_mode = "auto"
```

- [ ] **Step 2: Create empty package and test init files**

```bash
mkdir -p src/call_monitor tests
touch src/call_monitor/__init__.py tests/__init__.py
```

- [ ] **Step 3: Write smoke test**

`tests/test_smoke.py`:

```python
def test_package_importable():
    import call_monitor  # noqa: F401
```

- [ ] **Step 4: Write `.env.example`**

```
# Database
DATABASE_URL=postgresql+psycopg://call_monitor:call_monitor@localhost:5432/call_monitor

# Web / session
SESSION_SECRET=change-me-to-a-long-random-string

# Auth
ALLOWED_EMAIL_DOMAINS=karolinska.se,regionstockholm.se,ki.se
INITIAL_ADMIN_EMAIL=

# Email (Resend for v1; swap to SMTP later if needed)
RESEND_API_KEY=
EMAIL_FROM=no-reply@example.com
APP_BASE_URL=http://localhost:8000

# LLM (placeholders — ranking plan wires these up)
OLLAMA_URL=http://localhost:11434
GEMMA_MODEL=gemma-4-e4b-q8
```

- [ ] **Step 5: Install deps and run smoke test**

Run:
```bash
uv sync
uv run pytest tests/test_smoke.py -v
```
Expected: `1 passed`.

- [ ] **Step 6: Verify lint is clean**

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/ tests/ .env.example
git commit -m "feat: project scaffolding with uv, ruff, pytest"
```

---

## Task 2: Local Postgres via docker-compose

**Files:**
- Create: `docker-compose.yml`

- [ ] **Step 1: Write `docker-compose.yml`**

```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: call_monitor
      POSTGRES_PASSWORD: call_monitor
      POSTGRES_DB: call_monitor
    ports:
      # Bind to loopback so a known-credential dev DB is never exposed to
      # the LAN. Override the host port via HOST_DB_PORT if 5432 is busy
      # (e.g. another local Postgres):  HOST_DB_PORT=5433 docker compose up -d
      - "127.0.0.1:${HOST_DB_PORT:-5432}:5432"
    volumes:
      - call_monitor_pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U call_monitor"]
      interval: 5s
      timeout: 3s
      retries: 10

volumes:
  call_monitor_pgdata:
```

- [ ] **Step 2: Bring the DB up**

Run: `docker compose up -d db`
Expected: `Container call-monitor-db-1  Started` or similar.

- [ ] **Step 3: Verify connection**

Run: `docker compose exec db pg_isready -U call_monitor`
Expected: `accepting connections`.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: local Postgres via docker-compose"
```

---

## Task 3: Database session layer + testcontainers fixture

**Files:**
- Create: `src/call_monitor/db/__init__.py` (initially empty; Task 5 adds the models import for Alembic autogenerate — see Task 5 Step 4 note)
- Create: `src/call_monitor/db/base.py`
- Create: `src/call_monitor/db/session.py`
- Create: `tests/conftest.py`
- Create: `tests/test_db_session.py`

- [ ] **Step 1: Write the failing test**

`tests/test_db_session.py`:

```python
from sqlalchemy import text

from call_monitor.db.session import make_session_factory


def test_engine_executes_select_one(db_engine):
    with db_engine.connect() as conn:
        result = conn.execute(text("SELECT 1")).scalar()
    assert result == 1


def test_session_factory_yields_working_session(db_engine):
    SessionLocal = make_session_factory(db_engine)
    with SessionLocal() as session:
        assert session.execute(text("SELECT 42")).scalar() == 42
```

- [ ] **Step 2: Write the testcontainers fixture**

`tests/conftest.py`:

```python
import pytest
from testcontainers.postgres import PostgresContainer

from call_monitor.config import Settings
from call_monitor.db.session import make_engine


@pytest.fixture(autouse=True)
def _isolate_settings_from_dotenv(monkeypatch):
    """Disable .env loading for every test.

    A developer's local .env can otherwise pre-fill SESSION_SECRET,
    INITIAL_ADMIN_EMAIL, etc., turning hermetic env-driven tests into
    flaky pass-on-my-machine traps. Tests that exercise Settings() must
    rely solely on monkeypatch.setenv()."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture(scope="session")
def pg_container():
    # driver="psycopg" makes get_connection_url() emit `postgresql+psycopg://...`
    # directly. No post-hoc URL rewrite, so the fixture is robust if testcontainers
    # ever changes its default driver.
    with PostgresContainer("postgres:16", driver="psycopg") as pg:
        yield pg


@pytest.fixture(scope="session")
def pg_url(pg_container):
    return pg_container.get_connection_url()


@pytest.fixture(scope="session")
def db_engine(pg_url):
    """Session-scoped Engine: shared across tests, disposed at session teardown.

    Without this, tests that build their own Engine via `make_engine(pg_url)`
    leak DBAPI connections to the GC and emit `ResourceWarning` (which become
    fatal under `filterwarnings = ["error"]`). Centralizing here also avoids
    spinning up a fresh engine per test.
    """
    engine = make_engine(pg_url)
    try:
        yield engine
    finally:
        engine.dispose()
```

- [ ] **Step 3: Write `db/base.py`**

`src/call_monitor/db/base.py`:

```python
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
```

- [ ] **Step 4: Write `db/session.py`**

`src/call_monitor/db/session.py`:

```python
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False, class_=Session)


def get_db_dependency(session_factory: sessionmaker[Session]):
    """Return a FastAPI dependency that yields a session."""

    def _get_db() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    return _get_db
```

- [ ] **Step 5: Run tests — expect pass**

Run: `uv run pytest tests/test_db_session.py -v`
Expected: `2 passed`. Testcontainers will pull `postgres:16` on first run (~30 s).

- [ ] **Step 6: Commit**

```bash
git add src/call_monitor/db/ tests/conftest.py tests/test_db_session.py
git commit -m "feat: database session layer with testcontainers"
```

---

## Task 4: Alembic setup

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako`

- [ ] **Step 1: Initialize Alembic**

Run: `uv run alembic init alembic`
Expected: Creates `alembic/` dir and `alembic.ini`.

- [ ] **Step 2: Edit `alembic.ini`**

Make two edits:

1. Change the `sqlalchemy.url` line to:
   ```
   sqlalchemy.url =
   ```
   (empty — we'll inject it from env at runtime).

2. Change `prepend_sys_path = .` to:
   ```
   prepend_sys_path = src
   ```
   The project uses `src/` layout. Without this, `from call_monitor.db.base import Base` only works when the package is installed editable (`uv sync`). A future Dockerfile that runs `pip install --no-deps .` then `alembic upgrade head` would fail with `ModuleNotFoundError`. `prepend_sys_path = src` makes Alembic work directly from the source tree, no editable install required.

- [ ] **Step 3: Rewrite `alembic/env.py`**

```python
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from call_monitor.db.base import Base

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False is critical: the default (True) sets
    # `disabled=True` on every logger NOT named in the [loggers] section of
    # alembic.ini — silently turning off our app's own loggers (e.g.
    # `call_monitor.auth.email`) for the rest of the process. The damage
    # surfaces in tests as "the message was logged but caplog saw nothing"
    # because `Logger.disabled=True` short-circuits before any handler runs.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _resolve_database_url() -> str:
    """Prefer an explicit URL injected via ``cfg.set_main_option(...)`` (the
    test fixture path); fall back to ``DATABASE_URL`` env. Resolved lazily so
    metadata-only commands (``alembic heads``, ``history``, ``branches``)
    don't require a database URL — only commands that actually run migrations
    do.
    """
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "No database URL: set DATABASE_URL in the environment "
            "or inject via cfg.set_main_option('sqlalchemy.url', ...)"
        )
    return url


def run_migrations_offline() -> None:
    url = _resolve_database_url()
    config.set_main_option("sqlalchemy.url", url)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    config.set_main_option("sqlalchemy.url", _resolve_database_url())
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 4: Verify Alembic runs (no migrations yet, just check it starts)**

Run:
```bash
DATABASE_URL="postgresql+psycopg://call_monitor:call_monitor@localhost:5432/call_monitor" \
  uv run alembic current
```
Expected: Prints nothing (no migrations applied yet), exits 0.

- [ ] **Step 5: Commit**

```bash
git add alembic.ini alembic/
git commit -m "feat: alembic setup reading DATABASE_URL from env"
```

---

## Task 5: Users + magic_link_tokens models and initial migration

**Files:**
- Create: `src/call_monitor/db/models.py`
- Create: `alembic/versions/0001_users_and_magic_link_tokens.py`
- Create: `tests/test_models.py`

- [ ] **Step 1: Write the failing test**

`tests/test_models.py`:

```python
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from call_monitor.db.models import MagicLinkToken, User


def test_can_insert_and_read_user(pg_session_factory):
    SessionLocal = pg_session_factory
    with SessionLocal() as s:
        u = User(email="alice@ki.se", name="Alice", is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        assert u.id is not None
        assert u.created_at is not None


def test_user_email_is_unique(pg_session_factory):
    SessionLocal = pg_session_factory
    with SessionLocal() as s:
        s.add(User(email="bob@ki.se"))
        s.commit()
    with SessionLocal() as s, pytest.raises(IntegrityError):
        s.add(User(email="bob@ki.se"))
        s.commit()


def test_user_email_check_rejects_mixed_case(pg_session_factory):
    """The CHECK constraint forces lowercase storage so UNIQUE(email) is
    case-insensitive in practice. Inserting a non-lowercased email must fail."""
    SessionLocal = pg_session_factory
    with SessionLocal() as s, pytest.raises(IntegrityError):
        s.add(User(email="Alice@ki.se"))
        s.commit()


def test_magic_link_token_row(pg_session_factory):
    SessionLocal = pg_session_factory
    with SessionLocal() as s:
        u = User(email="carol@ki.se")
        s.add(u)
        s.flush()
        now = datetime.now(UTC)
        t = MagicLinkToken(
            user_id=u.id,
            token_hash="a" * 64,
            created_at=now,
            expires_at=now + timedelta(minutes=15),
        )
        s.add(t)
        s.commit()
        s.refresh(t)
        assert t.id is not None
        assert t.consumed_at is None
```

- [ ] **Step 2: Add a `pg_session_factory` fixture to `tests/conftest.py`**

Append to `tests/conftest.py`:

```python
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from call_monitor.db.session import make_session_factory


@pytest.fixture(scope="session")
def migrated_engine(db_engine, pg_url):
    """Same engine as `db_engine`, but with Alembic migrations applied first.

    Reuses `db_engine` (single shared, disposed at session teardown) to keep
    connection accounting clean.
    """
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    command.upgrade(cfg, "head")
    return db_engine


@pytest.fixture()
def pg_session_factory(migrated_engine):
    from call_monitor.db.base import Base

    SessionLocal = make_session_factory(migrated_engine)
    yield SessionLocal
    # Truncate every table Base knows about, sorted for determinism. Future
    # plans that add tables get isolation for free without having to remember
    # to update this fixture. CASCADE handles FK ordering; RESTART IDENTITY
    # resets sequences so id counters are predictable across tests.
    table_names = ", ".join(t.name for t in Base.metadata.sorted_tables)
    if table_names:
        with migrated_engine.begin() as conn:
            conn.execute(
                text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE")
            )
```

- [ ] **Step 3: Run the test to verify failure**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'call_monitor.db.models'`.

- [ ] **Step 4: Write `db/models.py` and register it in `db/__init__.py`**

Replace the (currently empty) `src/call_monitor/db/__init__.py` with:

```python
# Importing models here registers every model with Base.metadata as a side
# effect. Anything that imports from `call_monitor.db.*` (notably alembic/env.py
# via `from call_monitor.db.base import Base`) triggers this package init,
# which means `Base.metadata.tables` is populated when Alembic resolves
# `target_metadata`. Without this, `alembic revision --autogenerate` produces
# empty diffs.
from call_monitor.db import models  # noqa: F401
```

Then create `src/call_monitor/db/models.py`:

```python
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from call_monitor.db.base import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Defense in depth: the application always lowercases before insert,
        # but Postgres `UNIQUE(email)` is case-sensitive, so a rogue SQL
        # admin task could create both `Alice@ki.se` and `alice@ki.se` and
        # split a single person across two accounts. The CHECK forces
        # lowercase at the DB level — combined with UNIQUE(email), this
        # guarantees one row per address regardless of case.
        CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    magic_link_tokens: Mapped[list[MagicLinkToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class MagicLinkToken(Base):
    __tablename__ = "magic_link_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="magic_link_tokens")

    # `unique=True` on token_hash already creates a unique index — no separate
    # Index() needed. Verification path is `WHERE token_hash = :h`, which uses
    # the unique index.
```

- [ ] **Step 5: Write the migration**

`alembic/versions/0001_users_and_magic_link_tokens.py`:

```python
"""users and magic_link_tokens

Revision ID: 0001
Revises:
Create Date: 2026-04-14
"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("email", name="uq_users_email"),
        # CHECK forces lowercase storage so UNIQUE(email) actually means
        # one row per address (Postgres `=` is case-sensitive otherwise).
        sa.CheckConstraint(
            "email = lower(email)", name="ck_users_email_lowercase"
        ),
    )
    op.create_table(
        "magic_link_tokens",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE", name="fk_mlt_user_id"
        ),
        sa.UniqueConstraint("token_hash", name="uq_mlt_token_hash"),
    )
    # Postgres auto-creates a unique index backing uq_mlt_token_hash, which
    # satisfies the "fast lookup by token hash" verification query. No extra
    # index needed.


def downgrade() -> None:
    op.drop_table("magic_link_tokens")
    op.drop_table("users")
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_models.py -v`
Expected: `3 passed`.

- [ ] **Step 7: Commit**

```bash
git add src/call_monitor/db/models.py alembic/versions/ tests/conftest.py tests/test_models.py
git commit -m "feat: users and magic_link_tokens tables"
```

---

## Task 6: Config module (Pydantic settings)

**Files:**
- Create: `src/call_monitor/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

`tests/test_config.py`:

```python
import pytest

from call_monitor.config import Settings


def test_settings_parse_domain_list_from_csv(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se, karolinska.se,REGION.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")

    s = Settings()
    assert s.allowed_email_domains == ["ki.se", "karolinska.se", "region.se"]


def test_cookie_secure_follows_app_base_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")

    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    assert Settings().cookie_secure is False

    monkeypatch.setenv("APP_BASE_URL", "https://call-monitor.example.com")
    assert Settings().cookie_secure is True


def test_settings_parse_initial_admins_csv(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", "a@ki.se, B@Ki.se")

    s = Settings()
    assert s.initial_admin_emails == ["a@ki.se", "b@ki.se"]


def test_settings_require_session_secret(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")

    with pytest.raises(ValueError):
        Settings()
```

- [ ] **Step 2: Run test — expect fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: `ModuleNotFoundError` or similar.

- [ ] **Step 3: Write `config.py`**

`src/call_monitor/config.py`:

```python
from typing import Annotated

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = Field(alias="DATABASE_URL")
    session_secret: str = Field(alias="SESSION_SECRET", min_length=16)
    # NoDecode: pydantic-settings would otherwise try to JSON-decode the env
    # value for complex types. We want plain comma-separated strings, parsed
    # by the validator below.
    allowed_email_domains: Annotated[list[str], NoDecode] = Field(
        alias="ALLOWED_EMAIL_DOMAINS"
    )
    initial_admin_emails: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="INITIAL_ADMIN_EMAIL"
    )
    app_base_url: str = Field(alias="APP_BASE_URL")
    resend_api_key: str | None = Field(default=None, alias="RESEND_API_KEY")
    email_from: str = Field(default="no-reply@example.com", alias="EMAIL_FROM")

    @field_validator("allowed_email_domains", "initial_admin_emails", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [p.strip().lower() for p in v.split(",") if p.strip()]
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cookie_secure(self) -> bool:
        """Whether to set Secure on session cookies.

        Derived from `APP_BASE_URL` because that's the URL the **browser**
        sees — which is what the Secure attribute keys off. In a typical
        deploy a TLS-terminating proxy (Nginx/Traefik/Apache) sits in
        front and forwards HTTP to the app, so `request.url.scheme` would
        say `http` and is the wrong signal. Don't change this to look at
        the incoming request — `APP_BASE_URL` is the right answer.

        Same value also gates the email-sender fallback in `main.py`:
        without it, a misconfigured prod (https without `RESEND_API_KEY`)
        would silently log magic links to stderr instead of refusing to
        start.
        """
        return self.app_base_url.lower().startswith("https://")
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_config.py -v`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor/config.py tests/test_config.py
git commit -m "feat: Pydantic settings with CSV parsing for domain/admin lists"
```

---

## Task 7: Structured JSON logging

**Files:**
- Create: `src/call_monitor/logging.py`
- Create: `tests/test_logging.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_logging.py`:

```python
import json
import logging

import pytest

from call_monitor.logging import JsonFormatter, configure_logging


@pytest.fixture(autouse=True)
def reset_root_handlers():
    """Each test gets a fresh root logger so configure_logging() actually
    re-installs a handler bound to pytest's per-test captured stderr.
    Without this, the idempotent-configure check in configure_logging()
    keeps a stale handler pointing at a previous test's stderr buffer."""
    root = logging.getLogger()
    saved = list(root.handlers)
    root.handlers.clear()
    yield
    root.handlers.clear()
    root.handlers.extend(saved)


def _last_json_line(stderr: str) -> dict:
    return json.loads(stderr.strip().splitlines()[-1])


def test_logging_outputs_json(capsys):
    configure_logging()
    log = logging.getLogger("test")
    log.info("hello", extra={"user_id": 42})
    payload = _last_json_line(capsys.readouterr().err)
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["user_id"] == 42


def test_extra_cannot_clobber_reserved_keys(capsys):
    """A caller passing `extra={"level": "PWNED"}` must not overwrite the
    real log level — that would let bad code bypass level-based filters.

    Note: stdlib `makeRecord` already rejects `extra={"message": ...}` and
    `extra={"asctime": ...}` with KeyError, so those names can't be tested
    here. `level`, `logger`, `ts`, `exc` are not protected by stdlib and
    would otherwise reach our payload — _RESERVED keeps them out.
    """
    configure_logging()
    log = logging.getLogger("test")
    log.warning(
        "real-message",
        extra={"level": "PWNED", "logger": "fake", "ts": "0", "exc": "spoofed"},
    )
    payload = _last_json_line(capsys.readouterr().err)
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "test"
    assert payload["message"] == "real-message"
    assert payload["ts"] != "0"
    assert "exc" not in payload


def test_configure_logging_is_idempotent():
    """Calling configure_logging() twice must not duplicate handlers nor
    clobber existing ones — important because create_app() invokes it on
    every call and tests may attach their own handlers (e.g. caplog)."""
    configure_logging()
    root = logging.getLogger()
    json_handlers_before = sum(1 for h in root.handlers if isinstance(h.formatter, JsonFormatter))
    handlers_before = list(root.handlers)

    configure_logging()
    json_handlers_after = sum(1 for h in root.handlers if isinstance(h.formatter, JsonFormatter))
    assert json_handlers_after == json_handlers_before == 1
    assert root.handlers == handlers_before  # exact same handler instances
```

- [ ] **Step 2: Run test — expect fail**

Run: `uv run pytest tests/test_logging.py -v`
Expected: FAIL.

- [ ] **Step 3: Write `logging.py`**

`src/call_monitor/logging.py`:

```python
import json
import logging
import sys
from datetime import UTC, datetime

# Top-level keys we set explicitly in `format()`. User-supplied `extra={}`
# fields with these names would otherwise overwrite them — including `level`,
# which would let a careless caller bypass log-level filters or audit assertions.
_RESERVED = {"ts", "level", "logger", "message", "exc"}


class JsonFormatter(logging.Formatter):
    # Stdlib LogRecord attrs we should not surface as user fields.
    _STANDARD = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in self._STANDARD or key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Install a JsonFormatter stderr handler on the root logger.

    Idempotent: if a `JsonFormatter` handler is already attached, only the
    log level is updated and existing handlers are left in place. This
    matters because `create_app()` calls `configure_logging()` on every
    invocation; without this guard, each fresh app wipes the root logger's
    handlers, which would silently drop records from `caplog` or any other
    test-side handler attached between calls.
    """
    root = logging.getLogger()
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
```

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/test_logging.py -v`
Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor/logging.py tests/test_logging.py
git commit -m "feat: structured JSON logging to stderr"
```

---

## Task 8: FastAPI app factory + /healthz + base templates

**Files:**
- Create: `src/call_monitor/main.py`
- Create: `src/call_monitor/web/__init__.py` (empty)
- Create: `src/call_monitor/web/routes/__init__.py` (empty)
- Create: `src/call_monitor/web/routes/health.py`
- Create: `src/call_monitor/web/templates/base.html`
- Create: `tests/test_web_health.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_web_health.py`:

```python
from fastapi.testclient import TestClient

from call_monitor.db.session import make_engine
from call_monitor.main import create_app


def _set_min_env(monkeypatch, database_url):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


def test_healthz_returns_ok(pg_url, monkeypatch):
    _set_min_env(monkeypatch, pg_url)
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_healthz_returns_503_when_db_unreachable(pg_url, monkeypatch):
    """Outage path: simulate a dead database by swapping app.state.engine."""
    _set_min_env(monkeypatch, pg_url)
    app = create_app()
    # Replace the engine with one pointing at a closed port (fast TCP refusal).
    app.state.engine = make_engine("postgresql+psycopg://x:x@127.0.0.1:1/x")
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.status_code == 503
    assert "db unavailable" in r.json()["detail"]
```

- [ ] **Step 2: Write `web/templates/base.html`**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{% block title %}Call Monitor{% endblock %}</title>
  <script src="https://unpkg.com/htmx.org@1.9.12"></script>
</head>
<body>
  <header><a href="/">Call Monitor</a></header>
  <main>{% block content %}{% endblock %}</main>
</body>
</html>
```

- [ ] **Step 3: Write `web/routes/health.py`**

```python
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

router = APIRouter()


@router.get("/healthz")
def healthz(request: Request) -> dict[str, str]:
    """Liveness + readiness in one endpoint.

    Single-instance deploy: there's no useful liveness-vs-readiness split.
    Monitoring should treat any non-200 as "do not route traffic / page
    operator." We ping the database with `SELECT 1` so an unreachable DB
    surfaces as 503 instead of being silently invisible.
    """
    engine = request.app.state.engine
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — surface any DB error as 503
        raise HTTPException(status_code=503, detail=f"db unavailable: {exc}") from exc
    return {"status": "ok"}
```

- [ ] **Step 4: Write `main.py`**

`src/call_monitor/main.py`:

```python
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from call_monitor.config import Settings
from call_monitor.db.session import make_engine, make_session_factory
from call_monitor.logging import configure_logging
from call_monitor.web.routes import health

TEMPLATES_DIR = Path(__file__).parent / "web" / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup tasks belong here (admin bootstrap lands in Task 17).
    yield


def create_app() -> FastAPI:
    configure_logging()
    settings = Settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    app.include_router(health.router)
    return app
```

- [ ] **Step 5: Run the test**

Run: `uv run pytest tests/test_web_health.py -v`
Expected: `1 passed`.

- [ ] **Step 6: Manual smoke check**

Run:
```bash
DATABASE_URL="postgresql+psycopg://call_monitor:call_monitor@localhost:5432/call_monitor" \
SESSION_SECRET="dev-secret-at-least-16-chars-please" \
ALLOWED_EMAIL_DOMAINS="ki.se" \
APP_BASE_URL="http://localhost:8000" \
  uv run uvicorn call_monitor.main:create_app --factory --port 8000 &
sleep 2
curl -s http://localhost:8000/healthz
kill %1
```
Expected: `{"status":"ok"}`.

- [ ] **Step 7: Commit**

```bash
git add src/call_monitor/main.py src/call_monitor/web/ tests/test_web_health.py
git commit -m "feat: FastAPI app factory with /healthz"
```

---

## Task 9: Magic-link token operations

**Files:**
- Create: `src/call_monitor/auth/__init__.py` (empty)
- Create: `src/call_monitor/auth/tokens.py`
- Create: `tests/test_auth_tokens.py`

- [ ] **Step 1: Write the failing test**

`tests/test_auth_tokens.py`:

```python
from datetime import UTC, datetime, timedelta

import pytest

from call_monitor.auth.tokens import (
    TokenAlreadyConsumed,
    TokenExpired,
    TokenNotFound,
    consume_token,
    hash_token,
    issue_token,
)
from call_monitor.db.models import MagicLinkToken, User


def test_hash_token_is_sha256_hex(tmp_path):
    h = hash_token("hello")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)
    assert h == hash_token("hello")
    assert h != hash_token("world")


def test_issue_token_returns_raw_and_stores_hash(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)

        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()
        assert len(raw) >= 32

        row = s.query(MagicLinkToken).filter_by(user_id=u.id).one()
        assert row.token_hash == hash_token(raw)
        assert row.consumed_at is None
        assert row.expires_at > datetime.now(UTC)


def test_consume_token_marks_consumed_and_returns_user_id(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()

        user_id = consume_token(s, raw)
        s.commit()
        assert user_id == u.id

        row = s.query(MagicLinkToken).filter_by(user_id=u.id).one()
        assert row.consumed_at is not None


def test_consume_token_rejects_replay(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()
        consume_token(s, raw)
        s.commit()

        with pytest.raises(TokenAlreadyConsumed):
            consume_token(s, raw)


def test_consume_token_rejects_expired(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        raw = issue_token(s, user_id=u.id, ttl_minutes=-1)  # already expired
        s.commit()
        with pytest.raises(TokenExpired):
            consume_token(s, raw)


def test_consume_token_rejects_unknown(pg_session_factory):
    with pg_session_factory() as s:
        with pytest.raises(TokenNotFound):
            consume_token(s, "does-not-exist-at-all-12345678")
```

- [ ] **Step 2: Run test — expect fail**

Run: `uv run pytest tests/test_auth_tokens.py -v`
Expected: import error.

- [ ] **Step 3: Write `auth/tokens.py`**

`src/call_monitor/auth/tokens.py`:

```python
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from call_monitor.db.models import MagicLinkToken


class TokenNotFound(Exception):
    pass


class TokenExpired(Exception):
    pass


class TokenAlreadyConsumed(Exception):
    pass


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_token(session: Session, *, user_id: int, ttl_minutes: int = 15) -> str:
    raw = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    row = MagicLinkToken(
        user_id=user_id,
        token_hash=hash_token(raw),
        created_at=now,
        expires_at=now + timedelta(minutes=ttl_minutes),
    )
    session.add(row)
    return raw


def consume_token(session: Session, raw: str) -> int:
    """Atomically mark the token consumed and return the owning user_id.

    Raises TokenNotFound / TokenExpired / TokenAlreadyConsumed.
    """
    token_hash = hash_token(raw)
    now = datetime.now(UTC)

    # Atomic single-use: UPDATE ... WHERE consumed_at IS NULL AND expires_at > now()
    result = session.execute(
        text(
            """
            UPDATE magic_link_tokens
               SET consumed_at = :now
             WHERE token_hash = :token_hash
               AND consumed_at IS NULL
               AND expires_at > :now
         RETURNING user_id
            """
        ),
        {"now": now, "token_hash": token_hash},
    )
    row = result.first()
    if row is not None:
        return int(row[0])

    # Figure out *why* it failed so the caller can report accurately.
    existing = session.execute(
        text(
            "SELECT consumed_at, expires_at FROM magic_link_tokens WHERE token_hash = :h"
        ),
        {"h": token_hash},
    ).first()
    if existing is None:
        raise TokenNotFound()
    consumed_at, expires_at = existing
    if consumed_at is not None:
        raise TokenAlreadyConsumed()
    if expires_at <= now:
        raise TokenExpired()
    # Should be unreachable.
    raise TokenNotFound()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_auth_tokens.py -v`
Expected: `6 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor/auth/ tests/test_auth_tokens.py
git commit -m "feat: magic-link token issue/consume with atomic single-use"
```

---

## Task 10: Email-domain allowlist

**Files:**
- Create: `src/call_monitor/auth/domain.py`
- Create: `tests/test_auth_domain.py`

- [ ] **Step 1: Write the failing test**

`tests/test_auth_domain.py`:

```python
import pytest

from call_monitor.auth.domain import domain_allowed, extract_domain


@pytest.mark.parametrize(
    "email,expected",
    [
        ("alice@ki.se", "ki.se"),
        ("Bob@KI.SE", "ki.se"),
        ("carol+tag@karolinska.se", "karolinska.se"),
    ],
)
def test_extract_domain(email, expected):
    assert extract_domain(email) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "not-an-email",       # no @
        "x@y@ki.se",          # multiple @
        "@ki.se",             # empty local part
        "alice@",             # empty domain
        "",                   # empty
    ],
)
def test_extract_domain_rejects_malformed(bad):
    with pytest.raises(ValueError):
        extract_domain(bad)


@pytest.mark.parametrize(
    "email,allowed,ok",
    [
        ("alice@ki.se", ["ki.se"], True),
        ("alice@KI.SE", ["ki.se"], True),
        ("alice@ki.se", ["karolinska.se"], False),
        # subdomain does NOT match parent — explicit listing required
        ("alice@intern.ki.se", ["ki.se"], False),
        ("alice@intern.ki.se", ["intern.ki.se", "ki.se"], True),
    ],
)
def test_domain_allowed(email, allowed, ok):
    assert domain_allowed(email, allowed) is ok
```

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_auth_domain.py -v`
Expected: fails on import.

- [ ] **Step 3: Write `auth/domain.py`**

```python
def extract_domain(email: str) -> str:
    """Return the lower-cased domain of an email address.

    Strict: exactly one `@`, both local and domain parts non-empty. Rejects
    multi-`@` addresses (`x@y@ki.se`) so we never write malformed addresses
    to the users table or hand them to the email API.
    """
    parts = email.split("@")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"not a valid email address: {email!r}")
    return parts[1].lower()


def domain_allowed(email: str, allowed_domains: list[str]) -> bool:
    try:
        domain = extract_domain(email)
    except ValueError:
        return False
    normalized = {d.lower() for d in allowed_domains}
    return domain in normalized
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_auth_domain.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor/auth/domain.py tests/test_auth_domain.py
git commit -m "feat: email-domain allowlist with case-insensitive equality"
```

---

## Task 11: Per-email / per-IP rate limiter

**Files:**
- Create: `src/call_monitor/auth/rate_limit.py`
- Create: `tests/test_auth_rate_limit.py`

- [ ] **Step 1: Write the failing test**

`tests/test_auth_rate_limit.py`:

```python
import time

from call_monitor.auth.rate_limit import SlidingWindowLimiter


def test_allows_under_limit():
    rl = SlidingWindowLimiter(max_requests=3, window_seconds=60)
    for _ in range(3):
        assert rl.allow("alice@ki.se") is True


def test_blocks_over_limit():
    rl = SlidingWindowLimiter(max_requests=3, window_seconds=60)
    for _ in range(3):
        rl.allow("alice@ki.se")
    assert rl.allow("alice@ki.se") is False


def test_separate_keys_are_independent():
    rl = SlidingWindowLimiter(max_requests=1, window_seconds=60)
    assert rl.allow("a@ki.se") is True
    assert rl.allow("b@ki.se") is True
    assert rl.allow("a@ki.se") is False


def test_window_expires(monkeypatch):
    clock = {"t": 1000.0}

    def fake_time():
        return clock["t"]

    rl = SlidingWindowLimiter(max_requests=2, window_seconds=10, clock=fake_time)
    assert rl.allow("x") is True
    assert rl.allow("x") is True
    assert rl.allow("x") is False

    clock["t"] += 11  # past the window
    assert rl.allow("x") is True
```

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_auth_rate_limit.py -v`

- [ ] **Step 3: Write `auth/rate_limit.py`**

```python
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable


class SlidingWindowLimiter:
    """In-memory sliding-window counter.

    Single-instance deploy assumption — safe because APScheduler/FastAPI run
    as exactly one process. For multi-instance we'd promote this to a
    Postgres table or Redis.

    Thread-safe: FastAPI sync handlers run in anyio's thread pool, so two
    `/login` POSTs can race the read-then-modify pattern below. A single
    lock around the whole body costs essentially nothing at our request
    rates and removes the race.
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._max = max_requests
        self._window = window_seconds
        self._clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        with self._lock:
            now = self._clock()
            q = self._events[key]
            cutoff = now - self._window
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self._max:
                return False
            q.append(now)
            return True
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_auth_rate_limit.py -v`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor/auth/rate_limit.py tests/test_auth_rate_limit.py
git commit -m "feat: in-memory sliding-window rate limiter"
```

---

## Task 12: Outbound email sender with test double

**Files:**
- Create: `src/call_monitor/auth/email.py`
- Create: `tests/test_auth_email.py`

- [ ] **Step 1: Write the failing test**

`tests/test_auth_email.py`:

```python
import logging

from call_monitor.auth.email import ConsoleEmailSender, InMemoryEmailSender


def test_in_memory_sender_records_messages():
    sender = InMemoryEmailSender()
    sender.send(
        to="alice@ki.se",
        subject="Your login link",
        text_body="Click: https://x/verify?token=abc",
    )
    assert len(sender.sent) == 1
    msg = sender.sent[0]
    assert msg.to == "alice@ki.se"
    assert msg.subject == "Your login link"
    assert "https://x/verify?token=abc" in msg.text_body


def test_console_sender_logs_full_body(caplog):
    sender = ConsoleEmailSender()
    with caplog.at_level(logging.INFO, logger="call_monitor.auth.email"):
        sender.send(
            to="alice@ki.se",
            subject="Your login link",
            text_body="Click: https://x/verify?token=abc",
        )
    assert any(
        "https://x/verify?token=abc" in r.getMessage()
        or "https://x/verify?token=abc" in str(getattr(r, "body", ""))
        for r in caplog.records
    )
```

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_auth_email.py -v`

- [ ] **Step 3: Write `auth/email.py`**

```python
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

_logger = logging.getLogger(__name__)


@dataclass
class EmailMessage:
    to: str
    subject: str
    text_body: str


class EmailSender(Protocol):
    def send(self, *, to: str, subject: str, text_body: str) -> None: ...


@dataclass
class InMemoryEmailSender:
    """Pure test double — appends to a list, nothing else."""

    sent: list[EmailMessage] = field(default_factory=list)

    def send(self, *, to: str, subject: str, text_body: str) -> None:
        self.sent.append(EmailMessage(to=to, subject=subject, text_body=text_body))


class ConsoleEmailSender:
    """Local-dev sender: writes the full message to the logger so developers
    can copy the magic link out of the app's stderr stream.

    SECURITY: never select this in production — it logs the full magic link
    body, which inside a 15-minute window is equivalent to a session token.
    `create_app()` only allows it when `cookie_secure=False` (i.e. dev over
    plain HTTP); a misconfigured prod (https without `RESEND_API_KEY`) must
    fail to start, not silently log credentials.
    """

    def send(self, *, to: str, subject: str, text_body: str) -> None:
        _logger.info(
            "email_sent (console — development)",
            extra={
                "event": "email_sent",
                "to": to,
                "subject": subject,
                "body": text_body,
            },
        )


@dataclass
class ResendEmailSender:
    api_key: str
    from_address: str
    _client: httpx.Client | None = None

    def send(self, *, to: str, subject: str, text_body: str) -> None:
        client = self._client or httpx.Client(timeout=10.0)
        try:
            r = client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "from": self.from_address,
                    "to": [to],
                    "subject": subject,
                    "text": text_body,
                },
            )
            r.raise_for_status()
        finally:
            if self._client is None:
                client.close()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_auth_email.py -v`
Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor/auth/email.py tests/test_auth_email.py
git commit -m "feat: email sender protocol with Resend + in-memory double"
```

---

## Task 13: Session cookie sign/verify

**Files:**
- Create: `src/call_monitor/auth/session_cookie.py`
- Create: `tests/test_auth_session_cookie.py`

- [ ] **Step 1: Write the failing test**

`tests/test_auth_session_cookie.py`:

```python
import pytest

from call_monitor.auth.session_cookie import (
    InvalidSession,
    SessionSigner,
)


def test_roundtrip():
    signer = SessionSigner(secret="x" * 32, max_age_seconds=3600)
    cookie = signer.make(user_id=42)
    assert signer.verify(cookie) == 42


def test_tampered_cookie_rejected():
    signer = SessionSigner(secret="x" * 32, max_age_seconds=3600)
    cookie = signer.make(user_id=42)
    tampered = cookie[:-1] + ("A" if cookie[-1] != "A" else "B")
    with pytest.raises(InvalidSession):
        signer.verify(tampered)


def test_different_secret_rejects():
    signer1 = SessionSigner(secret="x" * 32, max_age_seconds=3600)
    cookie = signer1.make(user_id=42)
    signer2 = SessionSigner(secret="y" * 32, max_age_seconds=3600)
    with pytest.raises(InvalidSession):
        signer2.verify(cookie)


def test_expired_cookie_rejected():
    signer = SessionSigner(secret="x" * 32, max_age_seconds=0)
    cookie = signer.make(user_id=42)
    # max_age=0 means instantly expired when verified
    with pytest.raises(InvalidSession):
        signer.verify(cookie)
```

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_auth_session_cookie.py -v`

- [ ] **Step 3: Write `auth/session_cookie.py`**

```python
from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


class InvalidSession(Exception):
    pass


class SessionSigner:
    def __init__(self, *, secret: str, max_age_seconds: int = 30 * 24 * 3600):
        self._serializer = URLSafeTimedSerializer(secret, salt="call-monitor-session")
        self._max_age = max_age_seconds

    def make(self, *, user_id: int) -> str:
        return self._serializer.dumps({"uid": user_id})

    def verify(self, cookie: str) -> int:
        try:
            payload = self._serializer.loads(cookie, max_age=self._max_age)
        except (BadSignature, SignatureExpired) as e:
            raise InvalidSession(str(e)) from e
        if not isinstance(payload, dict) or "uid" not in payload:
            raise InvalidSession("malformed payload")
        return int(payload["uid"])
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_auth_session_cookie.py -v`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor/auth/session_cookie.py tests/test_auth_session_cookie.py
git commit -m "feat: signed session cookie with itsdangerous"
```

---

## Task 14: Login request endpoint (POST /login)

**Files:**
- Create: `src/call_monitor/web/routes/auth.py`
- Create: `src/call_monitor/web/templates/login.html`
- Create: `src/call_monitor/web/templates/login_sent.html`
- Modify: `src/call_monitor/main.py`
- Create: `tests/test_web_auth.py`

**Design notes:**

- Wire `EmailSender` and `SlidingWindowLimiter` through `app.state` so tests can substitute an `InMemoryEmailSender`.
- GET `/login` renders the form. POST `/login` always shows the same "check your email" page regardless of whether an email was sent — don't leak rate-limit or allowlist state.
- If the user doesn't exist and the domain is allowed, create them.

- [ ] **Step 1: Write the failing test**

`tests/test_web_auth.py`:

```python
from fastapi.testclient import TestClient

from call_monitor.auth.email import InMemoryEmailSender
from call_monitor.auth.rate_limit import SlidingWindowLimiter
from call_monitor.db.models import User
from call_monitor.main import create_app


def _mk_app(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()
    sender = InMemoryEmailSender()
    app.state.email_sender = sender
    # generous limits so shared tests don't interfere
    app.state.email_limiter = SlidingWindowLimiter(max_requests=100, window_seconds=60)
    app.state.ip_limiter = SlidingWindowLimiter(max_requests=100, window_seconds=60)
    return app, sender


def test_login_form_renders(monkeypatch, pg_url):
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/login")
    assert r.status_code == 200
    assert "email" in r.text.lower()


def test_login_post_sends_email_for_allowed_domain(monkeypatch, pg_url, pg_session_factory):
    app, sender = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post("/login", data={"email": "alice@ki.se"}, follow_redirects=False)
    assert r.status_code == 200
    assert "check your email" in r.text.lower()
    assert len(sender.sent) == 1
    assert sender.sent[0].to == "alice@ki.se"
    assert "/login/verify?token=" in sender.sent[0].text_body


def test_login_post_creates_user_on_first_request(monkeypatch, pg_url, pg_session_factory):
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        client.post("/login", data={"email": "brand-new@ki.se"})
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="brand-new@ki.se").one()
        assert u.name is None
        assert u.is_admin is False


def test_login_post_silently_drops_disallowed_domain(monkeypatch, pg_url, pg_session_factory):
    app, sender = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post("/login", data={"email": "intruder@example.com"})
    assert r.status_code == 200
    assert "check your email" in r.text.lower()
    assert sender.sent == []


def test_login_post_is_rate_limited_per_email(monkeypatch, pg_url):
    app, sender = _mk_app(monkeypatch, pg_url)
    app.state.email_limiter = SlidingWindowLimiter(max_requests=2, window_seconds=60)
    with TestClient(app) as client:
        for _ in range(5):
            r = client.post("/login", data={"email": "alice@ki.se"})
            assert r.status_code == 200
    assert len(sender.sent) == 2
```

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_web_auth.py -v`

- [ ] **Step 3: Write `web/routes/auth.py`**

`src/call_monitor/web/routes/auth.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from call_monitor.auth.domain import domain_allowed
from call_monitor.auth.tokens import issue_token
from call_monitor.db.models import User

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return request.app.state.templates.TemplateResponse(
        request=request, name="login.html", context={}
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, email: str = Form(...)):
    settings = request.app.state.settings
    email_limiter = request.app.state.email_limiter
    ip_limiter = request.app.state.ip_limiter
    email_sender = request.app.state.email_sender
    session_factory = request.app.state.session_factory

    email_normalized = email.strip().lower()
    client_ip = request.client.host if request.client else "0.0.0.0"

    # Always show the same page. Never leak rate-limit/allowlist state.
    response = request.app.state.templates.TemplateResponse(
        request=request, name="login_sent.html", context={}
    )

    if not ip_limiter.allow(client_ip):
        return response
    if not email_limiter.allow(email_normalized):
        return response
    if not domain_allowed(email_normalized, settings.allowed_email_domains):
        return response

    with session_factory() as session:  # type: Session
        user = session.query(User).filter_by(email=email_normalized).one_or_none()
        if user is None:
            user = User(email=email_normalized)
            session.add(user)
            session.flush()

        raw = issue_token(session, user_id=user.id, ttl_minutes=15)
        session.commit()

    link = f"{settings.app_base_url.rstrip('/')}/login/verify?token={raw}"
    email_sender.send(
        to=email_normalized,
        subject="Your Call Monitor login link",
        text_body=f"Click to sign in (valid 15 min):\n\n{link}\n",
    )
    return response
```

- [ ] **Step 4: Write the templates**

`src/call_monitor/web/templates/login.html`:

```html
{% extends "base.html" %}
{% block title %}Sign in — Call Monitor{% endblock %}
{% block content %}
<h1>Sign in</h1>
<form method="post" action="/login">
  <label>Email <input type="email" name="email" required></label>
  <button type="submit">Send login link</button>
</form>
{% endblock %}
```

`src/call_monitor/web/templates/login_sent.html`:

```html
{% extends "base.html" %}
{% block title %}Check your email — Call Monitor{% endblock %}
{% block content %}
<h1>Check your email</h1>
<p>If the address is recognized, a login link has been sent. It is valid for 15 minutes.</p>
{% endblock %}
```

- [ ] **Step 5: Wire the router and defaults into `main.py`**

Edit `src/call_monitor/main.py` — extend `create_app` so routers and state defaults are present. Replace the body of `create_app` with:

```python
def create_app() -> FastAPI:
    configure_logging()
    settings = Settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    from call_monitor.auth.email import ConsoleEmailSender, ResendEmailSender
    from call_monitor.auth.rate_limit import SlidingWindowLimiter
    from call_monitor.auth.session_cookie import SessionSigner
    from call_monitor.web.routes import auth, health

    # Email sender selection is environment-gated:
    # - RESEND_API_KEY set                → real ResendEmailSender (any environment).
    # - No key, but cookie_secure=False   → ConsoleEmailSender (dev over plain HTTP).
    # - No key AND cookie_secure=True     → REFUSE to start. Falling back to
    #   ConsoleEmailSender in prod would (a) silently break login delivery and
    #   (b) write the full magic link to stderr/log aggregator — a 15-minute
    #   takeover window for anyone with log access. Loud failure only.
    # Tests override this with InMemoryEmailSender via app.state after creation.
    if settings.resend_api_key:
        email_sender = ResendEmailSender(
            api_key=settings.resend_api_key, from_address=settings.email_from
        )
    elif not settings.cookie_secure:
        email_sender = ConsoleEmailSender()
    else:
        raise RuntimeError(
            "RESEND_API_KEY is required when APP_BASE_URL is https://. "
            "Refusing to fall back to ConsoleEmailSender in production "
            "(would log magic links to stderr — see auth/email.py)."
        )

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.email_sender = email_sender
    app.state.email_limiter = SlidingWindowLimiter(max_requests=5, window_seconds=15 * 60)
    app.state.ip_limiter = SlidingWindowLimiter(max_requests=30, window_seconds=15 * 60)
    app.state.session_signer = SessionSigner(
        secret=settings.session_secret, max_age_seconds=30 * 24 * 3600
    )

    app.include_router(health.router)
    app.include_router(auth.router)
    return app
```

- [ ] **Step 6: Run the auth tests**

Run: `uv run pytest tests/test_web_auth.py -v`
Expected: `5 passed`.

- [ ] **Step 7: Run the whole suite**

Run: `uv run pytest -v`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add src/call_monitor/web/ src/call_monitor/main.py tests/test_web_auth.py
git commit -m "feat: POST /login sends magic link with domain + rate-limit gates"
```

---

## Task 15: Magic-link verification endpoint (GET /login/verify)

**Files:**
- Modify: `src/call_monitor/web/routes/auth.py`
- Modify: `tests/test_web_auth.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_web_auth.py`:

```python
from call_monitor.auth.tokens import issue_token


def test_verify_sets_session_cookie_and_redirects(monkeypatch, pg_url, pg_session_factory):
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.flush()
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()

    with TestClient(app) as client:
        r = client.get(f"/login/verify?token={raw}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/me"
    assert "session" in r.cookies


def test_verify_rejects_unknown_token(monkeypatch, pg_url):
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/login/verify?token=nope", follow_redirects=False)
    assert r.status_code == 400


def test_verify_rejects_replay(monkeypatch, pg_url, pg_session_factory):
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.flush()
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()

    with TestClient(app) as client:
        assert client.get(f"/login/verify?token={raw}", follow_redirects=False).status_code == 303
        r = client.get(f"/login/verify?token={raw}", follow_redirects=False)
    assert r.status_code == 400
```

- [ ] **Step 2: Extend `web/routes/auth.py`**

Append to `src/call_monitor/web/routes/auth.py`:

```python
from datetime import UTC, datetime

from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from call_monitor.auth.tokens import (
    TokenAlreadyConsumed,
    TokenExpired,
    TokenNotFound,
    consume_token,
)


@router.get("/login/verify")
def login_verify(request: Request, token: str):
    session_factory = request.app.state.session_factory
    session_signer = request.app.state.session_signer
    settings = request.app.state.settings

    with session_factory() as session:
        try:
            user_id = consume_token(session, token)
        except (TokenNotFound, TokenExpired, TokenAlreadyConsumed) as e:
            raise HTTPException(status_code=400, detail="invalid or expired link") from e

        from call_monitor.db.models import User  # local import to avoid cycle
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=400, detail="invalid link")
        user.last_login_at = datetime.now(UTC)
        session.commit()

    cookie_value = session_signer.make(user_id=user_id)
    # Foundation has no "/" yet — land the user on /me, which exists.
    # The Ingestion plan will replace this with "/" once the dashboard is introduced.
    response = RedirectResponse(url="/me", status_code=303)
    response.set_cookie(
        "session",
        cookie_value,
        max_age=30 * 24 * 3600,
        httponly=True,
        secure=settings.cookie_secure,  # True when APP_BASE_URL is https://
        samesite="lax",
    )
    return response
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_web_auth.py -v`
Expected: all pass (5 from Task 14 + 3 new).

- [ ] **Step 4: Commit**

```bash
git add src/call_monitor/web/routes/auth.py tests/test_web_auth.py
git commit -m "feat: GET /login/verify consumes magic link and sets session cookie"
```

---

## Task 16: `get_current_user` dependency

**Files:**
- Create: `src/call_monitor/auth/dependencies.py`
- Create: `tests/test_auth_dependencies.py`

- [ ] **Step 1: Write the failing test**

`tests/test_auth_dependencies.py`:

```python
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from call_monitor.auth.dependencies import get_current_user, require_admin
from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import User
from call_monitor.db.session import make_engine, make_session_factory


def _mk_app(pg_url):
    signer = SessionSigner(secret="x" * 32, max_age_seconds=3600)
    engine = make_engine(pg_url)
    session_factory = make_session_factory(engine)

    app = FastAPI()
    app.state.session_signer = signer
    app.state.session_factory = session_factory

    @app.get("/whoami")
    def whoami(user: User = Depends(get_current_user)):
        return {"id": user.id, "email": user.email, "is_admin": user.is_admin}

    @app.get("/admin-only")
    def admin_only(user: User = Depends(require_admin)):
        return {"id": user.id}

    return app, signer, session_factory


def test_whoami_returns_401_without_cookie(pg_url):
    app, _, _ = _mk_app(pg_url)
    with TestClient(app) as client:
        r = client.get("/whoami")
    assert r.status_code == 401


def test_whoami_works_with_valid_cookie(pg_url, pg_session_factory):
    app, signer, _ = _mk_app(pg_url)
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id

    with TestClient(app) as client:
        client.cookies.set("session", signer.make(user_id=uid))
        r = client.get("/whoami")
    assert r.status_code == 200
    assert r.json()["email"] == "alice@ki.se"


def test_require_admin_rejects_non_admin(pg_url, pg_session_factory):
    app, signer, _ = _mk_app(pg_url)
    with pg_session_factory() as s:
        u = User(email="alice@ki.se", is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id

    with TestClient(app) as client:
        client.cookies.set("session", signer.make(user_id=uid))
        r = client.get("/admin-only")
    assert r.status_code == 403


def test_require_admin_allows_admin(pg_url, pg_session_factory):
    app, signer, _ = _mk_app(pg_url)
    with pg_session_factory() as s:
        u = User(email="root@ki.se", is_admin=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id

    with TestClient(app) as client:
        client.cookies.set("session", signer.make(user_id=uid))
        r = client.get("/admin-only")
    assert r.status_code == 200
```

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_auth_dependencies.py -v`

- [ ] **Step 3: Write `auth/dependencies.py`**

```python
from __future__ import annotations

from fastapi import Cookie, HTTPException, Request

from call_monitor.auth.session_cookie import InvalidSession
from call_monitor.db.models import User


def get_current_user(request: Request, session: str | None = Cookie(default=None)) -> User:
    if session is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    signer = request.app.state.session_signer
    session_factory = request.app.state.session_factory
    try:
        user_id = signer.verify(session)
    except InvalidSession as e:
        raise HTTPException(status_code=401, detail="invalid session") from e

    with session_factory() as db:
        user = db.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="user not found")
        db.expunge(user)
    return user


def require_admin(request: Request, session: str | None = Cookie(default=None)) -> User:
    user = get_current_user(request, session=session)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin required")
    return user
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_auth_dependencies.py -v`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor/auth/dependencies.py tests/test_auth_dependencies.py
git commit -m "feat: get_current_user and require_admin FastAPI deps"
```

---

## Task 17: Admin bootstrap on startup

**Files:**
- Create: `src/call_monitor/admin_bootstrap.py`
- Create: `tests/test_admin_bootstrap.py`
- Modify: `src/call_monitor/main.py`

- [ ] **Step 1: Write the failing test**

`tests/test_admin_bootstrap.py`:

```python
from call_monitor.admin_bootstrap import bootstrap_admins
from call_monitor.db.models import User


def test_creates_users_with_admin_flag(pg_session_factory):
    bootstrap_admins(pg_session_factory, emails=["root@ki.se"])
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="root@ki.se").one()
    assert u.is_admin is True


def test_promotes_existing_user(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="alice@ki.se", is_admin=False))
        s.commit()
    bootstrap_admins(pg_session_factory, emails=["alice@ki.se"])
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="alice@ki.se").one()
    assert u.is_admin is True


def test_is_idempotent(pg_session_factory):
    bootstrap_admins(pg_session_factory, emails=["root@ki.se"])
    bootstrap_admins(pg_session_factory, emails=["root@ki.se"])
    with pg_session_factory() as s:
        assert s.query(User).filter_by(email="root@ki.se").count() == 1


def test_no_admins_in_env_is_noop(pg_session_factory):
    bootstrap_admins(pg_session_factory, emails=[])
    with pg_session_factory() as s:
        assert s.query(User).count() == 0
```

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_admin_bootstrap.py -v`

- [ ] **Step 3: Write `admin_bootstrap.py`**

```python
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import sessionmaker

from call_monitor.db.models import User


def bootstrap_admins(
    session_factory: sessionmaker, emails: Iterable[str]
) -> None:
    normalized = [e.strip().lower() for e in emails if e and e.strip()]
    if not normalized:
        return
    with session_factory() as session:
        for email in normalized:
            user = session.query(User).filter_by(email=email).one_or_none()
            if user is None:
                session.add(User(email=email, is_admin=True))
            elif not user.is_admin:
                user.is_admin = True
        session.commit()
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_admin_bootstrap.py -v`
Expected: `4 passed`.

- [ ] **Step 5: Wire into startup in `main.py`**

Edit `src/call_monitor/main.py` — replace the `lifespan` function:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    from call_monitor.admin_bootstrap import bootstrap_admins
    bootstrap_admins(
        app.state.session_factory,
        emails=app.state.settings.initial_admin_emails,
    )
    yield
```

- [ ] **Step 6: Run whole suite**

Run: `uv run pytest -v`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/call_monitor/admin_bootstrap.py src/call_monitor/main.py tests/test_admin_bootstrap.py
git commit -m "feat: first-admin bootstrap from INITIAL_ADMIN_EMAIL on startup"
```

---

## Task 18: `/me` page (name editing)

**Files:**
- Create: `src/call_monitor/web/routes/me.py`
- Create: `src/call_monitor/web/templates/me.html`
- Modify: `src/call_monitor/main.py`
- Create: `tests/test_web_me.py`

- [ ] **Step 1: Write the failing test**

`tests/test_web_me.py`:

```python
from fastapi.testclient import TestClient

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import User
from call_monitor.main import create_app


def _logged_in_client(monkeypatch, pg_url, pg_session_factory, *, email="alice@ki.se"):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()

    with pg_session_factory() as s:
        u = User(email=email)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id

    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client, uid


def test_me_requires_auth(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/me")
    assert r.status_code == 401


def test_me_renders_current_user(monkeypatch, pg_url, pg_session_factory):
    client, _ = _logged_in_client(monkeypatch, pg_url, pg_session_factory)
    with client:
        r = client.get("/me")
    assert r.status_code == 200
    assert "alice@ki.se" in r.text


def test_me_post_updates_name(monkeypatch, pg_url, pg_session_factory):
    client, uid = _logged_in_client(monkeypatch, pg_url, pg_session_factory)
    with client:
        r = client.post("/me", data={"name": "Alice Andersson"})
    assert r.status_code == 200
    with pg_session_factory() as s:
        u = s.get(User, uid)
        assert u.name == "Alice Andersson"
```

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_web_me.py -v`

- [ ] **Step 3: Write `web/routes/me.py`**

```python
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from call_monitor.auth.dependencies import get_current_user
from call_monitor.db.models import User

router = APIRouter()


@router.get("/me", response_class=HTMLResponse)
def me_page(request: Request, user: User = Depends(get_current_user)):
    return request.app.state.templates.TemplateResponse(
        request=request, name="me.html", context={"user": user}
    )


@router.post("/me", response_class=HTMLResponse)
def update_me(
    request: Request,
    name: str = Form(""),
    user: User = Depends(get_current_user),
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        db_user = session.get(User, user.id)
        db_user.name = name.strip() or None
        session.commit()
        session.refresh(db_user)
        # Re-render with updated user
        return request.app.state.templates.TemplateResponse(
            request=request, name="me.html", context={"user": db_user}
        )
```

- [ ] **Step 4: Write `templates/me.html`**

```html
{% extends "base.html" %}
{% block title %}Your profile — Call Monitor{% endblock %}
{% block content %}
<h1>Your profile</h1>
<p>Signed in as <code>{{ user.email }}</code>{% if user.is_admin %} (admin){% endif %}.</p>

<form method="post" action="/me">
  <label>Display name
    <input type="text" name="name" value="{{ user.name or '' }}" maxlength="200">
  </label>
  <button type="submit">Save</button>
</form>
{% endblock %}
```

- [ ] **Step 5: Register the router in `main.py`**

In the `create_app` factory, add the import and `include_router`:

```python
from call_monitor.web.routes import auth, health, me
...
app.include_router(me.router)
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_web_me.py -v`
Expected: `3 passed`.

- [ ] **Step 7: Commit**

```bash
git add src/call_monitor/web/routes/me.py src/call_monitor/web/templates/me.html src/call_monitor/main.py tests/test_web_me.py
git commit -m "feat: /me page with display-name editing"
```

---

## Task 19: End-to-end login flow integration test

**Files:**
- Create: `tests/test_login_flow.py`

- [ ] **Step 1: Write the test**

`tests/test_login_flow.py`:

```python
from fastapi.testclient import TestClient

from call_monitor.auth.email import InMemoryEmailSender
from call_monitor.auth.rate_limit import SlidingWindowLimiter
from call_monitor.main import create_app


def test_full_login_flow(monkeypatch, pg_url, pg_session_factory):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")

    app = create_app()
    sender = InMemoryEmailSender()
    app.state.email_sender = sender
    app.state.email_limiter = SlidingWindowLimiter(max_requests=10, window_seconds=60)
    app.state.ip_limiter = SlidingWindowLimiter(max_requests=100, window_seconds=60)

    with TestClient(app) as client:
        # 1. Request magic link
        r = client.post("/login", data={"email": "alice@ki.se"})
        assert r.status_code == 200
        assert len(sender.sent) == 1
        body = sender.sent[0].text_body
        link = [line for line in body.splitlines() if "login/verify" in line][0].strip()
        # Strip host so TestClient can use it
        path = link[link.index("/login/verify"):]

        # 2. Click link → redirected, cookie set
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303
        assert "session" in client.cookies

        # 3. /me works
        r = client.get("/me")
        assert r.status_code == 200
        assert "alice@ki.se" in r.text

        # 4. Replay of the same link is rejected
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 400
```

- [ ] **Step 2: Run**

Run: `uv run pytest tests/test_login_flow.py -v`
Expected: `1 passed`.

- [ ] **Step 3: Run whole suite**

Run: `uv run pytest -v`
Expected: everything green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_login_flow.py
git commit -m "test: end-to-end magic-link login flow"
```

---

## Task 20: CI (GitHub Actions)

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Write the workflow**

`.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [main, master]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v3
        with:
          version: "0.4.x"

      - name: Set up Python
        run: uv python install 3.12

      - name: Sync deps
        run: uv sync --all-extras

      - name: Ruff check
        run: uv run ruff check .

      - name: Pytest
        run: uv run pytest -v
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: GitHub Actions running ruff + pytest on push/PR"
```

- [ ] **Step 3: Push and verify CI passes**

```bash
git push
```
Then check the Actions tab on GitHub — expected: green.

---

## Verification at end of plan

After all 20 tasks:

1. `uv run ruff check .` → clean
2. `uv run pytest -v` → all green
3. Manual smoke: `uv run uvicorn call_monitor.main:create_app --factory --reload`, then:
   - `curl localhost:8000/healthz` → `{"status": "ok"}`
   - Browse to `/login`, enter an allowed-domain email, **copy the magic link from the app's stderr logs** (the default `ConsoleEmailSender` writes `event=email_sent` with the body), paste into browser → redirect lands on `/me` with session cookie set.

## What the next plan (Ingestion) adds on top

- `sources/` module (adapter protocol + EU Portal + Vinnova adapters)
- `ingestion/` orchestrator
- APScheduler wired into the lifespan
- `sources` and `calls` tables + migration
- `/` dashboard listing recent calls (no ranking yet)
