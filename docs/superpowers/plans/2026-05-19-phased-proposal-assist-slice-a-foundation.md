# Phased Proposal-Assist — Slice A (Foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the schema, ingestion-time extractor modules, and Phase 0 background worker (plus its scheduler tick) for the phased proposal-assist workflow. End state: given a `workspace_setup_jobs` row in status `queued` (whoever enqueues it — workspace-create hook is base-spec territory), the in-process scheduler ticks the worker, the worker snapshots call body + profile description, ensures section extracts + call rubric are fresh (re-extracting if stale), copies both into per-workspace columns, writes a `rubric_initialized` audit event, and blocks at `waiting_for_rubric_review` until the workspace owner confirms the rubric — at which point `current_phase` advances to `spec`.

**Not in this slice:** the workspace-create hook that enqueues the setup job (base spec owns workspace creation, so the enqueue site lives in that PR). Slice A delivers a *drainable* Phase 0 queue; whoever creates the workspace inserts the job row.

**Architecture:** Module-isolated package at `src/call_monitor/proposal_assist/` with submodules for extractors, the Phase 0 worker, and the owner-facing rubric review endpoint. Templates in `call_section_extracts` and `call_rubrics` are per-call (shared) and re-extracted only when `source_text_hash` or `extractor_version` changes; workspaces snapshot from those templates at Phase 0 and never see them again. The rubric audit reproducibility surface is a pair of `workspace_events` rows (`rubric_initialized` + `rubric_edited`) replayed forward from the seed payload.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x + Alembic, FastAPI + Jinja2 + HTMX, Anthropic SDK (via existing `AnthropicClient`), pytest + testcontainers (real Postgres in tests, mocked LLM via stubbed `AnthropicClient.chat`).

**Spec:** [docs/superpowers/specs/2026-05-19-proposal-assist-phased-workflow-design.md](../specs/2026-05-19-proposal-assist-phased-workflow-design.md)

---

## Revision history

- **rev2 (2026-05-19)** — Codex review fixed: test snippets now use the real `pg_session_factory` fixture (not the imaginary `db_session`); `Source`/`Profile` constructors match the actual models; Settings tests use the project's `_settings_env` env-var fixture; `ensure_section_extracts_fresh` reorders to "extract first, then atomically replace" so a transient LLM failure cannot wipe cached rows; `Phase0Worker.run_one` rolls back before mutating job status on the failure path; web-endpoint imports use the real paths (`call_monitor.auth.dependencies`, `call_monitor.db.session.get_db_dependency`, app-state `templates` and `session_signer`); TestClient pattern matches `tests/test_web_admin_digest_panel.py`; Task 8 milestone language softened (ingestion-tick integration is Slice E, not Slice A); manual-paste rubric fallback explicitly deferred (spec lines 290, 517).

- **rev3 (2026-05-19)** — Codex round 2 fixed: Phase 0 worker now wired into the existing APScheduler via a new Task 19 (registers an interval job + adds a `phase0_tick_seconds` setting); workspace-create enqueue called out as base-spec territory in the goal; Part 2 migration IDs (`0014`–`0017`) called out as placeholders with a discovery instruction at the top of Part 2; rubric-review GET handler dropped the broken `parents[3]` path math in favor of `request.app.state.templates`, with a new wire-up step that adds `app.state.templates` and `app.state.session_factory` to `create_app()`; POST handler dropped the unused `Form`/`RedirectResponse` imports and replaced the body-level meta-refresh with proper HX-Redirect (HTMX) / 303 (non-HTMX) handling (tests updated to `follow_redirects=False` + expect 303); file map no longer lists a non-existent `proposal_assist/db.py`; task range corrected from `9–26` to `9–21`; Part 1 milestone wording no longer claims "in production."

---

## Project Conventions Reference

Read this once before executing any task. Every test snippet below assumes these patterns.

### Session / DB fixture

The repo's `tests/conftest.py` provides `pg_session_factory: () -> SessionContextManager`. Use it like this:

```python
def test_something(pg_session_factory, _settings_env):
    with pg_session_factory() as s:
        s.add(some_row)
        s.commit()
        # ... assertions on the same session
```

**There is no `db_session` fixture.** When the snippets below say `with pg_session_factory() as s`, that's the entire session lifetime for that test step.

The factory's teardown TRUNCATEs every table (`Base.metadata.sorted_tables`, CASCADE, RESTART IDENTITY) and re-seeds the two `sources` rows (`eu_portal`, `vinnova`) — so every test starts with those two sources present and IDs reset to 1.

### Settings env vars

`Settings()` requires `DATABASE_URL`, `SESSION_SECRET` (≥16 chars), `ALLOWED_EMAIL_DOMAINS`, `APP_BASE_URL`. Any test that instantiates `Settings()` directly needs the project's `_settings_env` helper. Copy this verbatim into `tests/proposal_assist/conftest.py`:

```python
@pytest.fixture
def _settings_env(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
```

Use it as a function-scoped dependency: any test that calls `Settings()` or `create_app()` declares `_settings_env` in its arg list.

### Model constructor shapes

Verified against `src/call_monitor/db/models.py`:

- `Source(id, name, slug, last_fetched_at, config, created_at)` — **no `kind`, no `is_active`**. Rows auto-seeded by `pg_session_factory` teardown; tests look them up by slug rather than constructing new ones.
- `Call(source_id, external_id, url, title, summary, full_text, deadline, fetched_at, ...)` — `full_text` is nullable; `external_id` and `url` and `title` are required.
- `Profile(name, kind: ProfileKind, description, created_by: FK users.id, ...)` — **no `slug`**, **`kind` required (enum)**, **`created_by` required**. Use `ProfileKind.GROUP` / `.ORG` / `.USER`. The repo's existing `profile_factory` fixture (`tests/conftest.py`) demonstrates the construction.
- `User(email, name=None, is_admin=False, ...)` — `id` is `BigInteger`, not UUID.

### Web app construction

`create_app()` takes **no `test_mode` argument**. The pattern for TestClient-based tests (from `tests/test_web_admin_digest_panel.py`):

```python
from fastapi.testclient import TestClient
from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _authed_client(app, user_id: int) -> TestClient:
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()  # lifespan runs (admin bootstrap, scheduler, etc.)
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=user_id))
    return client
```

The client must be exited (`client.__exit__(None, None, None)`) after the test request.

### Web infrastructure imports

- Auth dependency: `from call_monitor.auth.dependencies import get_current_user`
- DB session dependency: `from call_monitor.db.session import get_db_dependency` — call as `Depends(get_db_dependency(app.state.session_factory))`, or store the session factory on `app.state` at startup and access it from a thin module-level helper.
- Templates: `Jinja2Templates` is instantiated locally in `create_app()` (search `src/call_monitor/main.py` for the `templates = Jinja2Templates(...)` line). For new endpoints, the simplest pattern is to instantiate a module-local `Jinja2Templates(directory=TEMPLATES_DIR)` and pass `request` into `templates.TemplateResponse(request, ...)`. See `src/call_monitor/web/routes.py` or wherever the existing routes render HTML.

### Session signer for cookie creation

`app.state.session_signer: SessionSigner` is set during `create_app()`. Use `signer.make(user_id=u.id)` to produce a session-cookie value.

### Pattern for Part 2 test snippets

Every Part 2 test that touches a workspace follows this shape (factor into `tests/proposal_assist/conftest.py`):

```python
def make_owner_and_workspace(
    session, *, owner_email: str = "owner@ki.se",
    call_id: int | None = None, profile_id: int | None = None,
) -> tuple[int, "uuid.UUID"]:
    """Create a User + a Workspace owned by them; return their IDs.

    Workspace defaults: current_phase='setup', rubric_criteria=[],
    section_extracts={}. Optionally link to a Call (created via make_call)
    and/or a Profile.
    """
    from call_monitor.db.models import User, Workspace
    u = User(email=owner_email)
    session.add(u)
    session.flush()
    ws = Workspace(
        owner_id=u.id, title="Test workspace",
        call_id=call_id, profile_id=profile_id,
    )
    session.add(ws)
    session.commit()
    return u.id, ws.id
```

Snippets in Tasks 9–19 use `make_owner_and_workspace` for the owner+workspace setup and `make_call` (defined in Task 3) for the call. Both return only IDs so tests can use them across separate `pg_session_factory()` blocks without dealing with detached-instance errors.

---

## Prerequisites and scope honesty

This slice has two parts with different blockers:

**Part 1 — Buildable today** (Tasks 1–8): Settings, hashing utility, `call_section_extracts` template + extractor, `call_rubrics` template + extractor. These touch only `calls` (which exists) and add isolated new tables. Constitutes the *module* side of the spec's implementation-gate item 3 (section-aware extractor); the ingestion-tick integration that would make it "in production" is owned by Slice E and explicitly out of Slice A scope (see Out of Scope at the bottom).

**Part 2 — Blocked on base spec** (Tasks 9–21): everything that references `workspaces`, `workspace_viewers`, or `workspace_events`. The base spec ([2026-04-18-call-manager-proposal-assist-design.md](../specs/2026-04-18-call-manager-proposal-assist-design.md)) defines those tables, the Agent SDK adapter, the per-workspace subprocess, and the `call-monitor → call-manager` rename. **Do not attempt Tasks 9+ until base spec has shipped at minimum** `workspaces`, `workspace_viewers`, `workspace_events`, and any structural change required by the renamed `src/call_manager/` package layout. If executing under the un-renamed `call-monitor` package, use `src/call_monitor/proposal_assist/` paths and update the import roots when the rename happens.

When you reach the Part 2 boundary, stop and surface this to the user before proceeding.

---

## File map

**Created:**
- `src/call_monitor/proposal_assist/__init__.py`
- `src/call_monitor/proposal_assist/hashing.py`
- `src/call_monitor/proposal_assist/extractors/__init__.py`
- `src/call_monitor/proposal_assist/extractors/sections.py`
- `src/call_monitor/proposal_assist/extractors/rubric.py`
- `src/call_monitor/proposal_assist/phases/__init__.py`
- `src/call_monitor/proposal_assist/phases/setup.py`
- `src/call_monitor/proposal_assist/web/__init__.py`
- `src/call_monitor/proposal_assist/web/rubric_review.py`
- `src/call_monitor/web/templates/proposal_assist/rubric_review.html`
- `alembic/versions/0012_call_section_extracts.py`
- `alembic/versions/0013_call_rubrics.py`
- `alembic/versions/0014_workspaces_phase_columns.py`
- `alembic/versions/0015_phase_approvals.py`
- `alembic/versions/0016_workspace_setup_jobs.py`
- `alembic/versions/0017_review_runs_and_items.py`
- `tests/proposal_assist/__init__.py`
- `tests/proposal_assist/conftest.py`
- `tests/proposal_assist/test_models.py`
- `tests/proposal_assist/test_hashing.py`
- `tests/proposal_assist/test_extractors_sections.py`
- `tests/proposal_assist/test_extractors_rubric.py`
- `tests/proposal_assist/test_phase0_setup.py`
- `tests/proposal_assist/test_rubric_review_endpoint.py`

**Modified:**
- `src/call_monitor/config.py` — add `EXTRACTOR_VERSION`, `RUBRIC_EXTRACTOR_VERSION`, `PROPOSAL_ASSIST_LLM_MODEL` settings.
- `src/call_monitor/db/models.py` — add all new SQLAlchemy models here. The existing codebase keeps every model in this one file; we follow that pattern (no separate `proposal_assist/db.py`).
- `src/call_monitor/main.py` — register the rubric-review router.

---

# Part 1 — Buildable today (no base-spec dependency)

## Task 1: Settings additions

**Files:**
- Modify: `src/call_monitor/config.py`
- Test: `tests/proposal_assist/test_config.py` (created in this task)

- [ ] **Step 1: Create test files with failing test**

Create `tests/proposal_assist/__init__.py` (empty).

Create `tests/proposal_assist/conftest.py`:

```python
"""Shared fixtures for proposal_assist tests. See plan's
'Project Conventions Reference' section for context."""
import pytest


@pytest.fixture
def _settings_env(monkeypatch, pg_url):
    """Populate the env vars Settings() requires.

    Without these, pydantic validation raises during Settings(), so any test
    that constructs Settings directly OR via create_app() must depend on this.
    """
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
```

Create `tests/proposal_assist/test_config.py`:

```python
from call_monitor.config import Settings


def test_proposal_assist_settings_have_defaults(monkeypatch, _settings_env):
    # _settings_env provides DATABASE_URL etc.; clear only the new keys
    # to verify they take their declared defaults.
    monkeypatch.delenv("EXTRACTOR_VERSION", raising=False)
    monkeypatch.delenv("RUBRIC_EXTRACTOR_VERSION", raising=False)
    monkeypatch.delenv("PROPOSAL_ASSIST_LLM_MODEL", raising=False)
    s = Settings()
    assert s.extractor_version == "sections-v1"
    assert s.rubric_extractor_version == "rubric-v1"
    assert s.proposal_assist_llm_model == "claude-opus-4-7"


def test_proposal_assist_settings_override_via_env(monkeypatch, _settings_env):
    monkeypatch.setenv("EXTRACTOR_VERSION", "sections-v2")
    monkeypatch.setenv("RUBRIC_EXTRACTOR_VERSION", "rubric-v2")
    monkeypatch.setenv("PROPOSAL_ASSIST_LLM_MODEL", "claude-sonnet-4-6")
    s = Settings()
    assert s.extractor_version == "sections-v2"
    assert s.rubric_extractor_version == "rubric-v2"
    assert s.proposal_assist_llm_model == "claude-sonnet-4-6"
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_config.py -v
```

Expected: `AttributeError: 'Settings' object has no attribute 'extractor_version'`.

- [ ] **Step 3: Add the three fields to `src/call_monitor/config.py`**

Inside the existing `class Settings(BaseSettings):` block, add (after the existing `ANTHROPIC_API_KEY` field — pick a stable location):

```python
    extractor_version: str = Field(
        default="sections-v1",
        alias="EXTRACTOR_VERSION",
        description="Bump when the section extractor prompt or output schema changes; "
                    "stored on call_section_extracts.extractor_version and used by Phase 0 "
                    "to decide whether to re-extract.",
    )
    rubric_extractor_version: str = Field(
        default="rubric-v1",
        alias="RUBRIC_EXTRACTOR_VERSION",
        description="Bump when the rubric extractor prompt or output schema changes.",
    )
    proposal_assist_llm_model: str = Field(
        default="claude-opus-4-7",
        alias="PROPOSAL_ASSIST_LLM_MODEL",
        description="Anthropic model id used for proposal-assist extractors, "
                    "score_against_rubric, fit-check, persona scoring, and consolidation.",
    )
```

- [ ] **Step 4: Run to confirm passing**

```bash
uv run pytest tests/proposal_assist/test_config.py -v
```

Expected: both tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor/config.py tests/proposal_assist/__init__.py tests/proposal_assist/conftest.py tests/proposal_assist/test_config.py
git commit -m "feat(proposal-assist): add extractor + LLM-model settings"
```

---

## Task 2: sha256 hashing utility

**Files:**
- Create: `src/call_monitor/proposal_assist/__init__.py` (empty)
- Create: `src/call_monitor/proposal_assist/hashing.py`
- Test: `tests/proposal_assist/test_hashing.py`

- [ ] **Step 1: Write failing test**

`tests/proposal_assist/test_hashing.py`:

```python
import pytest

from call_monitor.proposal_assist.hashing import source_text_hash


def test_source_text_hash_is_64_lowercase_hex():
    h = source_text_hash("hello world")
    assert len(h) == 64
    assert h == h.lower()
    int(h, 16)  # raises ValueError if not valid hex


def test_source_text_hash_stable():
    assert source_text_hash("hello") == source_text_hash("hello")


def test_source_text_hash_differs_on_content():
    assert source_text_hash("a") != source_text_hash("b")


def test_source_text_hash_treats_empty_string():
    h = source_text_hash("")
    # sha256("") is well-known
    assert h == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_source_text_hash_rejects_non_str():
    with pytest.raises(TypeError):
        source_text_hash(b"bytes are not allowed")
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_hashing.py -v
```

Expected: `ModuleNotFoundError: No module named 'call_monitor.proposal_assist'`.

- [ ] **Step 3: Implement**

Create `src/call_monitor/proposal_assist/__init__.py` as empty file. Create `src/call_monitor/proposal_assist/hashing.py`:

```python
from __future__ import annotations

import hashlib


def source_text_hash(text: str) -> str:
    """Stable sha256 hex digest of a UTF-8 string. Used by both extractors to
    decide whether their cached output is fresh against the current call body.

    Non-str input raises TypeError eagerly so callers don't silently store the
    wrong hash for bytes / None / etc.
    """
    if not isinstance(text, str):
        raise TypeError(f"source_text_hash expects str, got {type(text).__name__}")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run to confirm passing**

```bash
uv run pytest tests/proposal_assist/test_hashing.py -v
```

Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor/proposal_assist/__init__.py src/call_monitor/proposal_assist/hashing.py tests/proposal_assist/test_hashing.py
git commit -m "feat(proposal-assist): add source_text_hash sha256 utility"
```

---

## Task 3: `call_section_extracts` migration + model

**Files:**
- Create: `alembic/versions/0012_call_section_extracts.py`
- Modify: `src/call_monitor/db/models.py`
- Test: `tests/proposal_assist/test_models.py` (created here)

- [ ] **Step 1: Extend conftest.py with a call-builder helper**

Append to `tests/proposal_assist/conftest.py`:

```python
from datetime import datetime, timezone

from sqlalchemy import select

from call_monitor.db.models import Call, Source


def make_call(session, *, external_id: str = "EXT-1",
              full_text: str = "Body about eligibility, scope, budget, "
                               "expected outcomes, restrictions, mandatory.",
              source_slug: str = "vinnova") -> Call:
    """Build a Call row attached to one of the auto-seeded sources.

    `vinnova` and `eu_portal` are seeded by pg_session_factory's teardown;
    look them up by slug rather than constructing a new Source.
    """
    src = session.execute(
        select(Source).where(Source.slug == source_slug)
    ).scalar_one()
    call = Call(
        source_id=src.id,
        external_id=external_id,
        title="Precision medicine programme",
        url="https://example.org/calls/" + external_id,
        summary="Funding for AI-based diagnostics.",
        full_text=full_text,
        deadline=datetime(2026, 12, 1, tzinfo=timezone.utc),
    )
    session.add(call)
    session.flush()
    return call
```

- [ ] **Step 2: Write the failing test**

`tests/proposal_assist/test_models.py`:

```python
from datetime import datetime

import pytest
from sqlalchemy import select

from call_monitor.db.models import CallSectionExtract
from tests.proposal_assist.conftest import make_call


def test_call_section_extract_can_be_inserted(pg_session_factory):
    with pg_session_factory() as s:
        call = make_call(s)
        s.add(CallSectionExtract(
            call_id=call.id,
            section="eligibility",
            extract_text="Open to Swedish universities.",
            extracted_by_model="claude-opus-4-7",
            extractor_version="sections-v1",
            source_text_hash="0" * 64,
        ))
        s.commit()

        fetched = s.scalar(
            select(CallSectionExtract).where(
                CallSectionExtract.call_id == call.id,
                CallSectionExtract.section == "eligibility",
            )
        )
        assert fetched is not None
        assert fetched.extract_text == "Open to Swedish universities."
        assert fetched.extractor_version == "sections-v1"
        assert isinstance(fetched.extracted_at, datetime)


def test_call_section_extract_section_check_constraint(pg_session_factory):
    with pg_session_factory() as s:
        call = make_call(s)
        s.add(CallSectionExtract(
            call_id=call.id,
            section="not_a_real_section",  # CHECK violation
            extract_text="x",
            extracted_by_model="m",
            extractor_version="sections-v1",
            source_text_hash="0" * 64,
        ))
        with pytest.raises(Exception):
            s.commit()
```

- [ ] **Step 3: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_models.py -v
```

Expected: `ImportError: cannot import name 'CallSectionExtract' from 'call_monitor.db.models'`.

- [ ] **Step 4: Write the migration**

Create `alembic/versions/0012_call_section_extracts.py`:

```python
"""call_section_extracts template table

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-19
"""

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "call_section_extracts",
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("extract_text", sa.Text(), nullable=False),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("extracted_by_model", sa.Text(), nullable=False),
        sa.Column("extractor_version", sa.Text(), nullable=False),
        sa.Column("source_text_hash", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["call_id"], ["calls.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("call_id", "section"),
        sa.CheckConstraint(
            "section IN ('eligibility','scope','budget',"
            "'expected_outcomes','restrictions','mandatory')",
            name="ck_call_section_extracts_section",
        ),
    )


def downgrade() -> None:
    op.drop_table("call_section_extracts")
```

- [ ] **Step 5: Add the SQLAlchemy model**

Edit `src/call_monitor/db/models.py`, near the bottom of the file (after the last existing model). Add:

```python
class CallSectionExtract(Base):
    __tablename__ = "call_section_extracts"

    call_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("calls.id", ondelete="CASCADE"),
        primary_key=True,
    )
    section: Mapped[str] = mapped_column(Text, primary_key=True)
    extract_text: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    extracted_by_model: Mapped[str] = mapped_column(Text, nullable=False)
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False)
    source_text_hash: Mapped[str] = mapped_column(Text, nullable=False)
```

Verify the existing imports at the top of `models.py` already include `Text`, `func`, `DateTime`, `BigInteger`, `ForeignKey`, `Mapped`, `mapped_column`. If any are missing, add them.

- [ ] **Step 6: Apply the migration in the test DB and run tests**

```bash
uv run alembic upgrade head
uv run pytest tests/proposal_assist/test_models.py -v
```

Expected: 2 tests pass. If the testcontainers fixture spins up a fresh DB and runs migrations automatically, just the pytest command suffices — confirm by reading the project's existing `tests/conftest.py`.

- [ ] **Step 7: Commit**

```bash
git add alembic/versions/0012_call_section_extracts.py src/call_monitor/db/models.py tests/proposal_assist/conftest.py tests/proposal_assist/test_models.py
git commit -m "feat(proposal-assist): add call_section_extracts template table"
```

---

## Task 4: Section extractor module

**Files:**
- Create: `src/call_monitor/proposal_assist/extractors/__init__.py` (empty)
- Create: `src/call_monitor/proposal_assist/extractors/sections.py`
- Test: `tests/proposal_assist/test_extractors_sections.py`

- [ ] **Step 1: Write failing tests**

`tests/proposal_assist/test_extractors_sections.py`:

```python
import json
from unittest.mock import Mock

import pytest

from call_monitor.proposal_assist.extractors.sections import (
    SECTION_NAMES,
    extract_sections,
)


def _envelope(content: str) -> dict:
    """Build the OpenAI-shaped envelope AnthropicClient.chat returns."""
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "model": "claude-opus-4-7",
    }


def test_extract_sections_returns_six_named_keys():
    payload = {section: f"text for {section}" for section in SECTION_NAMES}
    client = Mock()
    client.chat.return_value = _envelope(json.dumps(payload))
    client.model = "claude-opus-4-7"

    result = extract_sections(call_text="dummy call body", client=client)

    assert set(result.keys()) == set(SECTION_NAMES)
    for section, text in result.items():
        assert text == f"text for {section}"


def test_extract_sections_fills_missing_sections_with_empty_string():
    # Real LLM output sometimes omits a section entirely; the extractor must
    # backfill so callers always see all six keys.
    partial = {"eligibility": "stuff"}
    client = Mock()
    client.chat.return_value = _envelope(json.dumps(partial))
    client.model = "claude-opus-4-7"

    result = extract_sections(call_text="dummy", client=client)

    assert result["eligibility"] == "stuff"
    assert all(result[s] == "" for s in SECTION_NAMES if s != "eligibility")


def test_extract_sections_raises_on_malformed_json():
    client = Mock()
    client.chat.return_value = _envelope("not json at all")
    client.model = "claude-opus-4-7"

    with pytest.raises(ValueError, match="malformed JSON"):
        extract_sections(call_text="dummy", client=client)


def test_extract_sections_raises_on_non_str_value():
    client = Mock()
    client.chat.return_value = _envelope(json.dumps({"eligibility": 42}))
    client.model = "claude-opus-4-7"

    with pytest.raises(ValueError, match="expected str"):
        extract_sections(call_text="dummy", client=client)


def test_extract_sections_passes_call_text_in_user_message():
    client = Mock()
    client.chat.return_value = _envelope(
        json.dumps({s: "" for s in SECTION_NAMES})
    )
    client.model = "claude-opus-4-7"

    extract_sections(call_text="UNIQUE-CALL-BODY-TEXT", client=client)

    messages = client.chat.call_args.kwargs["messages"]
    user_msg = next(m for m in messages if m["role"] == "user")
    assert "UNIQUE-CALL-BODY-TEXT" in user_msg["content"]
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_extractors_sections.py -v
```

Expected: `ModuleNotFoundError` for `call_monitor.proposal_assist.extractors.sections`.

- [ ] **Step 3: Implement the module**

Create `src/call_monitor/proposal_assist/extractors/__init__.py` (empty).

Create `src/call_monitor/proposal_assist/extractors/sections.py`:

```python
"""Section-aware extractor — single Opus call that splits a call body into
six named sections. Per-call, ingestion-time. Output shape is fixed; the
Phase 0 worker persists the result into `call_section_extracts` (template)
and then copies it into `workspaces.section_extracts`.
"""
from __future__ import annotations

import json
from typing import Protocol

SECTION_NAMES: tuple[str, ...] = (
    "eligibility",
    "scope",
    "budget",
    "expected_outcomes",
    "restrictions",
    "mandatory",
)

_SYSTEM_PROMPT = """You are an extractor. You receive a funding-call body and
return a JSON object that splits its content into six named sections.

Output schema — STRICTLY this shape, no other keys, no prose around the JSON:

{
  "eligibility": "...",      // who can apply
  "scope": "...",            // what the call funds
  "budget": "...",           // budget ranges, currencies, durations
  "expected_outcomes": "...", // deliverables / impact targets
  "restrictions": "...",     // exclusions, ineligibilities, dual-use rules
  "mandatory": "..."         // must-include items (forms, signatures, etc.)
}

If a section is absent from the source, return an empty string for that key —
do NOT omit the key, do NOT invent content. Quote sparingly; the goal is a
faithful extract, not a paraphrase. Each value: at most 1500 characters.
"""


class _Client(Protocol):
    model: str

    def chat(
        self,
        *,
        messages: list[dict],
        response_format: dict | None = None,
    ) -> dict: ...


def extract_sections(*, call_text: str, client: _Client) -> dict[str, str]:
    """Run the extractor against `call_text`. Returns a dict keyed by every
    name in SECTION_NAMES (missing ones backfilled to ""). Raises ValueError
    on malformed JSON or wrong value types.

    Does NOT persist — the caller is responsible for writing rows + computing
    `source_text_hash` of the input.
    """
    envelope = client.chat(
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": call_text},
        ],
        response_format={"type": "json_object"},
    )
    content = envelope["choices"][0]["message"]["content"]

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"malformed JSON from section extractor: {e}") from e

    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSON object, got {type(parsed).__name__}")

    result: dict[str, str] = {}
    for section in SECTION_NAMES:
        value = parsed.get(section, "")
        if not isinstance(value, str):
            raise ValueError(
                f"section {section!r}: expected str, got {type(value).__name__}"
            )
        result[section] = value
    return result
```

- [ ] **Step 4: Run to confirm passing**

```bash
uv run pytest tests/proposal_assist/test_extractors_sections.py -v
```

Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor/proposal_assist/extractors/__init__.py src/call_monitor/proposal_assist/extractors/sections.py tests/proposal_assist/test_extractors_sections.py
git commit -m "feat(proposal-assist): add section extractor (sections-v1)"
```

---

## Task 5: `call_rubrics` migration + model

**Files:**
- Create: `alembic/versions/0013_call_rubrics.py`
- Modify: `src/call_monitor/db/models.py`
- Test: extend `tests/proposal_assist/test_models.py`

- [ ] **Step 1: Append failing test**

Append to `tests/proposal_assist/test_models.py`:

```python
from datetime import datetime, timezone

from call_monitor.db.models import CallRubric


def test_call_rubric_can_be_inserted(pg_session_factory):
    with pg_session_factory() as s:
        call = make_call(s)
        s.add(CallRubric(
            call_id=call.id,
            criteria=[
                {"name": "Scientific excellence", "description": "...",
                 "weight": 0.4, "scale_max": 5},
                {"name": "Clinical impact", "description": "...",
                 "weight": 0.6, "scale_max": 5},
            ],
            funder_notes="See FAQ on funder website.",
            extracted_at=datetime.now(timezone.utc),
            extracted_by_model="claude-opus-4-7",
            extractor_version="rubric-v1",
            source_text_hash="0" * 64,
        ))
        s.commit()
        fetched = s.get(CallRubric, call.id)
        assert fetched is not None
        assert len(fetched.criteria) == 2
        assert fetched.criteria[0]["name"] == "Scientific excellence"
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_models.py::test_call_rubric_can_be_inserted -v
```

Expected: `ImportError: cannot import name 'CallRubric'`.

- [ ] **Step 3: Write the migration**

Create `alembic/versions/0013_call_rubrics.py`:

```python
"""call_rubrics template table

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "call_rubrics",
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "criteria",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("funder_notes", sa.Text(), nullable=True),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extracted_by_model", sa.Text(), nullable=False),
        sa.Column("extractor_version", sa.Text(), nullable=False),
        sa.Column("source_text_hash", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["call_id"], ["calls.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("call_id"),
    )


def downgrade() -> None:
    op.drop_table("call_rubrics")
```

- [ ] **Step 4: Add the model**

In `src/call_monitor/db/models.py`, after the `CallSectionExtract` class:

```python
class CallRubric(Base):
    __tablename__ = "call_rubrics"

    call_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("calls.id", ondelete="CASCADE"),
        primary_key=True,
    )
    criteria: Mapped[list[dict]] = mapped_column(
        postgresql.JSONB, nullable=False
    )
    funder_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    extracted_by_model: Mapped[str] = mapped_column(Text, nullable=False)
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False)
    source_text_hash: Mapped[str] = mapped_column(Text, nullable=False)
```

If `postgresql` isn't already imported, add at the top of the file:

```python
from sqlalchemy.dialects import postgresql
```

- [ ] **Step 5: Migrate + run tests**

```bash
uv run alembic upgrade head
uv run pytest tests/proposal_assist/test_models.py -v
```

Expected: all model tests pass (3 now).

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/0013_call_rubrics.py src/call_monitor/db/models.py tests/proposal_assist/test_models.py
git commit -m "feat(proposal-assist): add call_rubrics template table"
```

---

## Task 6: Rubric extractor module

**Files:**
- Create: `src/call_monitor/proposal_assist/extractors/rubric.py`
- Test: `tests/proposal_assist/test_extractors_rubric.py`

- [ ] **Step 1: Write failing tests**

`tests/proposal_assist/test_extractors_rubric.py`:

```python
import json
from unittest.mock import Mock

import pytest

from call_monitor.proposal_assist.extractors.rubric import (
    extract_rubric,
)


def _envelope(content: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "model": "claude-opus-4-7",
    }


def test_extract_rubric_returns_criteria_and_notes():
    payload = {
        "criteria": [
            {"name": "Excellence", "description": "Scientific quality",
             "weight": 0.5, "scale_max": 5},
            {"name": "Impact", "description": "Translational potential",
             "weight": 0.5, "scale_max": 5},
        ],
        "funder_notes": "Note: late submissions rejected.",
    }
    client = Mock()
    client.chat.return_value = _envelope(json.dumps(payload))
    client.model = "claude-opus-4-7"

    result = extract_rubric(call_text="dummy", client=client)

    assert result["funder_notes"] == "Note: late submissions rejected."
    assert len(result["criteria"]) == 2
    assert result["criteria"][0]["name"] == "Excellence"


def test_extract_rubric_defaults_funder_notes_to_empty_string():
    payload = {
        "criteria": [
            {"name": "X", "description": "x", "weight": 1.0, "scale_max": 5},
        ],
    }
    client = Mock()
    client.chat.return_value = _envelope(json.dumps(payload))
    client.model = "claude-opus-4-7"

    result = extract_rubric(call_text="dummy", client=client)

    assert result["funder_notes"] == ""


def test_extract_rubric_raises_on_malformed_json():
    client = Mock()
    client.chat.return_value = _envelope("not json")
    client.model = "claude-opus-4-7"
    with pytest.raises(ValueError, match="malformed JSON"):
        extract_rubric(call_text="dummy", client=client)


def test_extract_rubric_raises_on_empty_criteria():
    payload = {"criteria": [], "funder_notes": ""}
    client = Mock()
    client.chat.return_value = _envelope(json.dumps(payload))
    client.model = "claude-opus-4-7"
    with pytest.raises(ValueError, match="at least one criterion"):
        extract_rubric(call_text="dummy", client=client)


def test_extract_rubric_raises_on_missing_required_criterion_fields():
    payload = {
        "criteria": [{"name": "Only a name"}],  # missing description/weight/scale_max
        "funder_notes": "",
    }
    client = Mock()
    client.chat.return_value = _envelope(json.dumps(payload))
    client.model = "claude-opus-4-7"
    with pytest.raises(ValueError, match="missing required field"):
        extract_rubric(call_text="dummy", client=client)
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_extractors_rubric.py -v
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `src/call_monitor/proposal_assist/extractors/rubric.py`:

```python
"""Rubric extractor — single Opus call that turns a funder's evaluation
criteria (usually buried in the call body) into a structured JSON rubric.

Per-call, ingestion-time. The Phase 0 worker copies the result into
workspaces.rubric_criteria + workspaces.rubric_funder_notes.
"""
from __future__ import annotations

import json
from typing import Protocol, TypedDict

_REQUIRED_CRITERION_FIELDS = ("name", "description", "weight", "scale_max")

_SYSTEM_PROMPT = """You are an extractor. You receive a funding-call body and
return a JSON object that captures the FUNDER'S OWN evaluation criteria — the
rubric their reviewers will use to score proposals.

Output schema — STRICTLY this shape:

{
  "criteria": [
    {
      "name": "<short name, e.g. Scientific excellence>",
      "description": "<one sentence explaining what this criterion measures>",
      "weight": <float 0..1; if the funder publishes percentages, normalize so
                 weights across criteria sum to ~1.0>,
      "scale_max": <int; the upper end of the rating scale, usually 5>,
      "subcriteria": [   // optional
        {"name": "...", "description": "..."}
      ]
    },
    ...
  ],
  "funder_notes": "<free text capturing eligibility-adjacent notes the rubric
                    consumer should see (FAQ links, late-submission policy,
                    review-panel composition). Empty string if none.>"
}

Return at least one criterion. If the call body doesn't publish explicit
criteria, infer the rubric from what the call asks proposers to demonstrate.
Do not invent weights — if the funder doesn't publish them, distribute evenly.
No prose around the JSON.
"""


class _Client(Protocol):
    model: str

    def chat(
        self,
        *,
        messages: list[dict],
        response_format: dict | None = None,
    ) -> dict: ...


class RubricPayload(TypedDict):
    criteria: list[dict]
    funder_notes: str


def extract_rubric(*, call_text: str, client: _Client) -> RubricPayload:
    """Run the extractor against `call_text`. Returns `{criteria, funder_notes}`.
    Raises ValueError on malformed JSON, empty criteria, or missing required
    criterion fields. Does NOT persist.
    """
    envelope = client.chat(
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": call_text},
        ],
        response_format={"type": "json_object"},
    )
    content = envelope["choices"][0]["message"]["content"]

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"malformed JSON from rubric extractor: {e}") from e

    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSON object, got {type(parsed).__name__}")

    criteria = parsed.get("criteria")
    if not isinstance(criteria, list) or len(criteria) == 0:
        raise ValueError("rubric must have at least one criterion")

    for i, crit in enumerate(criteria):
        if not isinstance(crit, dict):
            raise ValueError(f"criterion {i}: expected object, got {type(crit).__name__}")
        for required in _REQUIRED_CRITERION_FIELDS:
            if required not in crit:
                raise ValueError(
                    f"criterion {i}: missing required field {required!r}"
                )

    notes = parsed.get("funder_notes", "")
    if not isinstance(notes, str):
        raise ValueError(f"funder_notes: expected str, got {type(notes).__name__}")

    return {"criteria": criteria, "funder_notes": notes}
```

- [ ] **Step 4: Run to confirm passing**

```bash
uv run pytest tests/proposal_assist/test_extractors_rubric.py -v
```

Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor/proposal_assist/extractors/rubric.py tests/proposal_assist/test_extractors_rubric.py
git commit -m "feat(proposal-assist): add rubric extractor (rubric-v1)"
```

---

## Task 7: Ingestion-time hook — template seeding

The spec says templates are auto-extracted "at ingestion." For Part 1's executable scope this means: when a Call row lands or its `full_text` changes, queue a background job that ensures the templates are fresh.

For Slice A we keep this dead simple: a synchronous helper that, given a Call row, checks freshness and re-extracts if needed. Hooking it into the actual ingestion pipeline (APScheduler tick or per-source post-fetch callback) is left for Slice E (observability) — for now, exposing it as a callable lets Phase 0 (Tasks 19–20) drive it on first use.

**Files:**
- Create: `src/call_monitor/proposal_assist/extractors/refresh.py`
- Test: `tests/proposal_assist/test_extractors_refresh.py`

- [ ] **Step 1: Write failing tests**

`tests/proposal_assist/test_extractors_refresh.py`:

```python
import json
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from call_monitor.db.models import CallRubric, CallSectionExtract
from call_monitor.proposal_assist.extractors.refresh import (
    ensure_rubric_fresh,
    ensure_section_extracts_fresh,
)
from call_monitor.proposal_assist.extractors.sections import SECTION_NAMES
from tests.proposal_assist.conftest import make_call


def _section_envelope() -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": json.dumps(
            {s: f"extract for {s}" for s in SECTION_NAMES}
        )}}],
        "model": "claude-opus-4-7",
    }


def _rubric_envelope() -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": json.dumps({
            "criteria": [{"name": "X", "description": "x",
                          "weight": 1.0, "scale_max": 5}],
            "funder_notes": "",
        })}}],
        "model": "claude-opus-4-7",
    }


def test_ensure_section_extracts_fresh_creates_six_rows_first_time(
    pg_session_factory,
):
    client = Mock()
    client.chat.return_value = _section_envelope()
    client.model = "claude-opus-4-7"

    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ensure_section_extracts_fresh(
            session=s, call=call, client=client,
            extractor_version="sections-v1",
        )
        s.commit()
        rows = s.scalars(
            select(CallSectionExtract).where(
                CallSectionExtract.call_id == call.id
            )
        ).all()
        assert len(rows) == 6
        assert {r.section for r in rows} == set(SECTION_NAMES)
        assert client.chat.call_count == 1


def test_ensure_section_extracts_fresh_skips_when_hash_and_version_match(
    pg_session_factory,
):
    client = Mock()
    client.chat.return_value = _section_envelope()
    client.model = "claude-opus-4-7"

    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ensure_section_extracts_fresh(
            session=s, call=call, client=client,
            extractor_version="sections-v1",
        )
        s.commit()
        assert client.chat.call_count == 1

        ensure_section_extracts_fresh(
            session=s, call=call, client=client,
            extractor_version="sections-v1",
        )
        s.commit()
        assert client.chat.call_count == 1  # NOT re-called


def test_ensure_section_extracts_fresh_re_extracts_on_version_bump(
    pg_session_factory,
):
    client = Mock()
    client.chat.return_value = _section_envelope()
    client.model = "claude-opus-4-7"

    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ensure_section_extracts_fresh(
            session=s, call=call, client=client,
            extractor_version="sections-v1",
        )
        s.commit()
        ensure_section_extracts_fresh(
            session=s, call=call, client=client,
            extractor_version="sections-v2",  # bumped
        )
        s.commit()
        assert client.chat.call_count == 2
        rows = s.scalars(
            select(CallSectionExtract).where(
                CallSectionExtract.call_id == call.id
            )
        ).all()
        assert all(r.extractor_version == "sections-v2" for r in rows)


def test_ensure_section_extracts_fresh_re_extracts_on_source_text_change(
    pg_session_factory,
):
    client = Mock()
    client.chat.return_value = _section_envelope()
    client.model = "claude-opus-4-7"

    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ensure_section_extracts_fresh(
            session=s, call=call, client=client,
            extractor_version="sections-v1",
        )
        s.commit()

        call.full_text = (call.full_text or "") + " (RE-INGESTED)"
        s.commit()

        ensure_section_extracts_fresh(
            session=s, call=call, client=client,
            extractor_version="sections-v1",
        )
        s.commit()
        assert client.chat.call_count == 2


def test_ensure_section_extracts_fresh_preserves_existing_on_extractor_failure(
    pg_session_factory,
):
    """Regression: a transient LLM failure must NOT wipe the previously-cached
    rows. Extract-first ordering means the LLM call is attempted before any
    DELETE; on failure, existing rows are untouched."""
    good_client = Mock()
    good_client.chat.return_value = _section_envelope()
    good_client.model = "claude-opus-4-7"

    failing_client = Mock()
    failing_client.chat.side_effect = RuntimeError("LLM 503")
    failing_client.model = "claude-opus-4-7"

    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()

        # Seed with good extracts.
        ensure_section_extracts_fresh(
            session=s, call=call, client=good_client,
            extractor_version="sections-v1",
        )
        s.commit()
        before = sorted(
            (r.section, r.extract_text)
            for r in s.scalars(
                select(CallSectionExtract).where(
                    CallSectionExtract.call_id == call.id
                )
            ).all()
        )

        # Bump version to force re-extract; the client raises.
        with pytest.raises(RuntimeError):
            ensure_section_extracts_fresh(
                session=s, call=call, client=failing_client,
                extractor_version="sections-v2",
            )

        # Existing rows survive.
        after = sorted(
            (r.section, r.extract_text)
            for r in s.scalars(
                select(CallSectionExtract).where(
                    CallSectionExtract.call_id == call.id
                )
            ).all()
        )
        assert before == after


def test_ensure_rubric_fresh_creates_row_first_time(pg_session_factory):
    client = Mock()
    client.chat.return_value = _rubric_envelope()
    client.model = "claude-opus-4-7"

    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ensure_rubric_fresh(
            session=s, call=call, client=client,
            extractor_version="rubric-v1",
        )
        s.commit()
        row = s.get(CallRubric, call.id)
        assert row is not None
        assert row.extractor_version == "rubric-v1"
        assert client.chat.call_count == 1


def test_ensure_rubric_fresh_skips_when_fresh(pg_session_factory):
    client = Mock()
    client.chat.return_value = _rubric_envelope()
    client.model = "claude-opus-4-7"

    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ensure_rubric_fresh(
            session=s, call=call, client=client,
            extractor_version="rubric-v1",
        )
        s.commit()
        ensure_rubric_fresh(
            session=s, call=call, client=client,
            extractor_version="rubric-v1",
        )
        s.commit()
        assert client.chat.call_count == 1


def test_ensure_rubric_fresh_preserves_existing_on_extractor_failure(
    pg_session_factory,
):
    good_client = Mock()
    good_client.chat.return_value = _rubric_envelope()
    good_client.model = "claude-opus-4-7"

    failing_client = Mock()
    failing_client.chat.side_effect = RuntimeError("LLM 503")
    failing_client.model = "claude-opus-4-7"

    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()

        ensure_rubric_fresh(
            session=s, call=call, client=good_client,
            extractor_version="rubric-v1",
        )
        s.commit()
        before_criteria = s.get(CallRubric, call.id).criteria

        with pytest.raises(RuntimeError):
            ensure_rubric_fresh(
                session=s, call=call, client=failing_client,
                extractor_version="rubric-v2",
            )
        # Row content unchanged.
        assert s.get(CallRubric, call.id).criteria == before_criteria
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_extractors_refresh.py -v
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `src/call_monitor/proposal_assist/extractors/refresh.py`:

```python
"""Freshness helpers — idempotent template seeding driven by source_text_hash
+ extractor_version. Phase 0 calls these to guarantee both templates are
up-to-date before snapshotting them into the workspace.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from call_monitor.db.models import Call, CallRubric, CallSectionExtract
from call_monitor.proposal_assist.extractors.rubric import extract_rubric
from call_monitor.proposal_assist.extractors.sections import (
    SECTION_NAMES,
    extract_sections,
)
from call_monitor.proposal_assist.hashing import source_text_hash


def ensure_section_extracts_fresh(
    *,
    session: Session,
    call: Call,
    client,
    extractor_version: str,
) -> None:
    """Idempotent: if all six rows exist with matching `source_text_hash` AND
    matching `extractor_version`, no LLM call is made. Otherwise the extractor
    is run **first** (so a transient LLM failure cannot wipe cached rows),
    then prior rows are deleted and six fresh rows inserted in the same unit
    of work.

    Caller commits. If the extractor raises, the caller's session should
    rollback — no destructive state has been written yet.
    """
    current_hash = source_text_hash(call.full_text or "")

    existing = session.scalars(
        select(CallSectionExtract).where(
            CallSectionExtract.call_id == call.id
        )
    ).all()

    if (
        len(existing) == len(SECTION_NAMES)
        and all(r.source_text_hash == current_hash for r in existing)
        and all(r.extractor_version == extractor_version for r in existing)
    ):
        return

    # EXTRACT FIRST — if this raises, we haven't touched the DB yet.
    sections = extract_sections(call_text=call.full_text or "", client=client)

    # Now safe to mutate. Delete + insert happen in the same transaction;
    # neither lands without the other because the caller commits once.
    if existing:
        session.execute(
            delete(CallSectionExtract).where(
                CallSectionExtract.call_id == call.id
            )
        )
        # Flush so the DELETE lands before our INSERTs hit the PK constraint.
        session.flush()

    now = datetime.now(timezone.utc)
    for section_name in SECTION_NAMES:
        session.add(CallSectionExtract(
            call_id=call.id,
            section=section_name,
            extract_text=sections[section_name],
            extracted_at=now,
            extracted_by_model=client.model,
            extractor_version=extractor_version,
            source_text_hash=current_hash,
        ))


def ensure_rubric_fresh(
    *,
    session: Session,
    call: Call,
    client,
    extractor_version: str,
) -> None:
    """Idempotent: if the row exists with matching hash + version, no LLM
    call is made. Otherwise the extractor is run **first** (so a transient
    LLM failure cannot mutate the cached row), then the row is upserted.

    Caller commits. If the extractor raises, the caller's session should
    rollback — no destructive state has been written yet.
    """
    current_hash = source_text_hash(call.full_text or "")
    existing = session.get(CallRubric, call.id)
    if (
        existing is not None
        and existing.source_text_hash == current_hash
        and existing.extractor_version == extractor_version
    ):
        return

    # EXTRACT FIRST — if this raises, the existing row is untouched.
    payload = extract_rubric(call_text=call.full_text or "", client=client)
    now = datetime.now(timezone.utc)

    if existing is None:
        session.add(CallRubric(
            call_id=call.id,
            criteria=payload["criteria"],
            funder_notes=payload["funder_notes"],
            extracted_at=now,
            extracted_by_model=client.model,
            extractor_version=extractor_version,
            source_text_hash=current_hash,
        ))
    else:
        existing.criteria = payload["criteria"]
        existing.funder_notes = payload["funder_notes"]
        existing.extracted_at = now
        existing.extracted_by_model = client.model
        existing.extractor_version = extractor_version
        existing.source_text_hash = current_hash
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/proposal_assist/test_extractors_refresh.py -v
```

Expected: 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor/proposal_assist/extractors/refresh.py tests/proposal_assist/test_extractors_refresh.py
git commit -m "feat(proposal-assist): template freshness helpers driven by hash + version"
```

---

## Task 8: Part 1 milestone checkpoint

- [ ] **Step 1: Run the full proposal_assist suite**

```bash
uv run pytest tests/proposal_assist/ -v
```

Expected: all Tasks 1–7 tests pass (~25 tests).

- [ ] **Step 2: Lint + format**

```bash
uv run ruff check src/call_monitor/proposal_assist tests/proposal_assist
uv run ruff format src/call_monitor/proposal_assist tests/proposal_assist
```

Fix any issues; commit with `chore: ruff fixes for Part 1` if `format` changed anything.

- [ ] **Step 3: Stop here and check in with the user**

Part 1 delivers:
- Two new tables (`call_section_extracts`, `call_rubrics`) with freshness metadata
- Two extractor modules + idempotent freshness helpers with thorough unit-test coverage
- Safe-under-failure ordering: extractor runs **before** any destructive DB write, so a transient LLM error cannot wipe a previously-cached row

**What Part 1 does NOT deliver toward the spec's implementation-gate item 3:**
- The freshness helpers are *callable* but not yet *invoked* at ingestion. There's no APScheduler tick wiring or per-source post-fetch callback that runs them against every newly-ingested call. Slice E (observability + ops hooks) owns that integration. Until then, the extractor modules behave as pull-on-demand utilities — Phase 0 (Part 2) will be the first real consumer.
- Validation against the existing call corpus (the spec's "validated against the existing call corpus" language) is data work, not engineering — it lives outside this plan. Treat Part 1 as "production-shaped extractor modules, ready to be hooked into ingestion" rather than "in production."

Surface to the user before proceeding to Part 2 — Part 2 depends on the base spec (`workspaces`, `workspace_viewers`, `workspace_events`) being in place.

---

# Part 2 — Blocked on base spec

**Do not start these tasks until** `workspaces` (with `id uuid PK`, `owner_id bigint FK users`, `call_id`, `profile_id`, `created_at`), `workspace_viewers`, and `workspace_events` exist in the schema. If running under `src/call_monitor/` (no rename yet), use the imports in this plan as-is; if the base spec has done the `call-monitor → call-manager` rename, the paths shift to `src/call_manager/proposal_assist/...` and the Python imports change accordingly — adjust at execution time.

**Migration revision IDs in Part 2 are placeholders.** The plan shows `0014`–`0017` to keep the chain readable, but base spec will land its own migrations (workspaces / workspace_viewers / workspace_events) between `0013` and the first Part 2 migration. Before writing each Part 2 migration:

1. Run `ls alembic/versions/` and find the current head.
2. Rename the migration file to `<head + 1>_<topic>.py` (zero-padded 4-digit).
3. Set `revision = "<head + 1>"` and `down_revision = "<current head>"`.
4. Repeat with the new head for the next migration in the chain.

The body of each migration (the `upgrade()` / `downgrade()` SQL) does not change — only the IDs.

## Task 9: `workspaces` column additions

**Files:**
- Create: `alembic/versions/0014_workspaces_phase_columns.py`
- Modify: `src/call_monitor/db/models.py` (the existing `Workspace` model, added by base spec)
- Test: extend `tests/proposal_assist/test_models.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/proposal_assist/test_models.py`:

```python
from datetime import datetime, timezone


def test_workspace_has_phase_columns_with_defaults(pg_session_factory):
    from datetime import datetime

    from call_monitor.db.models import Workspace
    from tests.proposal_assist.conftest import make_owner_and_workspace

    with pg_session_factory() as s:
        _, wsid = make_owner_and_workspace(s)
        ws = s.get(Workspace, wsid)
        assert ws.current_phase == "setup"
        assert ws.call_body_snapshot == ""
        assert ws.profile_desc_snapshot == ""
        assert isinstance(ws.snapshot_refreshed_at, datetime)
        assert ws.rubric_criteria == []
        assert ws.rubric_funder_notes is None
        assert isinstance(ws.rubric_updated_at, datetime)
        assert ws.section_extracts == {}
        assert ws.review_runs_per_day_override is None
        assert ws.review_dollar_cap_override is None


def test_workspace_current_phase_rejects_bad_value(pg_session_factory):
    from call_monitor.db.models import Workspace
    from tests.proposal_assist.conftest import make_owner_and_workspace

    with pg_session_factory() as s:
        _, wsid = make_owner_and_workspace(s)
        ws = s.get(Workspace, wsid)
        ws.current_phase = "not_a_phase"
        with pytest.raises(Exception):
            s.commit()
```

Add `make_owner_and_workspace` to `tests/proposal_assist/conftest.py` per the canonical pattern in the conventions section.

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/proposal_assist/test_models.py::test_workspace_has_phase_columns_with_defaults -v
```

Expected: `AttributeError` on `current_phase` or similar.

- [ ] **Step 3: Write the migration**

Create `alembic/versions/0014_workspaces_phase_columns.py`:

```python
"""workspaces: phase + snapshot + rubric + section_extracts + overrides

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "current_phase", sa.Text(),
            nullable=False, server_default="setup",
        ),
    )
    op.create_check_constraint(
        "ck_workspaces_current_phase",
        "workspaces",
        "current_phase IN ('setup','spec','plan','draft','review')",
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "call_body_snapshot", sa.Text(),
            nullable=False, server_default="",
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "profile_desc_snapshot", sa.Text(),
            nullable=False, server_default="",
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "snapshot_refreshed_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "rubric_criteria", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column("rubric_funder_notes", sa.Text(), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "rubric_updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "section_extracts", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column("review_runs_per_day_override", sa.Integer(), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("review_dollar_cap_override", sa.Numeric(6, 3), nullable=True),
    )


def downgrade() -> None:
    for col in (
        "review_dollar_cap_override",
        "review_runs_per_day_override",
        "section_extracts",
        "rubric_updated_at",
        "rubric_funder_notes",
        "rubric_criteria",
        "snapshot_refreshed_at",
        "profile_desc_snapshot",
        "call_body_snapshot",
    ):
        op.drop_column("workspaces", col)
    op.drop_constraint("ck_workspaces_current_phase", "workspaces", type_="check")
    op.drop_column("workspaces", "current_phase")
```

- [ ] **Step 4: Add fields to the `Workspace` model**

In `src/call_monitor/db/models.py`, edit the existing `Workspace` class (added by base spec) to include:

```python
    current_phase: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="setup",
    )
    call_body_snapshot: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="",
    )
    profile_desc_snapshot: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="",
    )
    snapshot_refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    rubric_criteria: Mapped[list[dict]] = mapped_column(
        postgresql.JSONB, nullable=False, server_default=text("'[]'::jsonb"),
    )
    rubric_funder_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    rubric_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    section_extracts: Mapped[dict] = mapped_column(
        postgresql.JSONB, nullable=False, server_default=text("'{}'::jsonb"),
    )
    review_runs_per_day_override: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    review_dollar_cap_override: Mapped[float | None] = mapped_column(
        Numeric(6, 3), nullable=True,
    )
```

Confirm `text` and `Numeric` are imported at the top of `models.py`; add if missing.

- [ ] **Step 5: Migrate + run tests**

```bash
uv run alembic upgrade head
uv run pytest tests/proposal_assist/test_models.py -v
```

Expected: model tests pass.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/0014_workspaces_phase_columns.py src/call_monitor/db/models.py tests/proposal_assist/test_models.py
git commit -m "feat(proposal-assist): add phase + snapshot + rubric + section_extracts columns to workspaces"
```

---

## Task 10: `phase_approvals` migration + model

**Files:**
- Create: `alembic/versions/0015_phase_approvals.py`
- Modify: `src/call_monitor/db/models.py`
- Test: extend `tests/proposal_assist/test_models.py`

- [ ] **Step 1: Append failing test**

```python
from call_monitor.db.models import PhaseApproval


def test_phase_approval_can_be_inserted(pg_session_factory):
    from tests.proposal_assist.conftest import make_owner_and_workspace

    with pg_session_factory() as s:
        uid, wsid = make_owner_and_workspace(s)
        pa = PhaseApproval(
            workspace_id=wsid, phase="spec",
            approving_user_id=uid, approved_artifact_sha="a" * 40,
        )
        s.add(pa)
        s.commit()
        assert pa.id is not None
        assert isinstance(pa.approved_at, datetime)


def test_phase_approval_phase_check(pg_session_factory):
    from tests.proposal_assist.conftest import make_owner_and_workspace

    with pg_session_factory() as s:
        uid, wsid = make_owner_and_workspace(s)
        s.add(PhaseApproval(
            workspace_id=wsid, phase="not_a_phase",
            approving_user_id=uid, approved_artifact_sha="a" * 40,
        ))
        with pytest.raises(Exception):
            s.commit()
```

- [ ] **Step 2: Run to confirm failure** — `ImportError`.

- [ ] **Step 3: Write the migration**

Create `alembic/versions/0015_phase_approvals.py`:

```python
"""phase_approvals

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "phase_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column(
            "approved_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("approving_user_id", sa.BigInteger(), nullable=False),
        sa.Column("approved_artifact_sha", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["approving_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "phase IN ('spec','plan','draft','review')",
            name="ck_phase_approvals_phase",
        ),
    )
    op.create_index(
        "ix_phase_approvals_ws_phase_at",
        "phase_approvals",
        ["workspace_id", "phase", sa.text("approved_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_phase_approvals_ws_phase_at", table_name="phase_approvals")
    op.drop_table("phase_approvals")
```

- [ ] **Step 4: Add the model**

In `src/call_monitor/db/models.py`:

```python
import uuid
from sqlalchemy.dialects.postgresql import UUID


class PhaseApproval(Base):
    __tablename__ = "phase_approvals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    approving_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False,
    )
    approved_artifact_sha: Mapped[str] = mapped_column(Text, nullable=False)
```

- [ ] **Step 5: Migrate + test**

```bash
uv run alembic upgrade head
uv run pytest tests/proposal_assist/test_models.py -v
```

Expected: model tests pass.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/0015_phase_approvals.py src/call_monitor/db/models.py tests/proposal_assist/test_models.py
git commit -m "feat(proposal-assist): add phase_approvals table"
```

---

## Task 11: `workspace_setup_jobs` migration + model

**Files:**
- Create: `alembic/versions/0016_workspace_setup_jobs.py`
- Modify: `src/call_monitor/db/models.py`
- Test: extend `tests/proposal_assist/test_models.py`

- [ ] **Step 1: Append failing test**

```python
from call_monitor.db.models import WorkspaceSetupJob


def test_workspace_setup_job_unique_per_workspace(pg_session_factory):
    from tests.proposal_assist.conftest import make_owner_and_workspace

    with pg_session_factory() as s:
        _, wsid = make_owner_and_workspace(s)
        s.add(WorkspaceSetupJob(workspace_id=wsid, status="queued"))
        s.commit()
        s.add(WorkspaceSetupJob(workspace_id=wsid, status="queued"))
        with pytest.raises(Exception):
            s.commit()
        s.rollback()


def test_workspace_setup_job_status_check(pg_session_factory):
    from tests.proposal_assist.conftest import make_owner_and_workspace

    with pg_session_factory() as s:
        _, wsid = make_owner_and_workspace(s)
        s.add(WorkspaceSetupJob(workspace_id=wsid, status="bogus"))
        with pytest.raises(Exception):
            s.commit()
```

- [ ] **Step 2: Run to confirm failure** — `ImportError`.

- [ ] **Step 3: Migration**

Create `alembic/versions/0016_workspace_setup_jobs.py`:

```python
"""workspace_setup_jobs

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_setup_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "attempts", sa.SmallInteger(),
            nullable=False, server_default=sa.text("0"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", name="uq_workspace_setup_jobs_ws"),
        sa.CheckConstraint(
            "status IN ('queued','running','waiting_for_rubric_review',"
            "'done','failed')",
            name="ck_workspace_setup_jobs_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("workspace_setup_jobs")
```

- [ ] **Step 4: Model**

```python
class WorkspaceSetupJob(Base):
    __tablename__ = "workspace_setup_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"),
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
```

Add `SmallInteger` to the imports in `models.py` if not present.

- [ ] **Step 5: Migrate + test**

```bash
uv run alembic upgrade head
uv run pytest tests/proposal_assist/test_models.py -v
```

Expected: model tests pass.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/0016_workspace_setup_jobs.py src/call_monitor/db/models.py tests/proposal_assist/test_models.py
git commit -m "feat(proposal-assist): add workspace_setup_jobs table"
```

---

## Task 12: `review_runs` + `review_run_items` migration + models

**Files:**
- Create: `alembic/versions/0017_review_runs_and_items.py`
- Modify: `src/call_monitor/db/models.py`
- Test: extend `tests/proposal_assist/test_models.py`

- [ ] **Step 1: Append failing tests**

```python
from call_monitor.db.models import ReviewRun, ReviewRunItem


def test_review_run_active_lock_blocks_double_queued(pg_session_factory):
    from tests.proposal_assist.conftest import make_owner_and_workspace

    with pg_session_factory() as s:
        _, wsid = make_owner_and_workspace(s)
        s.add(ReviewRun(workspace_id=wsid, status="queued",
                        draft_sha="d" * 40))
        s.commit()
        s.add(ReviewRun(workspace_id=wsid, status="queued",
                        draft_sha="d" * 40))
        with pytest.raises(Exception):
            s.commit()
        s.rollback()


def test_review_run_active_lock_allows_after_terminal(pg_session_factory):
    from tests.proposal_assist.conftest import make_owner_and_workspace

    with pg_session_factory() as s:
        _, wsid = make_owner_and_workspace(s)
        s.add(ReviewRun(workspace_id=wsid, status="done",
                        draft_sha="d" * 40))
        s.commit()
        s.add(ReviewRun(workspace_id=wsid, status="queued",
                        draft_sha="d" * 40))
        s.commit()  # should NOT raise


def test_review_run_item_kind_unique_per_run(pg_session_factory):
    from tests.proposal_assist.conftest import make_owner_and_workspace

    with pg_session_factory() as s:
        _, wsid = make_owner_and_workspace(s)
        rr = ReviewRun(workspace_id=wsid, status="running",
                       draft_sha="d" * 40)
        s.add(rr)
        s.commit()
        s.add(ReviewRunItem(review_run_id=rr.id, kind="fit_check",
                            status="queued"))
        s.commit()
        s.add(ReviewRunItem(review_run_id=rr.id, kind="fit_check",
                            status="queued"))
        with pytest.raises(Exception):
            s.commit()
```

- [ ] **Step 2: Run to confirm failure** — `ImportError`.

- [ ] **Step 3: Migration**

Create `alembic/versions/0017_review_runs_and_items.py`:

```python
"""review_runs + review_run_items

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "triggered_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("draft_sha", sa.Text(), nullable=False),
        sa.Column("fit_check_sha", sa.Text(), nullable=True),
        sa.Column("rubric_review_sha", sa.Text(), nullable=True),
        sa.Column(
            "waived_from_review_run_id", postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_input_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_dollars", sa.Numeric(6, 3), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["waived_from_review_run_id"], ["review_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed','cancelled',"
            "'partial')",
            name="ck_review_runs_status",
        ),
    )
    op.create_index(
        "ix_review_runs_ws_triggered",
        "review_runs",
        ["workspace_id", sa.text("triggered_at DESC")],
    )
    # Active-review lock: at most one queued/running run per workspace.
    op.create_index(
        "uq_review_runs_active",
        "review_runs",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )

    op.create_table(
        "review_run_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_input_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_dollars", sa.Numeric(6, 4), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "raw_response_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["review_run_id"], ["review_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "review_run_id", "kind", name="uq_review_run_items_run_kind"
        ),
        sa.CheckConstraint(
            "kind IN ('fit_check',"
            "'persona_domain_skeptic',"
            "'persona_methodology_critic',"
            "'persona_translation_skeptic',"
            "'persona_devils_advocate',"
            "'consolidation')",
            name="ck_review_run_items_kind",
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed','skipped',"
            "'cancelled')",
            name="ck_review_run_items_status",
        ),
    )
    op.create_index(
        "ix_review_run_items_run",
        "review_run_items",
        ["review_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_review_run_items_run", table_name="review_run_items")
    op.drop_table("review_run_items")
    op.drop_index("uq_review_runs_active", table_name="review_runs")
    op.drop_index("ix_review_runs_ws_triggered", table_name="review_runs")
    op.drop_table("review_runs")
```

- [ ] **Step 4: Models**

```python
class ReviewRun(Base):
    __tablename__ = "review_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    draft_sha: Mapped[str] = mapped_column(Text, nullable=False)
    fit_check_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    rubric_review_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    waived_from_review_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("review_runs.id"),
        nullable=True,
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_input_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    estimated_dollars: Mapped[float | None] = mapped_column(
        Numeric(6, 3), nullable=True,
    )


class ReviewRunItem(Base):
    __tablename__ = "review_run_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    review_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("review_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_input_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    estimated_dollars: Mapped[float | None] = mapped_column(
        Numeric(6, 4), nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response_json: Mapped[dict | None] = mapped_column(
        postgresql.JSONB, nullable=True,
    )
```

- [ ] **Step 5: Migrate + test**

```bash
uv run alembic upgrade head
uv run pytest tests/proposal_assist/test_models.py -v
```

Expected: model tests pass.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/0017_review_runs_and_items.py src/call_monitor/db/models.py tests/proposal_assist/test_models.py
git commit -m "feat(proposal-assist): add review_runs + review_run_items with active-review lock"
```

---

## Task 13: Phase 0 worker scaffold — job claim + state transitions

The Phase 0 worker polls `workspace_setup_jobs` rows, claims them under `SELECT ... FOR UPDATE SKIP LOCKED`, and runs the 7-step sequence from the spec. This task lands the scaffold: claim logic, state transitions (queued → running → ... → done/failed), retries on transient failure (up to 5 attempts), and the `waiting_for_rubric_review` yield (where the worker releases the row and exits its loop, letting a separate trigger advance it when the owner confirms).

**Files:**
- Create: `src/call_monitor/proposal_assist/phases/__init__.py` (empty)
- Create: `src/call_monitor/proposal_assist/phases/setup.py`
- Test: `tests/proposal_assist/test_phase0_setup.py`

- [ ] **Step 1: Write failing tests for the scaffold**

`tests/proposal_assist/test_phase0_setup.py`:

```python
from unittest.mock import Mock

import pytest

from call_monitor.db.models import WorkspaceSetupJob
from call_monitor.proposal_assist.phases.setup import (
    Phase0Worker,
    claim_next_setup_job,
)


from tests.proposal_assist.conftest import make_owner_and_workspace


def test_claim_next_returns_oldest_queued(pg_session_factory):
    with pg_session_factory() as s:
        _, wsid = make_owner_and_workspace(s)
        job = WorkspaceSetupJob(workspace_id=wsid, status="queued")
        s.add(job)
        s.commit()
        claimed = claim_next_setup_job(session=s)
        s.commit()
        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.status == "running"
        assert claimed.started_at is not None


def test_claim_next_returns_none_when_nothing_queued(pg_session_factory):
    with pg_session_factory() as s:
        assert claim_next_setup_job(session=s) is None


def test_phase0_worker_transitions_to_done_on_success(
    pg_session_factory, monkeypatch, tmp_path,
):
    with pg_session_factory() as s:
        _, wsid = make_owner_and_workspace(s)
        s.add(WorkspaceSetupJob(workspace_id=wsid, status="queued"))
        s.commit()

        worker = Phase0Worker(
            session=s,
            client=Mock(model="claude-opus-4-7"),
            extractor_version="sections-v1",
            rubric_extractor_version="rubric-v1",
            workspace_repo_root=str(tmp_path / "repos"),
        )
        # Stub the long sub-steps so this test isolates the lifecycle wrapper.
        monkeypatch.setattr(worker, "_run_steps", lambda job: None)

        worker.run_one()
        s.expire_all()
        job = (
            s.query(WorkspaceSetupJob)
            .filter(WorkspaceSetupJob.workspace_id == wsid).one()
        )
        assert job.status == "done"
        assert job.completed_at is not None


def test_phase0_worker_increments_attempts_and_marks_failed_after_5(
    pg_session_factory, monkeypatch, tmp_path,
):
    with pg_session_factory() as s:
        _, wsid = make_owner_and_workspace(s)
        s.add(WorkspaceSetupJob(
            workspace_id=wsid, status="queued", attempts=4,
        ))
        s.commit()

        worker = Phase0Worker(
            session=s, client=Mock(model="m"),
            extractor_version="sections-v1",
            rubric_extractor_version="rubric-v1",
            workspace_repo_root=str(tmp_path / "repos"),
        )

        def boom(_job):
            raise RuntimeError("transient")
        monkeypatch.setattr(worker, "_run_steps", boom)

        worker.run_one()
        s.expire_all()
        job = (
            s.query(WorkspaceSetupJob)
            .filter(WorkspaceSetupJob.workspace_id == wsid).one()
        )
        assert job.status == "failed"
        assert job.attempts == 5
        assert "transient" in (job.last_error or "")


def test_phase0_worker_requeues_on_transient_failure_below_attempt_cap(
    pg_session_factory, monkeypatch, tmp_path,
):
    with pg_session_factory() as s:
        _, wsid = make_owner_and_workspace(s)
        s.add(WorkspaceSetupJob(
            workspace_id=wsid, status="queued", attempts=0,
        ))
        s.commit()

        worker = Phase0Worker(
            session=s, client=Mock(model="m"),
            extractor_version="sections-v1",
            rubric_extractor_version="rubric-v1",
            workspace_repo_root=str(tmp_path / "repos"),
        )

        def boom(_job):
            raise RuntimeError("transient")
        monkeypatch.setattr(worker, "_run_steps", boom)

        worker.run_one()
        s.expire_all()
        job = (
            s.query(WorkspaceSetupJob)
            .filter(WorkspaceSetupJob.workspace_id == wsid).one()
        )
        assert job.status == "queued"  # back to queued for next tick
        assert job.attempts == 1
        assert "transient" in (job.last_error or "")
```

- [ ] **Step 2: Run to confirm failure** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement scaffold**

Create `src/call_monitor/proposal_assist/phases/__init__.py` (empty).

Create `src/call_monitor/proposal_assist/phases/setup.py`:

```python
"""Phase 0 worker — runs the workspace_setup_jobs queue.

Lifecycle:
    queued
      └─ claim_next_setup_job (FOR UPDATE SKIP LOCKED) → running, started_at
            ├─ run_steps OK + rubric review pending → waiting_for_rubric_review
            ├─ run_steps OK + rubric reviewed       → done, completed_at
            ├─ run_steps raises + attempts < 5      → queued, attempts++
            └─ run_steps raises + attempts == 5     → failed, completed_at
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_monitor.db.models import WorkspaceSetupJob

logger = logging.getLogger(__name__)

MAX_SETUP_ATTEMPTS = 5


class _AnthropicClientLike(Protocol):
    model: str
    def chat(self, *, messages: list[dict], response_format: dict | None = None) -> dict: ...


def claim_next_setup_job(*, session: Session) -> WorkspaceSetupJob | None:
    """Claim the oldest queued job under SELECT FOR UPDATE SKIP LOCKED.

    Caller commits.
    """
    stmt = (
        select(WorkspaceSetupJob)
        .where(WorkspaceSetupJob.status == "queued")
        .order_by(WorkspaceSetupJob.id)  # FIFO-ish; UUIDs sort lexically
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = session.scalar(stmt)
    if job is None:
        return None
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    return job


@dataclass
class Phase0Worker:
    """Single-threaded worker; one instance per process.

    `_run_steps` is the long-running sub-step sequence — split out so tests
    can monkeypatch it. Tasks 14–17 fill in its implementation.
    """
    session: Session
    client: _AnthropicClientLike
    extractor_version: str
    rubric_extractor_version: str
    workspace_repo_root: str

    def run_one(self) -> bool:
        """Claim one job, execute it, advance its status. Returns True if a
        job was processed (regardless of success/failure), False if the queue
        was empty."""
        job = claim_next_setup_job(session=self.session)
        if job is None:
            return False
        self.session.commit()  # release the FOR UPDATE row lock early
        job_id = job.id  # capture before potential rollback detaches `job`

        try:
            self._run_steps(job)
        except Exception as exc:
            # CRITICAL: rollback any partial work (e.g. half-written extracts,
            # filesystem writes excluded — git ops outside the txn are a
            # known acceptable looseness, see Task 14). Without rollback we
            # would commit destructive intermediate state alongside the new
            # job-status row.
            self.session.rollback()

            # Re-fetch the job in a fresh transaction so we can write its
            # failure state without depending on a now-detached instance.
            job = self.session.get(WorkspaceSetupJob, job_id)
            if job is None:
                logger.error(
                    "phase0 setup job %s vanished after rollback", job_id,
                )
                return True
            job.attempts += 1
            job.last_error = f"{type(exc).__name__}: {exc}"
            if job.attempts >= MAX_SETUP_ATTEMPTS:
                job.status = "failed"
                job.completed_at = datetime.now(timezone.utc)
                logger.exception(
                    "phase0 setup job %s failed permanently after %d attempts",
                    job.id, job.attempts,
                )
            else:
                job.status = "queued"
                job.started_at = None  # ready to be re-claimed on next tick
                logger.warning(
                    "phase0 setup job %s transient failure (attempt %d/%d): %s",
                    job.id, job.attempts, MAX_SETUP_ATTEMPTS, exc,
                )
            self.session.commit()
            return True

        # Success path. _run_steps decides between waiting_for_rubric_review
        # and done by mutating job.status directly. If it left status as
        # 'running', treat that as 'done'.
        if job.status == "running":
            job.status = "done"
            job.completed_at = datetime.now(timezone.utc)
        elif job.status == "waiting_for_rubric_review":
            # Yield: do NOT set completed_at; the rubric review endpoint
            # will advance to 'done' when the owner confirms.
            pass
        self.session.commit()
        return True

    def _run_steps(self, job: WorkspaceSetupJob) -> None:
        """Filled in by Tasks 14–17. Raises on transient/permanent failure;
        mutates job.status to 'waiting_for_rubric_review' to yield."""
        raise NotImplementedError("filled in by Tasks 14–17")
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/proposal_assist/test_phase0_setup.py -v
```

Expected: 5 scaffold tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor/proposal_assist/phases/__init__.py src/call_monitor/proposal_assist/phases/setup.py tests/proposal_assist/test_phase0_setup.py
git commit -m "feat(proposal-assist): Phase 0 worker scaffold (claim + lifecycle)"
```

---

## Task 14: Phase 0 — git init + snapshot call body + snapshot profile description (steps 1–3)

**Files:**
- Modify: `src/call_monitor/proposal_assist/phases/setup.py`
- Test: extend `tests/proposal_assist/test_phase0_setup.py`

- [ ] **Step 1: Append failing test**

First, add a profile-builder helper to `tests/proposal_assist/conftest.py`:

```python
def make_profile(session, *, name: str = "Test PI Profile") -> "Profile":
    """Build a Profile owned by a fresh User. Real Profile requires
    `kind: ProfileKind` and `created_by` FK — no `slug`, no top-level
    is_active enforcement at constructor time (defaults to True)."""
    from call_monitor.db.models import Profile, ProfileKind, User
    u = User(email=f"profile-owner-{name}@ki.se")
    session.add(u)
    session.flush()
    p = Profile(
        name=name,
        kind=ProfileKind.GROUP,
        description="AI-assisted diagnosis of cancer biopsies, histopathology.",
        created_by=u.id,
    )
    session.add(p)
    session.flush()
    return p
```

Now the failing test:

```python
import subprocess

from sqlalchemy import select

from call_monitor.db.models import Workspace
from tests.proposal_assist.conftest import (
    make_call, make_owner_and_workspace, make_profile,
)


def test_phase0_steps_1_3_init_repo_and_snapshot(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        call = make_call(s)
        profile = make_profile(s)
        s.commit()
        _, wsid = make_owner_and_workspace(
            s, call_id=call.id, profile_id=profile.id,
        )
        s.add(WorkspaceSetupJob(workspace_id=wsid, status="running"))
        s.commit()

        worker = Phase0Worker(
            session=s,
            client=Mock(model="claude-opus-4-7"),
            extractor_version="sections-v1",
            rubric_extractor_version="rubric-v1",
            workspace_repo_root=str(tmp_path / "repos"),
        )
        job = s.scalar(
            select(WorkspaceSetupJob).where(
                WorkspaceSetupJob.workspace_id == wsid
            )
        )
        worker._snapshot_and_init_repo(job)
        s.commit()

        # repo exists with three empty artifact files
        repo_dir = tmp_path / "repos" / str(wsid) / "repo"
        assert (repo_dir / ".git").is_dir()
        for fname in ("proposal-spec.md", "proposal-plan.md", "proposal.md"):
            assert (repo_dir / fname).is_file()
            assert (repo_dir / fname).read_text() == ""

        # one initial commit exists
        out = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-list", "--count", "HEAD"],
            check=True, capture_output=True, text=True,
        )
        assert int(out.stdout.strip()) == 1

        # snapshots populated
        ws = s.get(Workspace, wsid)
        assert (call.full_text or "") in ws.call_body_snapshot
        assert ws.profile_desc_snapshot == profile.description
```

- [ ] **Step 2: Run to confirm failure** — `AttributeError: '_snapshot_and_init_repo'`.

- [ ] **Step 3: Implement step 1–3 helper**

In `src/call_monitor/proposal_assist/phases/setup.py`, add:

```python
import os
import subprocess
from pathlib import Path

from call_monitor.db.models import Call, Profile, Workspace
```

Inside `Phase0Worker`, add:

```python
    _PHASE_ARTIFACT_FILES: tuple[str, ...] = (
        "proposal-spec.md",
        "proposal-plan.md",
        "proposal.md",
    )

    def _workspace_repo_dir(self, workspace_id) -> Path:
        return Path(self.workspace_repo_root) / str(workspace_id) / "repo"

    def _snapshot_and_init_repo(self, job: WorkspaceSetupJob) -> None:
        ws = self.session.get(Workspace, job.workspace_id)
        if ws is None:
            raise RuntimeError(f"workspace {job.workspace_id} vanished")

        # Step 1: init git repo with empty phase artifacts.
        repo_dir = self._workspace_repo_dir(ws.id)
        repo_dir.mkdir(parents=True, exist_ok=True)
        if not (repo_dir / ".git").is_dir():
            subprocess.run(
                ["git", "init", "-q", "-b", "main"],
                cwd=repo_dir, check=True,
            )
            # Identity for the initial commit — match the project's git author
            # convention; override via env if the running user has no global
            # gitconfig (common in CI / containers).
            env = os.environ.copy()
            env.setdefault("GIT_AUTHOR_NAME", "call-manager")
            env.setdefault("GIT_AUTHOR_EMAIL", "call-manager@local")
            env.setdefault("GIT_COMMITTER_NAME", "call-manager")
            env.setdefault("GIT_COMMITTER_EMAIL", "call-manager@local")
            for fname in self._PHASE_ARTIFACT_FILES:
                (repo_dir / fname).write_text("")
            subprocess.run(
                ["git", "add", "-A"], cwd=repo_dir, check=True, env=env,
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", "phase0: initialize empty artifacts"],
                cwd=repo_dir, check=True, env=env,
            )

        # Step 2: snapshot call body.
        if ws.call_id is None:
            raise RuntimeError(
                f"workspace {ws.id} has no call_id — Phase 0 requires one"
            )
        call = self.session.get(Call, ws.call_id)
        if call is None:
            raise RuntimeError(f"call {ws.call_id} not found")
        ws.call_body_snapshot = (call.summary or "") + "\n\n" + (call.full_text or "")

        # Step 3: snapshot profile description (if profile linked).
        if ws.profile_id is not None:
            profile = self.session.get(Profile, ws.profile_id)
            if profile is not None:
                ws.profile_desc_snapshot = profile.description or ""

        ws.snapshot_refreshed_at = datetime.now(timezone.utc)
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/proposal_assist/test_phase0_setup.py -v
```

Expected: scaffold tests + the new step 1–3 test pass. Verify the repo dir is created under `tmp_path` (no leakage into real filesystem).

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor/proposal_assist/phases/setup.py tests/proposal_assist/conftest.py tests/proposal_assist/test_phase0_setup.py
git commit -m "feat(proposal-assist): Phase 0 steps 1-3 (git init + snapshot call body + profile)"
```

---

## Task 15: Phase 0 — ensure templates fresh + copy to workspace (steps 4–5)

**Files:**
- Modify: `src/call_monitor/proposal_assist/phases/setup.py`
- Test: extend `tests/proposal_assist/test_phase0_setup.py`

- [ ] **Step 1: Append failing test**

```python
import json

from sqlalchemy import select


def test_phase0_step_4_copies_section_extracts_into_workspace(
    pg_session_factory, tmp_path,
):
    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        _, wsid = make_owner_and_workspace(s, call_id=call.id)
        s.add(WorkspaceSetupJob(workspace_id=wsid, status="running"))
        s.commit()

        client = Mock()
        client.model = "claude-opus-4-7"
        client.chat.return_value = {
            "choices": [{"message": {"role": "assistant", "content": json.dumps({
                "eligibility": "E", "scope": "S", "budget": "B",
                "expected_outcomes": "O", "restrictions": "R", "mandatory": "M",
            })}}],
            "model": "claude-opus-4-7",
        }

        worker = Phase0Worker(
            session=s, client=client,
            extractor_version="sections-v1",
            rubric_extractor_version="rubric-v1",
            workspace_repo_root=str(tmp_path / "repos"),
        )
        job = s.scalar(
            select(WorkspaceSetupJob).where(
                WorkspaceSetupJob.workspace_id == wsid
            )
        )
        worker._ensure_section_extracts_snapshot(job)
        s.commit()
        ws = s.get(Workspace, wsid)
        assert set(ws.section_extracts.keys()) == {
            "eligibility", "scope", "budget",
            "expected_outcomes", "restrictions", "mandatory",
        }
        assert ws.section_extracts["eligibility"] == "E"


def test_phase0_step_5_copies_rubric_into_workspace_and_writes_init_event(
    pg_session_factory, tmp_path,
):
    from call_monitor.db.models import WorkspaceEvent

    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        _, wsid = make_owner_and_workspace(s, call_id=call.id)
        s.add(WorkspaceSetupJob(workspace_id=wsid, status="running"))
        s.commit()

        client = Mock()
        client.model = "claude-opus-4-7"
        client.chat.return_value = {
            "choices": [{"message": {"role": "assistant", "content": json.dumps({
                "criteria": [{"name": "X", "description": "x",
                              "weight": 1.0, "scale_max": 5}],
                "funder_notes": "see FAQ",
            })}}],
            "model": "claude-opus-4-7",
        }

        worker = Phase0Worker(
            session=s, client=client,
            extractor_version="sections-v1",
            rubric_extractor_version="rubric-v1",
            workspace_repo_root=str(tmp_path / "repos"),
        )
        job = s.scalar(
            select(WorkspaceSetupJob).where(
                WorkspaceSetupJob.workspace_id == wsid
            )
        )
        worker._ensure_rubric_snapshot(job)
        s.commit()
        ws = s.get(Workspace, wsid)
        assert len(ws.rubric_criteria) == 1
        assert ws.rubric_criteria[0]["name"] == "X"
        assert ws.rubric_funder_notes == "see FAQ"

        # Audit event: rubric_initialized written exactly once with the seed payload.
        events = s.scalars(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == wsid,
                WorkspaceEvent.kind == "rubric_initialized",
            )
        ).all()
        assert len(events) == 1
        assert events[0].payload["criteria"][0]["name"] == "X"
        assert events[0].payload["funder_notes"] == "see FAQ"
```

(Adjust `WorkspaceEvent` constructor / payload-column name to match base spec's actual model — verify with `grep -n "class WorkspaceEvent" src/call_monitor/db/models.py`.)

- [ ] **Step 2: Run to confirm failure** — `AttributeError`.

- [ ] **Step 3: Implement steps 4–5**

In `src/call_monitor/proposal_assist/phases/setup.py`, add:

```python
from call_monitor.db.models import WorkspaceEvent
from call_monitor.proposal_assist.extractors.refresh import (
    ensure_rubric_fresh,
    ensure_section_extracts_fresh,
)
from call_monitor.proposal_assist.extractors.sections import SECTION_NAMES
```

Inside `Phase0Worker`:

```python
    def _ensure_section_extracts_snapshot(self, job: WorkspaceSetupJob) -> None:
        ws = self.session.get(Workspace, job.workspace_id)
        if ws is None or ws.call_id is None:
            raise RuntimeError("workspace or call missing for section extracts")
        call = self.session.get(Call, ws.call_id)
        if call is None:
            raise RuntimeError(f"call {ws.call_id} not found")

        ensure_section_extracts_fresh(
            session=self.session, call=call, client=self.client,
            extractor_version=self.extractor_version,
        )
        # Read back into a dict the workspace owns from now on.
        from call_monitor.db.models import CallSectionExtract
        rows = (
            self.session.query(CallSectionExtract)
            .filter(CallSectionExtract.call_id == call.id)
            .all()
        )
        ws.section_extracts = {r.section: r.extract_text for r in rows}
        # All six keys must be present.
        for name in SECTION_NAMES:
            ws.section_extracts.setdefault(name, "")

    def _ensure_rubric_snapshot(self, job: WorkspaceSetupJob) -> None:
        from call_monitor.db.models import CallRubric
        ws = self.session.get(Workspace, job.workspace_id)
        if ws is None or ws.call_id is None:
            raise RuntimeError("workspace or call missing for rubric")
        call = self.session.get(Call, ws.call_id)

        ensure_rubric_fresh(
            session=self.session, call=call, client=self.client,
            extractor_version=self.rubric_extractor_version,
        )
        template = self.session.get(CallRubric, call.id)
        ws.rubric_criteria = template.criteria
        ws.rubric_funder_notes = template.funder_notes
        ws.rubric_updated_at = datetime.now(timezone.utc)

        # Audit: write the canonical starting state for forward-replay.
        self.session.add(WorkspaceEvent(
            workspace_id=ws.id,
            kind="rubric_initialized",
            payload={
                "criteria": template.criteria,
                "funder_notes": template.funder_notes,
                "seeded_from_call_rubrics_extracted_at": (
                    template.extracted_at.isoformat()
                ),
            },
        ))
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/proposal_assist/test_phase0_setup.py -v
```

Expected: all phase0 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor/proposal_assist/phases/setup.py tests/proposal_assist/test_phase0_setup.py
git commit -m "feat(proposal-assist): Phase 0 steps 4-5 (extracts + rubric snapshot + init event)"
```

---

## Task 16: Phase 0 — `waiting_for_rubric_review` gate (step 6)

**Files:**
- Modify: `src/call_monitor/proposal_assist/phases/setup.py`
- Test: extend `tests/proposal_assist/test_phase0_setup.py`

- [ ] **Step 1: Append failing test**

```python
def test_phase0_yields_to_waiting_for_rubric_review(
    pg_session_factory, tmp_path,
):
    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        _, wsid = make_owner_and_workspace(s, call_id=call.id)
        s.add(WorkspaceSetupJob(workspace_id=wsid, status="queued"))
        s.commit()

        client = Mock()
        client.model = "claude-opus-4-7"
        # Two chat calls expected: sections + rubric.
        client.chat.side_effect = [
            {"choices": [{"message": {"role": "assistant", "content": json.dumps(
                {sec: f"x_{sec}" for sec in (
                    "eligibility","scope","budget",
                    "expected_outcomes","restrictions","mandatory")}
            )}}], "model": "claude-opus-4-7"},
            {"choices": [{"message": {"role": "assistant", "content": json.dumps({
                "criteria": [{"name": "C", "description": "c",
                              "weight": 1.0, "scale_max": 5}],
                "funder_notes": "",
            })}}], "model": "claude-opus-4-7"},
        ]

        worker = Phase0Worker(
            session=s, client=client,
            extractor_version="sections-v1",
            rubric_extractor_version="rubric-v1",
            workspace_repo_root=str(tmp_path / "repos"),
        )
        worker.run_one()
        s.expire_all()
        job = s.scalar(
            select(WorkspaceSetupJob).where(
                WorkspaceSetupJob.workspace_id == wsid
            )
        )
        ws = s.get(Workspace, wsid)
        assert job.status == "waiting_for_rubric_review"
        assert job.completed_at is None
        assert ws.current_phase == "setup"
```

- [ ] **Step 2: Run to confirm failure** — job ends up in "done" not "waiting".

- [ ] **Step 3: Implement step 6 + wire into `_run_steps`**

In `src/call_monitor/proposal_assist/phases/setup.py`, replace the `_run_steps` placeholder:

```python
    def _run_steps(self, job: WorkspaceSetupJob) -> None:
        # Steps 1-3: git init + snapshots
        self._snapshot_and_init_repo(job)
        # Step 4: section extracts
        self._ensure_section_extracts_snapshot(job)
        # Step 5: rubric + rubric_initialized event
        self._ensure_rubric_snapshot(job)
        # Step 6: yield to owner rubric review.
        # The job stays at status='waiting_for_rubric_review' until the owner
        # confirms via the rubric-review endpoint (Task 18), which sets
        # status='done' and advances workspaces.current_phase.
        job.status = "waiting_for_rubric_review"
        # NOT setting completed_at — the job isn't terminal yet.
```

(Task 13's `run_one` already handles the `waiting_for_rubric_review` branch — no further edit to `run_one` is needed here.)

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/proposal_assist/test_phase0_setup.py -v
```

Expected: all phase0 tests pass; the new test confirms the waiting state.

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor/proposal_assist/phases/setup.py tests/proposal_assist/test_phase0_setup.py
git commit -m "feat(proposal-assist): Phase 0 step 6 (yield to waiting_for_rubric_review)"
```

---

## Task 17: Owner rubric-review endpoint — GET form

**Files:**
- Create: `src/call_monitor/proposal_assist/web/__init__.py` (empty)
- Create: `src/call_monitor/proposal_assist/web/rubric_review.py`
- Create: `src/call_monitor/web/templates/proposal_assist/rubric_review.html`
- Test: `tests/proposal_assist/test_rubric_review_endpoint.py`

- [ ] **Step 1: Write failing tests**

`tests/proposal_assist/test_rubric_review_endpoint.py`:

```python
"""Owner-only rubric review endpoint. TestClient pattern mirrors
tests/test_web_admin_digest_panel.py — see plan's 'Project Conventions
Reference' for the auth shape."""
import uuid

import pytest
from fastapi.testclient import TestClient

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import User, Workspace, WorkspaceSetupJob
from call_monitor.main import create_app


def _build_app(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _authed_client(app, *, user_id: int) -> TestClient:
    """Authenticated TestClient. Caller must client.__exit__ on cleanup
    (we enter the context manager here to fire lifespan)."""
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=user_id))
    return client


def _make_owner_and_workspace(session, *, owner_email: str = "owner@ki.se"):
    """Create a User + a Workspace owned by them. Returns (user_id, workspace_id)
    — IDs only so callers can use them after the session closes."""
    owner = User(email=owner_email)
    session.add(owner)
    session.flush()
    ws = Workspace(
        owner_id=owner.id,
        title="Test workspace",
        rubric_criteria=[
            {"name": "Excellence", "description": "x",
             "weight": 1.0, "scale_max": 5},
        ],
    )
    session.add(ws)
    session.commit()
    return owner.id, ws.id


def test_rubric_review_get_returns_form_for_owner(
    monkeypatch, pg_url, pg_session_factory,
):
    app = _build_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        uid, wsid = _make_owner_and_workspace(s)

    client = _authed_client(app, user_id=uid)
    try:
        resp = client.get(f"/workspaces/{wsid}/rubric-review")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    assert "Excellence" in resp.text


def test_rubric_review_get_returns_403_for_non_owner(
    monkeypatch, pg_url, pg_session_factory,
):
    app = _build_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        _, wsid = _make_owner_and_workspace(s, owner_email="owner@ki.se")
        other = User(email="other@ki.se")
        s.add(other)
        s.commit()
        other_id = other.id

    client = _authed_client(app, user_id=other_id)
    try:
        resp = client.get(f"/workspaces/{wsid}/rubric-review")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403


def test_rubric_review_get_returns_404_for_missing_workspace(
    monkeypatch, pg_url, pg_session_factory,
):
    app = _build_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="x@ki.se")
        s.add(u)
        s.commit()
        uid = u.id

    client = _authed_client(app, user_id=uid)
    try:
        resp = client.get(f"/workspaces/{uuid.uuid4()}/rubric-review")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 404
```

Note: the `Workspace` model (with `owner_id`, `title`, `rubric_criteria`) is added by the base spec / earlier tasks in this plan. If `Workspace` doesn't yet exist when running this task, surface back to the user — Part 2 ordering requires the workspace columns from Task 9 to be in place first.

- [ ] **Step 2: Run to confirm failure**

- [ ] **Step 3: Implement endpoint + template**

Create `src/call_monitor/proposal_assist/web/__init__.py` (empty).

Create `src/call_monitor/proposal_assist/web/rubric_review.py`. The endpoint reads its session factory and templates instance from `app.state`. Both are populated during `create_app()` — `session_signer` is already there; you'll add `session_factory` and `templates` next to it (see Step 4 below).

```python
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from call_monitor.auth.dependencies import get_current_user
from call_monitor.db.models import Workspace
from call_monitor.db.session import get_db_dependency

router = APIRouter(prefix="/workspaces", tags=["proposal-assist"])


def _require_owner(
    *, workspace_id: uuid.UUID, user, session: Session,
) -> Workspace:
    ws = session.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if ws.owner_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="only the workspace owner may view or edit the rubric",
        )
    return ws


def _session_dep(request: Request):
    """Resolve the per-request session dependency from the app's factory.
    `get_db_dependency` is a factory that returns the actual dependency
    callable; we wire it via app.state to avoid a global at import time."""
    factory = request.app.state.session_factory
    yield from get_db_dependency(factory)()


@router.get(
    "/{workspace_id}/rubric-review",
    response_class=HTMLResponse,
)
def get_rubric_review(
    workspace_id: uuid.UUID,
    request: Request,
    user = Depends(get_current_user),
    session: Session = Depends(_session_dep),
):
    ws = _require_owner(
        workspace_id=workspace_id, user=user, session=session,
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "proposal_assist/rubric_review.html",
        {"workspace": ws, "criteria": ws.rubric_criteria,
         "funder_notes": ws.rubric_funder_notes or ""},
    )
```

**Step 4 — wire `app.state.session_factory` and `app.state.templates` in `create_app()`.**

In `src/call_monitor/main.py`, find the existing block that constructs the templates env and the session signer (search for `Jinja2Templates` and `session_signer = `). Add immediately after them:

```python
    app.state.templates = templates
    app.state.session_factory = session_factory  # the sessionmaker created
                                                  # elsewhere in create_app()
```

If `session_factory` isn't bound as a local name in `create_app()` (i.e., the existing app uses a different DI shape), surface back to the user before continuing — Slice A's web endpoint depends on it.

Create `src/call_monitor/web/templates/proposal_assist/rubric_review.html`:

```html
{% extends "base.html" %}

{% block title %}Rubric review — {{ workspace.title }}{% endblock %}

{% block content %}
<h1>Review the funder's rubric</h1>
<p>
  The system extracted these criteria from the call. Edit anything that looks
  wrong — your edits stay scoped to this workspace.
</p>

<form method="post"
      action="/workspaces/{{ workspace.id }}/rubric-review"
      hx-post="/workspaces/{{ workspace.id }}/rubric-review"
      hx-swap="outerHTML">
  <ol id="criteria-list">
    {% for crit in criteria %}
      <li>
        <input type="text" name="criteria[{{ loop.index0 }}][name]"
               value="{{ crit.name }}" required>
        <textarea name="criteria[{{ loop.index0 }}][description]"
                  rows="2" required>{{ crit.description }}</textarea>
        <label>weight
          <input type="number" step="0.01" min="0" max="1"
                 name="criteria[{{ loop.index0 }}][weight]"
                 value="{{ crit.weight }}" required>
        </label>
        <label>scale max
          <input type="number" min="1" max="10"
                 name="criteria[{{ loop.index0 }}][scale_max]"
                 value="{{ crit.scale_max }}" required>
        </label>
      </li>
    {% endfor %}
  </ol>
  <label for="funder-notes">Funder notes (optional)</label>
  <textarea id="funder-notes" name="funder_notes" rows="4">{{ funder_notes }}</textarea>
  <button type="submit">Confirm rubric and proceed to Spec</button>
</form>
{% endblock %}
```

Register the router in `src/call_monitor/main.py`:

```python
from call_monitor.proposal_assist.web.rubric_review import router as rubric_review_router

# inside create_app():
app.include_router(rubric_review_router)
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/proposal_assist/test_rubric_review_endpoint.py::test_rubric_review_get_returns_form_for_owner -v
uv run pytest tests/proposal_assist/test_rubric_review_endpoint.py::test_rubric_review_get_returns_403_for_non_owner -v
uv run pytest tests/proposal_assist/test_rubric_review_endpoint.py::test_rubric_review_get_returns_404_for_missing_workspace -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor/proposal_assist/web/__init__.py src/call_monitor/proposal_assist/web/rubric_review.py src/call_monitor/web/templates/proposal_assist/rubric_review.html src/call_monitor/main.py tests/proposal_assist/test_rubric_review_endpoint.py
git commit -m "feat(proposal-assist): rubric review GET endpoint (owner-only)"
```

---

## Task 18: Owner rubric-review endpoint — POST submit + advance Phase 0 + write `rubric_edited`

**Files:**
- Modify: `src/call_monitor/proposal_assist/web/rubric_review.py`
- Test: extend `tests/proposal_assist/test_rubric_review_endpoint.py`

- [ ] **Step 1: Append failing tests**

```python
def test_rubric_review_post_writes_edited_event_and_advances_phase(
    monkeypatch, pg_url, pg_session_factory,
):
    from call_monitor.db.models import WorkspaceEvent

    app = _build_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        uid, wsid = _make_owner_and_workspace(s)
        # Override rubric/criteria to a known starting state and put the
        # workspace in setup with a waiting job.
        ws = s.get(Workspace, wsid)
        ws.rubric_criteria = [
            {"name": "Old", "description": "old",
             "weight": 1.0, "scale_max": 5},
        ]
        ws.rubric_funder_notes = ""
        ws.current_phase = "setup"
        s.add(WorkspaceSetupJob(
            workspace_id=wsid, status="waiting_for_rubric_review",
        ))
        s.commit()

    client = _authed_client(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/rubric-review",
            data={
                "criteria[0][name]": "New",
                "criteria[0][description]": "new",
                "criteria[0][weight]": "1.0",
                "criteria[0][scale_max]": "5",
                "funder_notes": "updated",
            },
            follow_redirects=False,
        )
    finally:
        client.__exit__(None, None, None)
    # Successful confirm → 303 to the workspace page (non-HTMX path).
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/workspaces/{wsid}"

    with pg_session_factory() as s:
        ws = s.get(Workspace, wsid)
        assert ws.rubric_criteria[0]["name"] == "New"
        assert ws.rubric_funder_notes == "updated"
        assert ws.current_phase == "spec"
        job = s.execute(
            select(WorkspaceSetupJob).where(
                WorkspaceSetupJob.workspace_id == wsid
            )
        ).scalar_one()
        assert job.status == "done"
        assert job.completed_at is not None
        edits = s.execute(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == wsid,
                WorkspaceEvent.kind == "rubric_edited",
            )
        ).scalars().all()
        assert len(edits) == 1
        assert edits[0].payload["old_criteria"][0]["name"] == "Old"
        assert edits[0].payload["new_criteria"][0]["name"] == "New"
        assert edits[0].payload["edited_by_user_id"] == uid


def test_rubric_review_post_confirm_without_edits_still_advances_phase(
    monkeypatch, pg_url, pg_session_factory,
):
    app = _build_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        uid, wsid = _make_owner_and_workspace(s)
        ws = s.get(Workspace, wsid)
        ws.rubric_criteria = [
            {"name": "X", "description": "x",
             "weight": 1.0, "scale_max": 5},
        ]
        ws.current_phase = "setup"
        s.add(WorkspaceSetupJob(
            workspace_id=wsid, status="waiting_for_rubric_review",
        ))
        s.commit()

    client = _authed_client(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/rubric-review",
            data={
                "criteria[0][name]": "X",
                "criteria[0][description]": "x",
                "criteria[0][weight]": "1.0",
                "criteria[0][scale_max]": "5",
                "funder_notes": "",
            },
            follow_redirects=False,
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 303  # confirms phase advanced even without edits

    with pg_session_factory() as s:
        ws = s.get(Workspace, wsid)
        job = s.execute(
            select(WorkspaceSetupJob).where(
                WorkspaceSetupJob.workspace_id == wsid
            )
        ).scalar_one()
        assert ws.current_phase == "spec"
        assert job.status == "done"


def test_rubric_review_post_returns_403_for_non_owner(
    monkeypatch, pg_url, pg_session_factory,
):
    app = _build_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        _, wsid = _make_owner_and_workspace(s, owner_email="owner@ki.se")
        other = User(email="other@ki.se")
        s.add(other)
        s.commit()
        other_id = other.id

    client = _authed_client(app, user_id=other_id)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/rubric-review",
            data={"criteria[0][name]": "X", "criteria[0][description]": "x",
                  "criteria[0][weight]": "1.0", "criteria[0][scale_max]": "5",
                  "funder_notes": ""},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403
```

Add `from sqlalchemy import select` and `from call_monitor.db.models import WorkspaceSetupJob` to the imports at the top of the test file if not already present.

- [ ] **Step 2: Run to confirm failure** — endpoint returns 405 or doesn't exist.

- [ ] **Step 3: Implement the POST handler**

In `src/call_monitor/proposal_assist/web/rubric_review.py`, add:

```python
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import Response
from fastapi.responses import HTMLResponse

from call_monitor.db.models import WorkspaceEvent, WorkspaceSetupJob


def _parse_form_criteria(form: dict[str, str]) -> list[dict[str, Any]]:
    """Parse criteria[N][field] form keys into a list[dict].

    FastAPI's Form() decoding gives flat key/value pairs; we re-nest by index.
    """
    pattern = re.compile(r"^criteria\[(\d+)\]\[(\w+)\]$")
    rows: dict[int, dict[str, Any]] = {}
    for key, value in form.items():
        m = pattern.match(key)
        if not m:
            continue
        idx, field = int(m.group(1)), m.group(2)
        rows.setdefault(idx, {})[field] = value

    out: list[dict[str, Any]] = []
    for idx in sorted(rows.keys()):
        crit = rows[idx]
        # Cast numeric fields.
        try:
            crit["weight"] = float(crit.get("weight", 0))
            crit["scale_max"] = int(crit.get("scale_max", 5))
        except (TypeError, ValueError) as e:
            raise HTTPException(400, f"criterion {idx}: bad numeric value ({e})")
        # Ensure required keys present.
        for required in ("name", "description", "weight", "scale_max"):
            if required not in crit:
                raise HTTPException(400, f"criterion {idx}: missing {required}")
        out.append(crit)
    if not out:
        raise HTTPException(400, "rubric must have at least one criterion")
    return out


@router.post(
    "/{workspace_id}/rubric-review",
    response_class=HTMLResponse,
)
async def post_rubric_review(
    workspace_id: uuid.UUID,
    request: Request,
    user = Depends(get_current_user),
    session: Session = Depends(_session_dep),
):
    ws = _require_owner(workspace_id=workspace_id, user=user, session=session)

    form = dict(await request.form())
    new_criteria = _parse_form_criteria(form)
    new_funder_notes = form.get("funder_notes", "")

    old_criteria = ws.rubric_criteria
    old_funder_notes = ws.rubric_funder_notes or ""

    # Only write a rubric_edited event when content actually changed.
    content_changed = (
        new_criteria != old_criteria
        or new_funder_notes != old_funder_notes
    )

    ws.rubric_criteria = new_criteria
    ws.rubric_funder_notes = new_funder_notes
    if content_changed:
        ws.rubric_updated_at = datetime.now(timezone.utc)
        session.add(WorkspaceEvent(
            workspace_id=ws.id,
            kind="rubric_edited",
            payload={
                "old_criteria": old_criteria,
                "new_criteria": new_criteria,
                "old_funder_notes": old_funder_notes,
                "new_funder_notes": new_funder_notes,
                "edited_by_user_id": user.id,
            },
        ))

    # Advance Phase 0 if this is the first review beat (job exists and is
    # waiting_for_rubric_review).
    job = (
        session.query(WorkspaceSetupJob)
        .filter(WorkspaceSetupJob.workspace_id == ws.id)
        .one_or_none()
    )
    if job is not None and job.status == "waiting_for_rubric_review":
        job.status = "done"
        job.completed_at = datetime.now(timezone.utc)
        ws.current_phase = "spec"

    session.commit()

    # After advancing to Spec, redirect the browser to the workspace's main
    # view. HTMX recognizes the HX-Redirect header and performs a client-
    # side navigation; non-HTMX (full-page form post) gets a normal 303.
    if ws.current_phase == "spec":
        target = f"/workspaces/{ws.id}"
        if "HX-Request" in request.headers:
            return Response(status_code=204, headers={"HX-Redirect": target})
        return Response(status_code=303, headers={"Location": target})

    # Otherwise re-render the form with the updated content (HTMX swap target).
    return request.app.state.templates.TemplateResponse(
        request,
        "proposal_assist/rubric_review.html",
        {"workspace": ws, "criteria": ws.rubric_criteria,
         "funder_notes": ws.rubric_funder_notes or ""},
    )
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/proposal_assist/test_rubric_review_endpoint.py -v
```

Expected: all rubric-review tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor/proposal_assist/web/rubric_review.py tests/proposal_assist/test_rubric_review_endpoint.py
git commit -m "feat(proposal-assist): rubric review POST (owner-only, advances Phase 0 to spec)"
```

---

## Task 19: Register the Phase 0 scheduler tick

The worker is drainable now, but nothing drives it. The project's existing
`build_scheduler` (in `src/call_monitor/scheduler.py`) is an APScheduler
`BackgroundScheduler`; add an interval job that calls `Phase0Worker.run_one`
on a configurable cadence.

**Files:**
- Modify: `src/call_monitor/config.py` — add `phase0_tick_seconds`
- Modify: `src/call_monitor/scheduler.py` — register the Phase 0 job
- Modify: `src/call_monitor/proposal_assist/phases/setup.py` — add a top-level
  `run_phase0_tick(session_factory, client_factory, settings)` driver
- Test: `tests/proposal_assist/test_phase0_scheduler_tick.py`

- [ ] **Step 1: Add the setting**

In `Settings`:

```python
    phase0_tick_seconds: int = Field(
        default=30, alias="PHASE0_TICK_SECONDS",
        description="How often the in-process Phase 0 worker drains "
                    "workspace_setup_jobs. Lowering this shortens the gap "
                    "between workspace creation and rubric-review prompt; "
                    "raising it reduces idle DB churn when the queue is empty.",
    )
```

- [ ] **Step 2: Write the failing test**

`tests/proposal_assist/test_phase0_scheduler_tick.py`:

```python
"""The scheduler-tick driver: on each tick, claims one job and runs it. The
driver is sync (matches BackgroundScheduler's worker pool) and opens its own
session per tick so APScheduler isn't holding a long-lived one."""
from unittest.mock import Mock

from sqlalchemy import select

from call_monitor.db.models import WorkspaceSetupJob
from call_monitor.proposal_assist.phases.setup import run_phase0_tick
from tests.proposal_assist.conftest import (
    make_call, make_owner_and_workspace,
)


def test_run_phase0_tick_drains_one_queued_job(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        _, wsid = make_owner_and_workspace(s, call_id=call.id)
        s.add(WorkspaceSetupJob(workspace_id=wsid, status="queued"))
        s.commit()

    client = Mock(model="claude-opus-4-7")
    # _run_steps stub keeps this test scoped to the driver wiring.
    captured: dict = {}

    def fake_run_steps(worker, job):
        captured["worker_seen"] = True

    from call_monitor.proposal_assist.phases.setup import Phase0Worker
    Phase0Worker._run_steps = fake_run_steps  # type: ignore[method-assign]

    run_phase0_tick(
        session_factory=pg_session_factory,
        client_factory=lambda: client,
        extractor_version="sections-v1",
        rubric_extractor_version="rubric-v1",
        workspace_repo_root=str(tmp_path / "repos"),
    )

    with pg_session_factory() as s:
        job = s.scalar(
            select(WorkspaceSetupJob).where(
                WorkspaceSetupJob.workspace_id == wsid
            )
        )
        assert job.status == "done"
        assert captured.get("worker_seen") is True


def test_run_phase0_tick_is_a_noop_when_queue_empty(pg_session_factory, tmp_path):
    # No jobs in the queue. The driver should return cleanly.
    run_phase0_tick(
        session_factory=pg_session_factory,
        client_factory=lambda: Mock(model="m"),
        extractor_version="sections-v1",
        rubric_extractor_version="rubric-v1",
        workspace_repo_root=str(tmp_path / "repos"),
    )
    # No assertion needed — absence of exception is the contract.
```

- [ ] **Step 3: Implement the driver**

Append to `src/call_monitor/proposal_assist/phases/setup.py`:

```python
from typing import Callable

from sqlalchemy.orm import sessionmaker


def run_phase0_tick(
    *,
    session_factory: sessionmaker,
    client_factory: Callable[[], _AnthropicClientLike],
    extractor_version: str,
    rubric_extractor_version: str,
    workspace_repo_root: str,
) -> None:
    """Scheduler-friendly tick: open one session, claim+drain at most one job.

    A single-job-per-tick discipline keeps individual ticks short (so the
    BackgroundScheduler worker pool doesn't pile up), gives Postgres' FOR
    UPDATE SKIP LOCKED a clean release point, and bounds the cost of a
    failed LLM call to one workspace per tick.

    Multiple ticks per second are fine — claim_next_setup_job returns None
    when the queue is empty and the function returns immediately.
    """
    with session_factory() as session:
        worker = Phase0Worker(
            session=session,
            client=client_factory(),
            extractor_version=extractor_version,
            rubric_extractor_version=rubric_extractor_version,
            workspace_repo_root=workspace_repo_root,
        )
        worker.run_one()
```

- [ ] **Step 4: Register in `build_scheduler`**

In `src/call_monitor/scheduler.py`, after the existing `sched.add_job(...)`
calls for ingestion / ranking, add:

```python
    # Phase 0 (proposal-assist) drain tick.
    sched.add_job(
        _phase0_tick_job,
        trigger=IntervalTrigger(seconds=settings.phase0_tick_seconds),
        id="proposal_assist_phase0_tick",
        max_instances=1,        # single in-flight tick at a time
        coalesce=True,           # collapse missed runs
    )
```

And define `_phase0_tick_job` (at module scope, alongside the other job
callables in this file):

```python
def _phase0_tick_job(settings, session_factory) -> None:
    """Closure wired up in build_scheduler. Lazy-import the worker to keep
    scheduler module-load cheap and avoid importing the Anthropic SDK at
    process start (mirrors the existing ranking-tick pattern)."""
    from call_monitor.proposal_assist.phases.setup import run_phase0_tick
    from call_monitor.ranking.anthropic_client import AnthropicClient

    if not settings.anthropic_api_key:
        # No key configured → Phase 0 cannot run. Log once per tick at INFO
        # so the operator sees it but it doesn't spam at WARNING.
        logger.info(
            "phase0 tick skipped: ANTHROPIC_API_KEY not set"
        )
        return

    def make_client():
        return AnthropicClient(
            api_key=settings.anthropic_api_key,
            model=settings.proposal_assist_llm_model,
        )

    run_phase0_tick(
        session_factory=session_factory,
        client_factory=make_client,
        extractor_version=settings.extractor_version,
        rubric_extractor_version=settings.rubric_extractor_version,
        workspace_repo_root=settings.workspace_repo_root,  # NEW SETTING
    )
```

The `WORKSPACE_REPO_ROOT` setting belongs to the base spec (it points at
`var/workspaces/`). If base spec hasn't added it yet, add a `workspace_repo_root`
field to `Settings` here with default `"var/workspaces"`.

Pass `settings` + `session_factory` into the closure when calling
`sched.add_job` — the existing scheduler functions already curry these in;
mirror that pattern.

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/proposal_assist/test_phase0_scheduler_tick.py -v
```

Expected: 2 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/call_monitor/config.py src/call_monitor/scheduler.py src/call_monitor/proposal_assist/phases/setup.py tests/proposal_assist/test_phase0_scheduler_tick.py
git commit -m "feat(proposal-assist): register Phase 0 drain tick in scheduler"
```

---

## Task 20: Phase 0 happy-path end-to-end test

**Files:**
- Test: `tests/proposal_assist/test_phase0_e2e.py`

- [ ] **Step 1: Write the test**

`tests/proposal_assist/test_phase0_e2e.py`:

```python
"""End-to-end happy path: workspace created → Phase 0 runs → owner confirms
rubric → workspace.current_phase = 'spec', setup job is done.

LLM is mocked: AnthropicClient.chat returns canned section + rubric envelopes.
Git operations run for real against a tmp_path repo root.
"""
import json
from unittest.mock import Mock

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import (
    CallRubric,
    CallSectionExtract,
    Workspace,
    WorkspaceEvent,
    WorkspaceSetupJob,
)
from call_monitor.main import create_app
from call_monitor.proposal_assist.extractors.sections import SECTION_NAMES
from call_monitor.proposal_assist.phases.setup import Phase0Worker
from tests.proposal_assist.conftest import (
    make_call, make_owner_and_workspace, make_profile,
)


def _section_envelope() -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": json.dumps(
            {s: f"section text for {s}" for s in SECTION_NAMES}
        )}}],
        "model": "claude-opus-4-7",
    }


def _rubric_envelope() -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": json.dumps({
            "criteria": [
                {"name": "Excellence", "description": "Scientific quality",
                 "weight": 0.5, "scale_max": 5},
                {"name": "Impact", "description": "Translational potential",
                 "weight": 0.5, "scale_max": 5},
            ],
            "funder_notes": "See FAQ.",
        })}}],
        "model": "claude-opus-4-7",
    }


def test_phase0_happy_path_to_waiting_for_rubric_review(
    monkeypatch, pg_url, pg_session_factory, tmp_path,
):
    # Phase 0 part: run the worker directly against the test DB.
    with pg_session_factory() as s:
        call = make_call(s)
        profile = make_profile(s)
        s.commit()
        uid, wsid = make_owner_and_workspace(
            s, call_id=call.id, profile_id=profile.id,
        )
        s.add(WorkspaceSetupJob(workspace_id=wsid, status="queued"))
        s.commit()
        call_id = call.id

        llm = Mock()
        llm.model = "claude-opus-4-7"
        llm.chat.side_effect = [_section_envelope(), _rubric_envelope()]

        worker = Phase0Worker(
            session=s, client=llm,
            extractor_version="sections-v1",
            rubric_extractor_version="rubric-v1",
            workspace_repo_root=str(tmp_path / "repos"),
        )
        processed = worker.run_one()
        assert processed is True

        s.expire_all()
        job = s.scalar(
            select(WorkspaceSetupJob).where(
                WorkspaceSetupJob.workspace_id == wsid
            )
        )
        ws = s.get(Workspace, wsid)
        assert job.status == "waiting_for_rubric_review"
        assert job.completed_at is None
        assert ws.current_phase == "setup"

        # call template tables populated
        assert s.query(CallSectionExtract).filter_by(
            call_id=call_id).count() == 6
        assert s.get(CallRubric, call_id) is not None

        # workspace snapshot populated
        assert ws.call_body_snapshot != ""
        assert ws.profile_desc_snapshot != ""
        assert set(ws.section_extracts.keys()) == set(SECTION_NAMES)
        assert len(ws.rubric_criteria) == 2
        assert ws.rubric_funder_notes == "See FAQ."

        # init event written
        init_events = s.scalars(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == wsid,
                WorkspaceEvent.kind == "rubric_initialized",
            )
        ).all()
        assert len(init_events) == 1

        # repo on disk
        repo_dir = tmp_path / "repos" / str(wsid) / "repo"
        assert (repo_dir / "proposal-spec.md").exists()
        assert (repo_dir / ".git").is_dir()

    # Endpoint part: spin up the FastAPI app and POST as the owner.
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    try:
        signer: SessionSigner = app.state.session_signer
        client.cookies.set("session", signer.make(user_id=uid))
        resp = client.post(
            f"/workspaces/{wsid}/rubric-review",
            data={
                "criteria[0][name]": "Excellence",
                "criteria[0][description]": "Scientific quality",
                "criteria[0][weight]": "0.5",
                "criteria[0][scale_max]": "5",
                "criteria[1][name]": "Impact",
                "criteria[1][description]": "Translational potential",
                "criteria[1][weight]": "0.5",
                "criteria[1][scale_max]": "5",
                "funder_notes": "See FAQ.",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
    finally:
        client.__exit__(None, None, None)

    with pg_session_factory() as s:
        ws = s.get(Workspace, wsid)
        job = s.scalar(
            select(WorkspaceSetupJob).where(
                WorkspaceSetupJob.workspace_id == wsid
            )
        )
        assert ws.current_phase == "spec"
        assert job.status == "done"
        assert job.completed_at is not None
```

Note: the `make_owner_and_workspace`, `make_call`, and `make_profile` helpers are defined in `tests/proposal_assist/conftest.py` per the Project Conventions Reference. No additional `sample_user` / `another_user` fixtures are needed in conftest — the helpers create them inline.

- [ ] **Step 2: Run**

```bash
uv run pytest tests/proposal_assist/test_phase0_e2e.py -v
```

Expected: the happy-path test passes.

- [ ] **Step 3: Commit**

```bash
git add tests/proposal_assist/conftest.py tests/proposal_assist/test_phase0_e2e.py
git commit -m "test(proposal-assist): Phase 0 happy-path end-to-end"
```

---

## Task 21: Slice A milestone

- [ ] **Step 1: Full suite + lint**

```bash
uv run pytest tests/proposal_assist/ -v
uv run ruff check src/call_monitor/proposal_assist tests/proposal_assist
uv run ruff format src/call_monitor/proposal_assist tests/proposal_assist
```

Expected: ~35 tests pass, no lint issues.

- [ ] **Step 2: Migration round-trip check**

```bash
uv run alembic downgrade 0011
uv run alembic upgrade head
uv run pytest tests/proposal_assist/test_models.py -v
```

Verifies that the migrations are reversible and that re-applying them gives the same schema. If downgrade fails, the migration's `downgrade()` is incomplete — fix and re-commit.

- [ ] **Step 3: Stop and surface to user**

Slice A is complete. The codebase now has:
- All 7 new tables/column-additions migrated
- Section + rubric extractors with idempotent freshness gating
- Phase 0 worker that snapshots, extracts, copies templates, writes the `rubric_initialized` audit event, and yields at `waiting_for_rubric_review`
- Phase 0 scheduler tick registered in `build_scheduler` — given a queued `workspace_setup_jobs` row, the worker drains it on the next interval
- Owner-only rubric review GET + POST that advances Phase 0 to `done` and the workspace to `current_phase='spec'`
- An end-to-end happy-path test exercising the full lifecycle with mocked LLM and real git operations

End state: given a queued `workspace_setup_jobs` row (the workspace-create site that enqueues it is base-spec territory — see the goal at the top), Phase 0 runs to the rubric-review gate, the owner confirms, `current_phase` advances to `spec`.

**Next slice:** Slice B (Spec/Plan/Draft phases + state machine + staleness + rubric editing UI). That builds the interactive chat phases on top of the Phase 0 foundation. Surface to the user before starting it.

---

## Self-review notes (for the engineer running this plan)

1. **`WorkspaceEvent` model shape** — the plan assumes a `workspaces`-FK + `kind` + `payload (jsonb)` columns. If base spec ships a different shape, the audit-event writes in Tasks 15 and 18 need re-shaping.

2. **`app.state.session_factory`** — Tasks 17/18 assume `create_app()` puts the session factory on `app.state` next to the existing `session_signer`. If it doesn't, the simplest patch is a one-line addition inside `create_app()`; if the existing app uses a different DI shape, surface back to the user before continuing.

3. **Migration revision numbers** — `0012`–`0017` assume no other branch is also adding migrations after `0011`. If a concurrent branch has bumped past `0011`, renumber accordingly (and update `down_revision` chains).

4. **Slice rename to `call_manager`** — base spec calls for a `call-monitor → call-manager` rename. This plan uses the un-renamed `src/call_monitor/proposal_assist/` paths. If the rename has happened by the time Part 2 starts, update all `call_monitor` imports to `call_manager`.

---

## Explicitly out of Slice A scope

- **Manual-paste rubric fallback** (spec lines 290, 517 — "Permanent failure surfaces with option to manually paste rubric"; "user must paste rubric UI flag"). If the rubric extractor fails 5 times, `workspace_setup_jobs` ends in `failed` and the workspace cannot proceed. The recovery UX — a route that accepts user-pasted criteria + funder notes, validates the same shape the extractor outputs, writes them to `workspaces.rubric_criteria` + a `rubric_initialized` event, and advances Phase 0 to `done` — is a follow-on task. It is not blocked by Slice A; it can be added once an operator hits a real extractor failure. Track this as a known gap when surfacing Slice A completion to the user.

- **APScheduler ingestion hook** — the freshness helpers from Task 7 are callable but not invoked at ingestion. Slice E (observability + ops hooks) owns wiring them into the existing 03:00 / 03:15 Stockholm cron ticks so newly-ingested calls get extracts + rubric without a workspace having to trigger them.

- **Spec/Plan/Draft phase modules, Review fan-out, cost guardrails, MODIFIED/OUTDATED computation, rubric-edit settings UI** — Slices B / C / D / E respectively.
