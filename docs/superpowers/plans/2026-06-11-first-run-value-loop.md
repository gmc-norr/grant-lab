# First-Run Value Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make call-manager deliver its core promise by default — a newly created profile is bootstrapped asynchronously without freezing the browser, gets ranked on its own with visible progress, subscribes its creator to the digest, and becomes the dashboard's default — without touching ranking-model internals.

**Architecture:** A DB-backed `profile_bootstrap_jobs` queue drained by a scheduler tick (mirroring `WorkspaceSetupJob`/`run_phase0_tick`). Profile + job + subscription + default are inserted in one atomic transaction (mirroring `create_workspace`). The profile is read-only while its bootstrap job is non-terminal, so the worker is the sole writer and owns the single embed+backfill. Bootstrap success/failure is detected via explicit outcome fields on `DraftProfile` (the function never throws). A per-user `users.default_profile_id` makes the ranked view sticky.

**Tech Stack:** Python 3.12, FastAPI + Jinja2 + HTMX, SQLAlchemy 2.x + Alembic, APScheduler, PostgreSQL 16, pytest (+ testcontainers Postgres).

**Spec:** [docs/superpowers/specs/2026-06-11-first-run-value-loop-design.md](../specs/2026-06-11-first-run-value-loop-design.md)

**PRECONDITION (cross-plan dependency):** the ranking-v2.1 plan reserves Alembic migrations `0042–0045`. This plan's migration is `0046` with `down_revision = "0045"`. Before starting Task 1, confirm `0042–0045` are merged (`uv run alembic heads` shows `0045`). **If they are not yet merged, set `down_revision` to the actual current head and rename the migration file to `head+1`** — the rest of the plan is unaffected.

**Test commands:** `uv run pytest <path> -v`. Lint/format: `uv run ruff check . && uv run ruff format .`.

---

## File Structure

**Created:**
- `src/call_manager/profiles/create.py` — `create_profile_with_bootstrap()` atomic service (Task 4).
- `src/call_manager/profiles/bootstrap_worker.py` — the bootstrap-job worker + stuck-job reaper (Task 5).
- `alembic/versions/0046_first_run_value_loop.py` — schema migration (Task 1).
- `src/call_manager/web/templates/proposal_assist/` — N/A. New templates:
  - `src/call_manager/web/templates/partials/bootstrap_status.html` — bootstrap progress/completeness fragment (Task 7).
  - `src/call_manager/web/templates/partials/ranking_status_strip.html` — ranking-status strip fragment (Task 8).
- Test modules: `tests/test_migration_0046_first_run_value_loop.py`, `tests/test_profiles_bootstrap_outcome.py`, `tests/test_profiles_create_service.py`, `tests/test_web_profiles_new_async.py`, `tests/test_profiles_bootstrap_worker.py`, `tests/test_web_profiles_edit_guards.py`, `tests/test_web_bootstrap_status.py`, `tests/test_web_ranking_status_strip.py`, `tests/test_web_calls_sticky_default.py`.

**Modified:**
- `src/call_manager/db/models.py` — `ProfileBootstrapJob` model + `User.default_profile_id` (Task 1).
- `src/call_manager/profiles/seed.py` — `DraftProfile` outcome fields (Task 2).
- `src/call_manager/profiles/bootstrap.py` — set the outcome fields (Task 2).
- `src/call_manager/web/routes/profiles.py` — async create route + seed caps + edit guards (Tasks 4, 6, 7).
- `src/call_manager/scheduler.py` — register the bootstrap tick (Task 5).
- `src/call_manager/config.py` — `profile_bootstrap_tick_seconds` (Task 5).
- `src/call_manager/web/routes/calls.py` — sticky default + ranking-status fragment route (Tasks 8, 9).
- `src/call_manager/web/templates/profile_edit.html` — embed the bootstrap status fragment (Task 7).
- `src/call_manager/web/templates/calls_index.html` — embed the ranking-status strip (Task 8).

---

## Phase 1 — Schema foundation (migration 0046)

### Task 1: `ProfileBootstrapJob` model + `users.default_profile_id` + migration 0046

**Files:**
- Modify: `src/call_manager/db/models.py`
- Create: `alembic/versions/0046_first_run_value_loop.py`
- Test: `tests/test_migration_0046_first_run_value_loop.py`

- [ ] **Step 1: Write the failing migration round-trip + model test**

```python
# tests/test_migration_0046_first_run_value_loop.py
import uuid

from sqlalchemy import inspect, text

from call_manager.db.models import (
    ProfileBootstrapJob,
    Profile,
    ProfileKind,
    User,
)


def _mk_profile(session) -> int:
    u = User(email="creator@ki.se")
    session.add(u)
    session.flush()
    p = Profile(name="Grp", kind=ProfileKind.ORGANIZATIONAL_UNIT.value, level="group",
                description="", structured={}, created_by=u.id)
    session.add(p)
    session.flush()
    return p.id, u.id


def test_bootstrap_job_table_and_default_profile_column_exist(pg_session_factory):
    with pg_session_factory() as s:
        insp = inspect(s.bind)
        assert "profile_bootstrap_jobs" in insp.get_table_names()
        user_cols = {c["name"] for c in insp.get_columns("users")}
        assert "default_profile_id" in user_cols


def test_bootstrap_job_round_trips_and_is_unique_per_profile(pg_session_factory):
    with pg_session_factory() as s:
        pid, _ = _mk_profile(s)
        s.add(ProfileBootstrapJob(profile_id=pid, status="queued",
                                  seed={"name": "Grp", "members": []}))
        s.commit()
        # Second job for the same profile violates UNIQUE(profile_id).
        dup = s.execute(
            text("INSERT INTO profile_bootstrap_jobs (id, profile_id, status) "
                 "VALUES (:i, :p, 'queued') ON CONFLICT DO NOTHING RETURNING id"),
            {"i": str(uuid.uuid4()), "p": pid},
        ).first()
        assert dup is None


def test_default_profile_id_set_null_on_profile_delete(pg_session_factory):
    with pg_session_factory() as s:
        pid, uid = _mk_profile(s)
        u = s.get(User, uid)
        u.default_profile_id = pid
        s.commit()
        s.execute(text("DELETE FROM profiles WHERE id = :p"), {"p": pid})
        s.commit()
        s.expire_all()
        assert s.get(User, uid).default_profile_id is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_migration_0046_first_run_value_loop.py -v`
Expected: FAIL — `ImportError: cannot import name 'ProfileBootstrapJob'`.

- [ ] **Step 3: Add the model + column to `src/call_manager/db/models.py`**

Add `default_profile_id` to the `User` class (near the other user columns; `BigInteger` matches `Profile.id` — verify `Profile.id` is `BigInteger`):

```python
    default_profile_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
```

Add the new model (place it near `WorkspaceSetupJob`; reuse the already-imported `JSONB`, `UUID`, `BigInteger`, `SmallInteger`, `Text`, `Integer`, `DateTime`, `func`, `text`, `ForeignKey`, `CheckConstraint`, `UniqueConstraint`):

```python
class ProfileBootstrapJob(Base):
    __tablename__ = "profile_bootstrap_jobs"
    __table_args__ = (
        UniqueConstraint("profile_id", name="uq_profile_bootstrap_jobs_profile"),
        CheckConstraint(
            "status IN ('queued','running','done','failed')",
            name="ck_profile_bootstrap_jobs_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Bootstrap inputs (members/orcids, pasted_grants, mission_text, kind, level, name).
    # CLEARED (set null) on terminal completion — see retention in the spec.
    seed: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    pubmed_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    publications_fetched: Mapped[int | None] = mapped_column(Integer, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 4: Write the migration `alembic/versions/0046_first_run_value_loop.py`**

```python
"""First-run value loop: profile_bootstrap_jobs table + users.default_profile_id.

Revision ID: 0046
Revises: 0045
Create Date: 2026-06-11

See docs/superpowers/plans/2026-06-11-first-run-value-loop.md Task 1.
PRECONDITION: ranking-v2.1 migrations 0042-0045 are merged. If not, set
down_revision to the current head and rename this file accordingly.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "profile_bootstrap_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("profile_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("seed", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("pubmed_status", sa.Text(), nullable=True),
        sa.Column("publications_fetched", sa.Integer(), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("profile_id", name="uq_profile_bootstrap_jobs_profile"),
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed')",
            name="ck_profile_bootstrap_jobs_status",
        ),
    )
    op.add_column(
        "users",
        sa.Column("default_profile_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_users_default_profile_id",
        "users",
        "profiles",
        ["default_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_users_default_profile_id", "users", type_="foreignkey")
    op.drop_column("users", "default_profile_id")
    op.drop_table("profile_bootstrap_jobs")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_migration_0046_first_run_value_loop.py -v`
Expected: PASS (the `migrated_engine` fixture runs `alembic upgrade head`, creating the new objects).

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check . && uv run ruff format .
git add src/call_manager/db/models.py alembic/versions/0046_first_run_value_loop.py tests/test_migration_0046_first_run_value_loop.py
git commit -m "feat(profiles): schema for first-run value loop (bootstrap jobs + default profile)"
```

---

## Phase 2 — Bootstrap outcome detection

### Task 2: explicit `llm_succeeded` + `pubmed_status` on `DraftProfile`

**Files:**
- Modify: `src/call_manager/profiles/seed.py`
- Modify: `src/call_manager/profiles/bootstrap.py`
- Test: `tests/test_profiles_bootstrap_outcome.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_profiles_bootstrap_outcome.py
from call_manager.profiles.bootstrap import bootstrap_profile
from call_manager.profiles.pubmed import PubMedError
from call_manager.profiles.seed import Member, SeedInput
from call_manager.ranking.ollama_client import LLMError


class _Pub:  # minimal stand-in matching pubmed.fetch_publications return shape
    def __init__(self, n):
        self._n = n

    def fetch_publications(self, members):
        return [object()] * self._n


class _PubFail:
    def fetch_publications(self, members):
        raise PubMedError("down")


class _LLM:
    def chat(self, *, messages):
        return {"choices": [{"message": {"content": '{"description": "Solid draft.", "structured": {}}'}}]}


class _LLMFail:
    def chat(self, *, messages):
        raise LLMError("model down")


def _seed(members):
    return SeedInput(name="G", kind="organizational_unit", members=members,
                     mission_text="x", pasted_grants="")


def test_llm_success_with_pubs_is_ok():
    d = bootstrap_profile(seed=_seed([Member(orcid="0000-0002-1825-0097")]),
                          pubmed=_Pub(3), ollama=_LLM())
    assert d.llm_succeeded is True
    assert d.pubmed_status == "ok"
    assert d.publications_fetched == 3


def test_pubmed_empty_vs_unavailable_vs_not_queried():
    m = [Member(orcid="0000-0002-1825-0097")]
    assert bootstrap_profile(seed=_seed(m), pubmed=_Pub(0), ollama=_LLM()).pubmed_status == "empty"
    assert bootstrap_profile(seed=_seed(m), pubmed=_PubFail(), ollama=_LLM()).pubmed_status == "unavailable"
    assert bootstrap_profile(seed=_seed([]), pubmed=_Pub(0), ollama=_LLM()).pubmed_status == "not_queried"


def test_llm_failure_sets_llm_succeeded_false():
    d = bootstrap_profile(seed=_seed([]), pubmed=_Pub(0), ollama=_LLMFail())
    assert d.llm_succeeded is False
    assert d.description == ""
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_profiles_bootstrap_outcome.py -v`
Expected: FAIL — `AttributeError: 'DraftProfile' object has no attribute 'llm_succeeded'`.

- [ ] **Step 3: Add fields to `DraftProfile` in `src/call_manager/profiles/seed.py`**

Append to the `DraftProfile` dataclass (defaults keep existing 4-arg constructions — e.g. test helpers — working):

```python
    # Explicit outcomes (bootstrap never raises). Worker branches on these.
    llm_succeeded: bool = True
    pubmed_status: str = "ok"  # 'ok' | 'empty' | 'unavailable' | 'not_queried'
```

- [ ] **Step 4: Set the outcomes in `src/call_manager/profiles/bootstrap.py`**

Replace the PubMed block (lines ~31-49) so it records a status, and track LLM success:

```python
    # 1. PubMed (best-effort) — record an explicit outcome.
    publications = []
    publications_skipped = 0
    if not seed.members:
        pubmed_status = "not_queried"
    else:
        try:
            publications = pubmed.fetch_publications(seed.members)
            pubmed_status = "ok" if publications else "empty"
        except PubMedError as e:
            pubmed_status = "unavailable"
            _logger.warning(
                "profiles.bootstrap.pubmed_failed",
                extra={"event": "bootstrap_pubmed_failed", "error": str(e)},
            )
    if publications:
        publications_skipped = 0
    elif seed.members:
        publications_skipped = len(seed.members)
```

In the LLM block, set `llm_succeeded` only on a parsed, non-empty draft:

```python
    description = ""
    structured: dict = {
        "areas": [], "budget_min": None, "budget_max": None,
        "trl_min": None, "trl_max": None, "must_include": [], "hard_exclusions": [],
    }
    llm_succeeded = False
    try:
        envelope = ollama.chat(messages=messages)
        content = envelope["choices"][0]["message"].get("content")
        if content:
            draft = parse_bootstrap_response(content)
            description = draft.description
            structured = draft.structured
            llm_succeeded = True
    except (LLMError, BootstrapParseError, KeyError, IndexError, TypeError, AttributeError) as e:
        _logger.warning(
            "profiles.bootstrap.llm_failed",
            extra={"event": "bootstrap_llm_failed", "error": str(e)},
        )
```

Update the `return`:

```python
    return DraftProfile(
        description=description,
        structured=structured,
        publications_fetched=len(publications),
        publications_skipped=publications_skipped,
        llm_succeeded=llm_succeeded,
        pubmed_status=pubmed_status,
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_profiles_bootstrap_outcome.py tests/test_profiles_bootstrap.py -v`
Expected: PASS (existing bootstrap tests still pass — the new fields default).

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check . && uv run ruff format .
git add src/call_manager/profiles/seed.py src/call_manager/profiles/bootstrap.py tests/test_profiles_bootstrap_outcome.py
git commit -m "feat(profiles): explicit bootstrap outcome (llm_succeeded, pubmed_status)"
```

---

## Phase 3 — Atomic create + worker + edit guards

### Task 3: shared bootstrap-merge helper

**Files:**
- Modify: `src/call_manager/profiles/create.py` (new file, created here for the helper; the service lands in Task 4)
- Test: `tests/test_profiles_create_service.py`

The worker (Task 5) must write the drafted description and fill only the profile fields the user left empty. Extract that as a pure helper now so both the worker and its test can use it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_profiles_create_service.py
from call_manager.profiles.create import merge_bootstrap_into_profile
from call_manager.profiles.seed import DraftProfile


class _P:  # stand-in for a Profile row
    def __init__(self, kind, description, structured):
        self.kind = kind
        self.description = description
        self.structured = structured


def test_merge_fills_empty_description_and_keeps_user_areas():
    p = _P("organizational_unit", "", {"areas": ["oncology"], "must_include": []})
    draft = DraftProfile(
        description="Drafted summary.",
        structured={"areas": ["genomics"], "eligibility": {"career_stage": "senior"}},
        publications_fetched=2, publications_skipped=0,
    )
    merge_bootstrap_into_profile(p, draft)
    assert p.description == "Drafted summary."
    assert p.structured["areas"] == ["oncology"]          # user value wins
    assert p.structured["eligibility"]["career_stage"] == "senior"  # filled from draft


def test_merge_does_not_overwrite_nonempty_description():
    p = _P("organizational_unit", "User wrote this.", {"areas": []})
    draft = DraftProfile(description="LLM draft.", structured={"areas": ["x"]},
                         publications_fetched=0, publications_skipped=0)
    merge_bootstrap_into_profile(p, draft)
    assert p.description == "User wrote this."
    assert p.structured["areas"] == ["x"]                  # user left areas empty → filled
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_profiles_create_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'call_manager.profiles.create'`.

- [ ] **Step 3: Create `src/call_manager/profiles/create.py` with the helper**

```python
from __future__ import annotations

from call_manager.profiles.fields import MAX_LIST_ENTRIES, MAX_TAG_LEN, sanitize_structured
from call_manager.profiles.seed import DraftProfile

_NAMESPACES = ("eligibility", "org", "funding", "geo")


def merge_bootstrap_into_profile(profile, draft: DraftProfile) -> None:
    """Write the LLM draft into an existing profile WITHOUT overwriting any
    field the user already filled. Safe because the profile is read-only while
    its bootstrap job is non-terminal (the worker is the sole writer)."""
    if not (profile.description or "").strip():
        profile.description = draft.description

    draft_clean = sanitize_structured(kind=profile.kind, structured=draft.structured)
    current = dict(profile.structured or {})

    if not current.get("areas"):
        draft_areas = [a for a in draft.structured.get("areas", []) if len(str(a)) <= MAX_TAG_LEN]
        current["areas"] = draft_areas[:MAX_LIST_ENTRIES]

    for ns in _NAMESPACES:
        base_ns = draft_clean.get(ns, {})
        user_ns = {k: v for k, v in (current.get(ns) or {}).items() if v not in (None, [], "")}
        if base_ns or user_ns:
            current[ns] = {**base_ns, **user_ns}  # user values win
    profile.structured = current
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_profiles_create_service.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check . && uv run ruff format .
git add src/call_manager/profiles/create.py tests/test_profiles_create_service.py
git commit -m "feat(profiles): non-destructive bootstrap-merge helper"
```

### Task 4: atomic `create_profile_with_bootstrap` service + async create route + seed caps

**Files:**
- Modify: `src/call_manager/profiles/create.py`
- Modify: `src/call_manager/web/routes/profiles.py` (the `POST /profiles/new` handler + `_parse_members`)
- Test: `tests/test_profiles_create_service.py`, `tests/test_web_profiles_new_async.py`

- [ ] **Step 1: Write the failing service + route tests**

```python
# tests/test_profiles_create_service.py  (append)
from call_manager.db.models import ProfileBootstrapJob, Profile, Subscription, User


def test_create_profile_with_bootstrap_is_atomic(pg_session_factory):
    from call_manager.profiles.create import create_profile_with_bootstrap
    with pg_session_factory() as s:
        u = User(email="c@ki.se")
        s.add(u)
        s.flush()
        p = create_profile_with_bootstrap(
            session=s, owner_id=u.id, name="Grp", kind="organizational_unit",
            level="group", structured={"areas": ["oncology"]},
            seed_payload={"name": "Grp", "members": [], "mission_text": "m", "pasted_grants": ""},
        )
        s.commit()
        pid = p.id
    with pg_session_factory() as s:
        assert s.get(Profile, pid).description == ""
        assert s.query(ProfileBootstrapJob).filter_by(profile_id=pid, status="queued").count() == 1
        assert s.get(Subscription, (u.id, pid)) is not None
        assert s.get(User, u.id).default_profile_id == pid
```

```python
# tests/test_web_profiles_new_async.py
from fastapi.testclient import TestClient

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import ProfileBootstrapJob, Profile, Subscription, User
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _client(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="u@ki.se", is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid


def test_new_profile_does_no_llm_in_request_and_enqueues_job(monkeypatch, pg_url, pg_session_factory):
    # Fail loudly if bootstrap_profile is called in-request.
    def _boom(**_):
        raise AssertionError("bootstrap_profile must not run in the request")
    monkeypatch.setattr("call_manager.web.routes.profiles.bootstrap_profile", _boom, raising=False)

    app = _app(monkeypatch, pg_url)
    c, uid = _client(app, pg_session_factory)
    r = c.post("/profiles/new", data={"name": "Grp", "kind": "organizational_unit",
                                      "level": "group", "mission_text": "m"}, follow_redirects=False)
    assert r.status_code == 303
    assert "/edit" in r.headers["location"]
    with pg_session_factory() as s:
        p = s.query(Profile).filter_by(name="Grp").one()
        assert p.description == ""
        assert s.query(ProfileBootstrapJob).filter_by(profile_id=p.id, status="queued").count() == 1
        assert s.get(Subscription, (uid, p.id)) is not None
        assert s.get(User, uid).default_profile_id == p.id


def test_new_profile_rejects_oversized_mission(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, _ = _client(app, pg_session_factory)
    r = c.post("/profiles/new", data={"name": "Grp", "mission_text": "x" * 10_241})
    assert r.status_code == 413
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_profiles_create_service.py tests/test_web_profiles_new_async.py -v`
Expected: FAIL — `ImportError: cannot import name 'create_profile_with_bootstrap'`.

- [ ] **Step 3: Add the service to `src/call_manager/profiles/create.py`**

```python
from call_manager.db.models import Profile, ProfileBootstrapJob, Subscription, User


def create_profile_with_bootstrap(
    *, session, owner_id: int, name: str, kind: str, level: str | None,
    structured: dict, seed_payload: dict,
) -> Profile:
    """Atomic: insert the profile (empty description), enqueue its bootstrap
    job, subscribe the creator, and seed their default profile if unset.
    Mirrors create_workspace — the CALLER commits, so a failure rolls all of
    it back (no orphan profile)."""
    p = Profile(name=name, kind=kind, level=level, description="",
                structured=structured, created_by=owner_id)
    session.add(p)
    session.flush()  # populate p.id

    session.add(ProfileBootstrapJob(profile_id=p.id, status="queued", seed=seed_payload))
    session.add(Subscription(user_id=owner_id, profile_id=p.id, digest_enabled=True))

    u = session.get(User, owner_id)
    if u is not None and u.default_profile_id is None:
        u.default_profile_id = p.id
    return p
```

- [ ] **Step 4: Rewrite `POST /profiles/new` in `src/call_manager/web/routes/profiles.py`**

Replace the body from the `members = _parse_members(...)` line through the `return RedirectResponse(...)` (drop the inline `bootstrap_profile` call, the inline merge, and the background embedding task; the `BackgroundTasks` param is no longer needed for create). Add the mission cap near the other caps at the top of the handler:

```python
    if len(mission_text) > 10_240:
        raise HTTPException(status_code=413, detail="mission text too large (max 10 KB)")
```

Then, after the structured validation and duplicate-name pre-check:

```python
    members = _parse_members(orcids, names_affiliations)  # now capped (Step 5)
    seed_payload = {
        "name": name,
        "kind": kind,
        "level": level_val,
        "members": [
            {"orcid": m.orcid, "name": m.name, "affiliation": m.affiliation} for m in members
        ],
        "pasted_grants": pasted_grants,
        "mission_text": mission_text,
    }
    try:
        with session_factory() as session:
            profile = create_profile_with_bootstrap(
                session=session, owner_id=user.id, name=name, kind=kind,
                level=level_val, structured={**user_structured}, seed_payload=seed_payload,
            )
            session.commit()
            session.refresh(profile)
            new_id = profile.id
    except IntegrityError as e:
        raise HTTPException(status_code=409, detail="profile name already exists") from e

    return RedirectResponse(url=f"/profiles/{new_id}/edit", status_code=303)
```

Add the import at the top: `from call_manager.profiles.create import create_profile_with_bootstrap`. Remove now-unused imports if ruff flags them (`bootstrap_profile`, `OllamaClient`, `PubMedClient`, `sanitize_structured`, `refresh_profile_embedding`, `backfill_profile`, `MAX_TAG_LEN`, `MAX_LIST_ENTRIES`) **only if** no other handler in the file still uses them — the edit handler keeps `backfill_profile`/`refresh_profile_embedding`, so leave those.

> Note: `{**user_structured}` carries the user-entered areas/namespaces. The LLM-suggested namespaces are merged later by the worker via `merge_bootstrap_into_profile`. The old create-time merge is intentionally gone.

- [ ] **Step 5: Cap members in `_parse_members`**

Replace the two loops' append guards so the total is capped at 50 and each line at 500 chars:

```python
def _parse_members(orcids: str, names_affiliations: str) -> list[Member]:
    members: list[Member] = []
    _MAX_MEMBERS = 50
    for raw in orcids.splitlines():
        orcid = raw.strip()[:500]
        if orcid and len(members) < _MAX_MEMBERS:
            with contextlib.suppress(ValueError):
                members.append(Member(orcid=orcid))
    for raw in names_affiliations.splitlines():
        line = raw.strip()[:500]
        if not line or "|" not in line or len(members) >= _MAX_MEMBERS:
            continue
        name_part, aff_part = line.split("|", 1)
        name_part, aff_part = name_part.strip(), aff_part.strip()
        if name_part and aff_part:
            with contextlib.suppress(ValueError):
                members.append(Member(name=name_part, affiliation=aff_part))
    return members
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_profiles_create_service.py tests/test_web_profiles_new_async.py tests/test_web_profiles.py -v`
Expected: PASS. (If `test_web_profiles.py` had a test asserting the old synchronous-bootstrap behavior on create, update it to assert the new enqueue behavior — the create path no longer drafts in-request.)

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check . && uv run ruff format .
git add src/call_manager/profiles/create.py src/call_manager/web/routes/profiles.py tests/test_profiles_create_service.py tests/test_web_profiles_new_async.py
git commit -m "feat(profiles): atomic async profile creation with bootstrap job + auto-subscribe + default"
```

### Task 5: the bootstrap-job worker + stuck-job reaper + scheduler wiring

**Files:**
- Create: `src/call_manager/profiles/bootstrap_worker.py`
- Modify: `src/call_manager/config.py`, `src/call_manager/scheduler.py`
- Test: `tests/test_profiles_bootstrap_worker.py`

- [ ] **Step 1: Write the failing worker test**

```python
# tests/test_profiles_bootstrap_worker.py
from call_manager.db.models import ProfileBootstrapJob, Profile, ProfileKind, User
from call_manager.profiles.bootstrap_worker import run_profile_bootstrap_tick
from call_manager.profiles.seed import DraftProfile


class _FakePubmed:
    def fetch_publications(self, members):
        return []


class _FakeEmbed:
    model = "fake-embed"

    def embed(self, text):
        return [0.0] * 8


def _enqueue(session_factory):
    with session_factory() as s:
        u = User(email="c@ki.se")
        s.add(u)
        s.flush()
        p = Profile(name="Grp", kind=ProfileKind.ORGANIZATIONAL_UNIT.value, level="group",
                    description="", structured={"areas": []}, created_by=u.id)
        s.add(p)
        s.flush()
        s.add(ProfileBootstrapJob(profile_id=p.id, status="queued",
                                  seed={"name": "Grp", "kind": "organizational_unit",
                                        "level": "group", "members": [], "pasted_grants": "",
                                        "mission_text": "m"}))
        s.commit()
        return p.id


def test_success_writes_description_embeds_backfills_and_clears_seed(monkeypatch, pg_session_factory):
    pid = _enqueue(pg_session_factory)
    calls = {"embed": 0, "backfill": 0}
    monkeypatch.setattr("call_manager.profiles.bootstrap_worker.bootstrap_profile",
                        lambda **_: DraftProfile("Drafted.", {"areas": []}, 0, 0,
                                                 llm_succeeded=True, pubmed_status="not_queried"))
    monkeypatch.setattr("call_manager.profiles.bootstrap_worker.refresh_profile_embedding",
                        lambda *a, **k: calls.__setitem__("embed", calls["embed"] + 1))
    monkeypatch.setattr("call_manager.profiles.bootstrap_worker.backfill_profile",
                        lambda **k: calls.__setitem__("backfill", calls["backfill"] + 1) or 0)

    run_profile_bootstrap_tick(session_factory=pg_session_factory, pubmed=_FakePubmed(),
                               ollama=object(), embed_client=_FakeEmbed())

    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        job = s.query(ProfileBootstrapJob).filter_by(profile_id=pid).one()
        assert p.description == "Drafted."
        assert job.status == "done"
        assert job.pubmed_status == "not_queried"
        assert job.seed is None  # cleared on terminal completion
    assert calls == {"embed": 1, "backfill": 1}


def test_llm_failure_marks_failed_and_skips_backfill(monkeypatch, pg_session_factory):
    pid = _enqueue(pg_session_factory)
    called = {"backfill": 0}
    monkeypatch.setattr("call_manager.profiles.bootstrap_worker.bootstrap_profile",
                        lambda **_: DraftProfile("", {"areas": []}, 0, 0,
                                                 llm_succeeded=False, pubmed_status="not_queried"))
    monkeypatch.setattr("call_manager.profiles.bootstrap_worker.refresh_profile_embedding",
                        lambda *a, **k: None)
    monkeypatch.setattr("call_manager.profiles.bootstrap_worker.backfill_profile",
                        lambda **k: called.__setitem__("backfill", 1) or 0)

    # Exhaust attempts by running the tick MAX times.
    for _ in range(5):
        run_profile_bootstrap_tick(session_factory=pg_session_factory, pubmed=_FakePubmed(),
                                   ollama=object(), embed_client=_FakeEmbed())

    with pg_session_factory() as s:
        job = s.query(ProfileBootstrapJob).filter_by(profile_id=pid).one()
        assert job.status == "failed"
        assert s.get(Profile, pid).description == ""
    assert called["backfill"] == 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_profiles_bootstrap_worker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'call_manager.profiles.bootstrap_worker'`.

- [ ] **Step 3: Create `src/call_manager/profiles/bootstrap_worker.py`**

```python
"""Bootstrap-job worker — drains profile_bootstrap_jobs.

Mirrors proposal_assist.phases.setup.run_phase0_tick: claim one queued job
under FOR UPDATE SKIP LOCKED, run the (best-effort, never-raising)
bootstrap, then branch on the explicit outcome. The profile is read-only
while the job is non-terminal, so the worker is the sole writer.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from call_manager.db.models import Profile, ProfileBootstrapJob
from call_manager.embeddings.refresh import refresh_profile_embedding
from call_manager.profiles.bootstrap import bootstrap_profile
from call_manager.profiles.create import merge_bootstrap_into_profile
from call_manager.profiles.seed import Member, SeedInput
from call_manager.ranking.backfill import backfill_profile

logger = logging.getLogger(__name__)

MAX_BOOTSTRAP_ATTEMPTS = 5
STUCK_BOOTSTRAP_AFTER = timedelta(minutes=30)


def _seed_from_payload(payload: dict) -> SeedInput:
    members = []
    for m in payload.get("members", []):
        try:
            members.append(Member(orcid=m.get("orcid"), name=m.get("name"),
                                  affiliation=m.get("affiliation")))
        except ValueError:
            continue
    return SeedInput(
        name=payload.get("name", ""), kind=payload.get("kind", "organizational_unit"),
        level=payload.get("level"), members=members,
        pasted_grants=payload.get("pasted_grants", ""), mission_text=payload.get("mission_text", ""),
    )


def requeue_stuck_bootstrap_jobs(session: Session, *, max_attempts: int = MAX_BOOTSTRAP_ATTEMPTS) -> int:
    """Recover jobs stuck 'running' past STUCK_BOOTSTRAP_AFTER (worker crash).
    Caller commits."""
    cutoff = datetime.now(UTC) - STUCK_BOOTSTRAP_AFTER
    dead = session.execute(
        update(ProfileBootstrapJob)
        .where(ProfileBootstrapJob.status == "running",
               ProfileBootstrapJob.started_at.is_not(None),
               ProfileBootstrapJob.started_at < cutoff,
               ProfileBootstrapJob.attempts + 1 >= max_attempts)
        .values(status="failed", attempts=ProfileBootstrapJob.attempts + 1,
                last_error="dead-lettered: stuck running past max_attempts", seed=None,
                completed_at=datetime.now(UTC))
    ).rowcount
    session.execute(
        update(ProfileBootstrapJob)
        .where(ProfileBootstrapJob.status == "running",
               ProfileBootstrapJob.started_at.is_not(None),
               ProfileBootstrapJob.started_at < cutoff)
        .values(status="queued", started_at=None)
    )
    return dead


def run_profile_bootstrap_tick(*, session_factory: sessionmaker, pubmed, ollama, embed_client) -> None:
    with session_factory() as session:
        requeue_stuck_bootstrap_jobs(session)
        session.commit()

    with session_factory() as session:
        job = session.execute(
            select(ProfileBootstrapJob)
            .where(ProfileBootstrapJob.status == "queued")
            .order_by(ProfileBootstrapJob.queued_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        ).scalar_one_or_none()
        if job is None:
            return
        job.status = "running"
        job.started_at = datetime.now(UTC)
        job.attempts += 1
        profile = session.get(Profile, job.profile_id)
        seed_payload = dict(job.seed or {})
        session.commit()

    draft = bootstrap_profile(seed=_seed_from_payload(seed_payload), pubmed=pubmed, ollama=ollama)

    with session_factory() as session:
        job = session.get(ProfileBootstrapJob, job.id)
        profile = session.get(Profile, job.profile_id)
        if not draft.llm_succeeded:
            if job.attempts >= MAX_BOOTSTRAP_ATTEMPTS:
                job.status = "failed"
                job.last_error = "llm draft failed"
                job.completed_at = datetime.now(UTC)
                job.seed = None
            else:
                job.status = "queued"
                job.started_at = None
                job.last_error = "llm draft failed (will retry)"
            session.commit()
            return

        merge_bootstrap_into_profile(profile, draft)
        refresh_profile_embedding(session, embed_client, profile.id)
        session.commit()

    backfill_profile(session_factory=session_factory, profile_id=job.profile_id)

    with session_factory() as session:
        job = session.get(ProfileBootstrapJob, job.id)
        job.status = "done"
        job.completed_at = datetime.now(UTC)
        job.pubmed_status = draft.pubmed_status
        job.publications_fetched = draft.publications_fetched
        job.seed = None
        session.commit()
```

- [ ] **Step 4: Add the config tick interval to `src/call_manager/config.py`**

Near `phase0_tick_seconds`:

```python
    profile_bootstrap_tick_seconds: int = Field(
        default=30, alias="PROFILE_BOOTSTRAP_TICK_SECONDS"
    )
```

- [ ] **Step 5: Register the tick in `src/call_manager/scheduler.py`**

`build_scheduler` already receives `embed_client`. After the Phase-0 tick registration, add:

```python
    def _profile_bootstrap_tick() -> None:
        from call_manager.profiles.bootstrap_worker import run_profile_bootstrap_tick
        from call_manager.profiles.pubmed import PubMedClient
        from call_manager.ranking.ollama_client import OllamaClient
        try:
            run_profile_bootstrap_tick(
                session_factory=session_factory,
                pubmed=PubMedClient(),
                ollama=OllamaClient(base_url=settings.ollama_url, model=settings.gemma_model),
                embed_client=embed_client,
            )
        except Exception as e:  # noqa: BLE001
            _logger.warning("profile_bootstrap.tick_failed",
                            extra={"event": "profile_bootstrap_tick_failed", "error": str(e)})

    sched.add_job(
        _profile_bootstrap_tick,
        trigger=IntervalTrigger(seconds=settings.profile_bootstrap_tick_seconds),
        id="profile_bootstrap_tick",
        max_instances=1,
        coalesce=True,
    )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_profiles_bootstrap_worker.py -v`
Expected: PASS.

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check . && uv run ruff format .
git add src/call_manager/profiles/bootstrap_worker.py src/call_manager/config.py src/call_manager/scheduler.py tests/test_profiles_bootstrap_worker.py
git commit -m "feat(profiles): bootstrap-job worker + scheduler tick (embed+backfill on success, no backfill on failure)"
```

### Task 6: edit-route guards (block while drafting; skip backfill on blank description)

**Files:**
- Modify: `src/call_manager/web/routes/profiles.py` (`profile_edit_submit`)
- Test: `tests/test_web_profiles_edit_guards.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_profiles_edit_guards.py
from fastapi.testclient import TestClient

from call_manager.db.models import ProfileBootstrapJob, Profile, ProfileKind, User
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _client_owner(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="owner@ki.se", is_admin=False)
        s.add(u)
        s.flush()
        p = Profile(name="Grp", kind=ProfileKind.ORGANIZATIONAL_UNIT.value, level="group",
                    description="", structured={"areas": []}, created_by=u.id)
        s.add(p)
        s.flush()
        s.add(ProfileBootstrapJob(profile_id=p.id, status="running"))
        s.commit()
        uid, pid = u.id, p.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, pid


def test_edit_blocked_while_bootstrap_active(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid = _client_owner(app, pg_session_factory)
    r = c.post(f"/profiles/{pid}/edit", data={"description": "x", "budget_currency": "SEK"},
               follow_redirects=False)
    assert r.status_code == 409


def test_blank_description_save_does_not_backfill(monkeypatch, pg_url, pg_session_factory):
    called = {"n": 0}
    monkeypatch.setattr("call_manager.web.routes.profiles.backfill_profile",
                        lambda **k: called.__setitem__("n", called["n"] + 1) or 0)
    app = _app(monkeypatch, pg_url)
    # Profile with a TERMINAL job (done) so edit is allowed.
    with pg_session_factory() as s:
        u = User(email="o2@ki.se", is_admin=False)
        s.add(u)
        s.flush()
        p = Profile(name="G2", kind=ProfileKind.ORGANIZATIONAL_UNIT.value, level="group",
                    description="", structured={"areas": []}, created_by=u.id)
        s.add(p)
        s.flush()
        s.add(ProfileBootstrapJob(profile_id=p.id, status="done"))
        s.commit()
        uid, pid = u.id, p.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    r = c.post(f"/profiles/{pid}/edit", data={"description": "   ", "budget_currency": "SEK"},
               follow_redirects=False)
    assert r.status_code == 303
    # background task runs on shutdown; assert it was never scheduled by checking no call after request
    assert called["n"] == 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_web_profiles_edit_guards.py -v`
Expected: FAIL — the edit currently returns 303 even with an active job, and schedules backfill regardless of description.

- [ ] **Step 3: Add the guards to `profile_edit_submit` in `src/call_manager/web/routes/profiles.py`**

Right after loading the profile and the owner-or-admin check, add the active-bootstrap guard:

```python
        active_job = session.execute(
            select(ProfileBootstrapJob.id).where(
                ProfileBootstrapJob.profile_id == profile_id,
                ProfileBootstrapJob.status.in_(("queued", "running")),
            )
        ).scalar_one_or_none()
        if active_job is not None:
            raise HTTPException(status_code=409, detail="profile is still drafting — try again shortly")
```

Add the import: `from call_manager.db.models import ProfileBootstrapJob` (extend the existing models import line). Then gate the background backfill on a non-blank description — replace the unconditional `background.add_task(_refresh_and_backfill_in_background, ...)` with:

```python
    if description.strip():
        embed_client = request.app.state.embed_client
        background.add_task(
            _refresh_and_backfill_in_background,
            session_factory=session_factory,
            embed_client=embed_client,
            profile_id=profile_id,
            refresh_embedding=description_changed,
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_profiles_edit_guards.py tests/test_web_profiles.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check . && uv run ruff format .
git add src/call_manager/web/routes/profiles.py tests/test_web_profiles_edit_guards.py
git commit -m "feat(profiles): block edits while bootstrap active; skip backfill on blank description"
```

---

## Phase 4 — Progress UI + ranking-status strip

### Task 7: bootstrap progress/completeness fragment

**Files:**
- Modify: `src/call_manager/web/routes/profiles.py` (new `GET /profiles/{id}/bootstrap-status`)
- Create: `src/call_manager/web/templates/partials/bootstrap_status.html`
- Modify: `src/call_manager/web/templates/profile_edit.html`
- Test: `tests/test_web_bootstrap_status.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_bootstrap_status.py
from fastapi.testclient import TestClient

from call_manager.db.models import ProfileBootstrapJob, Profile, ProfileKind, User
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _setup(app, pg_session_factory, status, **jobkw):
    with pg_session_factory() as s:
        u = User(email="owner@ki.se", is_admin=False)
        s.add(u)
        s.flush()
        p = Profile(name="Grp", kind=ProfileKind.ORGANIZATIONAL_UNIT.value, level="group",
                    description="", structured={}, created_by=u.id)
        s.add(p)
        s.flush()
        s.add(ProfileBootstrapJob(profile_id=p.id, status=status, **jobkw))
        s.commit()
        uid, pid = u.id, p.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, pid


def test_running_shows_drafting(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid = _setup(app, pg_session_factory, "running")
    r = c.get(f"/profiles/{pid}/bootstrap-status")
    assert r.status_code == 200
    assert "Drafting" in r.text


def test_done_unavailable_wording(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid = _setup(app, pg_session_factory, "done", pubmed_status="unavailable", publications_fetched=0)
    r = c.get(f"/profiles/{pid}/bootstrap-status")
    assert "PubMed was unavailable" in r.text


def test_failed_invites_manual(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid = _setup(app, pg_session_factory, "failed")
    r = c.get(f"/profiles/{pid}/bootstrap-status")
    assert "Write one below" in r.text
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_web_bootstrap_status.py -v`
Expected: FAIL — 404 (route not defined).

- [ ] **Step 3: Create `src/call_manager/web/templates/partials/bootstrap_status.html`**

```html
{% if job and job.status in ("queued", "running") %}
<div class="banner banner-info" hx-get="/profiles/{{ profile_id }}/bootstrap-status"
     hx-trigger="every 3s" hx-swap="outerHTML">
  <span class="spinner" aria-hidden="true"></span>
  Drafting your profile from PubMed and past grants — this can take a minute on the local model.
</div>
{% elif job and job.status == "done" %}
<div class="banner banner-success" hx-get="/profiles/{{ profile_id }}/bootstrap-status"
     hx-trigger="load" hx-swap="none">
  Draft ready.
  {% if job.pubmed_status == "ok" %}Found {{ job.publications_fetched }} publications for your members.
  {% elif job.pubmed_status == "empty" %}No publications matched your members on PubMed — the draft rests on your mission text and pasted grants.
  {% elif job.pubmed_status == "unavailable" %}PubMed was unavailable, so the draft rests on your mission text and pasted grants. You can edit the description below.
  {% endif %}
</div>
{% elif job and job.status == "failed" %}
<div class="banner banner-warning">
  We couldn't auto-draft a description (the local model was unavailable). Write one below and save.
</div>
{% endif %}
```

- [ ] **Step 4: Add the route to `src/call_manager/web/routes/profiles.py`**

```python
@router.get("/profiles/{profile_id}/bootstrap-status", response_class=HTMLResponse)
def profile_bootstrap_status(
    request: Request,
    profile_id: int,
    user: User = Depends(get_current_user),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        p = session.get(Profile, profile_id)
        if p is None or p.is_active is False:
            raise HTTPException(status_code=404, detail="profile not found")
        if p.created_by != user.id and not user.is_admin:
            raise HTTPException(status_code=403, detail="not authorized")
        job = session.execute(
            select(ProfileBootstrapJob).where(ProfileBootstrapJob.profile_id == profile_id)
        ).scalar_one_or_none()
        session.expunge_all()
    return request.app.state.templates.TemplateResponse(
        request=request, name="partials/bootstrap_status.html",
        context={"job": job, "profile_id": profile_id},
    )
```

- [ ] **Step 5: Embed the fragment in `src/call_manager/web/templates/profile_edit.html`**

At the top of the form body, add the initial poll mount and hide the description while drafting:

```html
<div hx-get="/profiles/{{ profile.id }}/bootstrap-status" hx-trigger="load" hx-swap="outerHTML"></div>
```

Wrap the description field block so it's hidden while a bootstrap job is non-terminal. The simplest server-side approach: pass an `editable` flag from the edit-form GET handler (`profile_edit_form`) — query the active job there and set `editable = job is None or job.status in ('done','failed')`, then `{% if editable %}…description field…{% endif %}`. Update `profile_edit_form` to compute and pass `editable`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_bootstrap_status.py -v`
Expected: PASS.

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check . && uv run ruff format .
git add src/call_manager/web/routes/profiles.py src/call_manager/web/templates/partials/bootstrap_status.html src/call_manager/web/templates/profile_edit.html tests/test_web_bootstrap_status.py
git commit -m "feat(profiles): bootstrap progress + honest completeness banner on the edit page"
```

### Task 8: ranking-status strip on the profile-filtered feed

**Files:**
- Modify: `src/call_manager/web/routes/calls.py` (new `GET /calls/ranking-status` fragment + counts)
- Create: `src/call_manager/web/templates/partials/ranking_status_strip.html`
- Modify: `src/call_manager/web/templates/calls_index.html`
- Test: `tests/test_web_ranking_status_strip.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_ranking_status_strip.py
from fastapi.testclient import TestClient

from call_manager.db.models import RankingJob, RankingJobStatus, Profile, ProfileKind, User
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _client(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="u@ki.se")
        s.add(u)
        s.flush()
        p = Profile(name="Grp", kind=ProfileKind.ORGANIZATIONAL_UNIT.value, level="group",
                    description="d", structured={}, created_by=u.id)
        s.add(p)
        s.commit()
        uid, pid = u.id, p.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, pid


def test_strip_absent_when_no_pending(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid = _client(app, pg_session_factory)
    r = c.get(f"/calls/ranking-status?profile={pid}")
    assert r.status_code == 200
    assert "Ranking" not in r.text  # nothing pending → empty strip


def test_strip_shows_pending_and_failed(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid = _client(app, pg_session_factory)
    with pg_session_factory() as s:
        s.add(RankingJob(call_id=1, profile_id=pid, status=RankingJobStatus.PENDING))
        s.add(RankingJob(call_id=2, profile_id=pid, status=RankingJobStatus.FAILED))
        s.commit()
    r = c.get(f"/calls/ranking-status?profile={pid}")
    assert "Ranking 1 call" in r.text
    assert "couldn't be scored" in r.text
```

> `RankingJob.status` is mapped as the native enum `RankingJobStatus` (values `pending`/`running`/`failed`/`done`). The test and the count helper below use the enum members, not raw strings. (`RankingJob` may also require a non-null `attempts`/`updated_at` — both have server defaults, so the bare constructor above is fine; confirm against `src/call_manager/db/models.py` if the insert errors.)

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_web_ranking_status_strip.py -v`
Expected: FAIL — 404 (route not defined).

- [ ] **Step 3: Create `src/call_manager/web/templates/partials/ranking_status_strip.html`**

```html
{% if pending > 0 or failed > 0 %}
<div class="ranking-status-strip" {% if pending > 0 %}hx-get="/calls/ranking-status?profile={{ profile_id }}"
     hx-trigger="every 5s" hx-swap="outerHTML"{% endif %}>
  {% if pending > 0 %}Ranking {{ pending }} call{{ "s" if pending != 1 else "" }} for this profile…{% endif %}
  {% if failed > 0 %}<span class="ranking-status-failed">{{ failed }} call{{ "s" if failed != 1 else "" }} couldn't be scored automatically.</span>{% endif %}
</div>
{% else %}
<div class="ranking-status-strip"></div>
{% endif %}
```

- [ ] **Step 4: Add the fragment route + a count helper in `src/call_manager/web/routes/calls.py`**

```python
from call_manager.db.models import RankingJob, RankingJobStatus  # add to imports


def _ranking_counts(session, profile_id: int) -> tuple[int, int]:
    pending = session.scalar(
        select(func.count()).select_from(RankingJob).where(
            RankingJob.profile_id == profile_id,
            RankingJob.status.in_((RankingJobStatus.PENDING, RankingJobStatus.RUNNING)),
        )
    ) or 0
    failed = session.scalar(
        select(func.count()).select_from(RankingJob).where(
            RankingJob.profile_id == profile_id, RankingJob.status == RankingJobStatus.FAILED,
        )
    ) or 0
    return pending, failed


@router.get("/calls/ranking-status", response_class=HTMLResponse)
def ranking_status(
    request: Request,
    profile: int = Query(...),
    user: User = Depends(get_current_user),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        pending, failed = _ranking_counts(session, profile)
    return request.app.state.templates.TemplateResponse(
        request=request, name="partials/ranking_status_strip.html",
        context={"pending": pending, "failed": failed, "profile_id": profile},
    )
```

Ensure `func` is imported (`from sqlalchemy import func, select, ...`).

- [ ] **Step 5: Mount the strip on the profile-filtered feed in `src/call_manager/web/templates/calls_index.html`**

Just above the call-feed list, when a profile is selected:

```html
{% if selected_profile_id %}
<div hx-get="/calls/ranking-status?profile={{ selected_profile_id }}" hx-trigger="load" hx-swap="outerHTML"></div>
{% endif %}
```

(Use whatever variable the template already exposes for the selected profile id — confirm the name in `calls_index.html`.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_ranking_status_strip.py -v`
Expected: PASS.

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check . && uv run ruff format .
git add src/call_manager/web/routes/calls.py src/call_manager/web/templates/partials/ranking_status_strip.html src/call_manager/web/templates/calls_index.html tests/test_web_ranking_status_strip.py
git commit -m "feat(calls): ranking-status strip (polls, shows pending + failed) on the profile feed"
```

---

## Phase 5 — Sticky default profile

### Task 9: per-user default profile resolution in `calls_index`

**Files:**
- Modify: `src/call_manager/web/routes/calls.py` (`calls_index`)
- Test: `tests/test_web_calls_sticky_default.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_calls_sticky_default.py
from fastapi.testclient import TestClient

from call_manager.db.models import Profile, ProfileKind, User
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _client(app, pg_session_factory, default_profile_id=None):
    with pg_session_factory() as s:
        u = User(email="u@ki.se")
        s.add(u)
        s.flush()
        p = Profile(name="Grp", kind=ProfileKind.ORGANIZATIONAL_UNIT.value, level="group",
                    description="d", structured={}, created_by=u.id)
        s.add(p)
        s.flush()
        if default_profile_id == "self":
            u.default_profile_id = p.id
        s.commit()
        uid, pid = u.id, p.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid, pid


def test_selecting_a_profile_persists_it_as_default(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _client(app, pg_session_factory)
    c.get(f"/?profile={pid}")
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id == pid


def test_bare_root_uses_stored_default(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _client(app, pg_session_factory, default_profile_id="self")
    r = c.get("/")
    # The selected-profile id should round-trip into the page (dropdown selected, etc.)
    assert str(pid) in r.text


def test_explicit_empty_clears_default(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _client(app, pg_session_factory, default_profile_id="self")
    c.get("/?profile=")
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_web_calls_sticky_default.py -v`
Expected: FAIL — selection isn't persisted; bare `/` doesn't use the default.

- [ ] **Step 3: Implement three-way resolution in `calls_index` (`src/call_manager/web/routes/calls.py`)**

At the top of `calls_index`, the parameter is `profile: str | None = Query(default=None)`. Distinguish absent (`None`) from explicit-empty (`""`) **before** `_parse_optional_profile_id` collapses them. Replace the opening resolution:

```python
    raw_profile = profile  # None = absent; "" = explicit "no profile"
    profile_id = _parse_optional_profile_id(profile)

    session_factory = request.app.state.session_factory
    with session_factory() as session:
        # Resolve sticky default before validating, then persist/clear.
        if raw_profile is None:
            # Absent → fall back to the user's stored default (if still active).
            stored = session.scalar(select(User.default_profile_id).where(User.id == user.id))
            if stored is not None:
                active = session.scalar(
                    select(Profile.id).where(Profile.id == stored, Profile.is_active.is_(True))
                )
                profile_id = active  # None if the stored default is inactive → unranked
        elif raw_profile == "":
            # Explicit "— No profile —" → clear the stored default.
            u = session.get(User, user.id)
            if u is not None and u.default_profile_id is not None:
                u.default_profile_id = None
                session.commit()
            profile_id = None
        elif profile_id is not None:
            # Valid selection → persist as default only when it changed.
            u = session.get(User, user.id)
            if u is not None and u.default_profile_id != profile_id:
                u.default_profile_id = profile_id
                session.commit()
        profile = profile_id  # type: ignore[assignment]  # rest of handler uses int
```

Keep the rest of the handler (the existing `profile_obj` validation at lines ~88-99 that degrades unknown/inactive ids) unchanged — it still guards a stale default that became inactive between resolution and use.

> The `_now`/`offset` locals computed before the old `with` block move inside or stay above as-is; ensure no duplicate session is opened — fold this resolution into the existing single `with session_factory() as session:` block rather than opening a second one.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_calls_sticky_default.py tests/test_web_calls.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check . && uv run ruff format .
git add src/call_manager/web/routes/calls.py tests/test_web_calls_sticky_default.py
git commit -m "feat(calls): per-user sticky default profile (seed on create, persist on select, clear on empty)"
```

---

## Final verification

- [ ] **Run the full suite + lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all green. Investigate any failure in `test_web_profiles.py`, `test_profiles_e2e.py`, or `test_scheduler.py` — those are the modules most likely to assert the old synchronous-create behavior or the scheduler job set; update their expectations to match the async create + the new `profile_bootstrap_tick` job.

- [ ] **Manual smoke (optional, needs a running app + Ollama)**

Create a profile via the UI → confirm immediate redirect to the edit page with the "Drafting…" banner → after a tick, the banner flips to the completeness line and the description appears → the dashboard shows the ranking-status strip for the new profile and lands on it by default on the next bare-`/` visit.

---

## Self-Review (completed by the plan author)

**Spec coverage:**
- Component 1 (async bootstrap, atomic create, outcome-aware worker) → Tasks 2, 3, 4, 5. ✓
- A2 edit-blocking + single backfill → Tasks 5, 6. ✓
- Component 2 (progress + honest banner, `pubmed_status` wording) → Tasks 2, 7. ✓
- Component 3 (silent creator auto-subscribe, in the atomic tx) → Task 4. ✓
- Component 4 (sticky default, seed-on-create fill-if-null, three-way resolution) → Tasks 4, 9. ✓
- Component 5 (status strip: polls, pending+failed, no ETA, no "admin notified") → Task 8. ✓
- Data model (`profile_bootstrap_jobs`, `users.default_profile_id`, seed caps, retention/seed-clear, migration 0046) → Tasks 1, 4, 5. ✓
- Failure handling (LLM fail no backfill, PubMed degraded success, stuck reclaim, blank-desc guard, atomic rollback) → Tasks 5, 6, 4. ✓

**Placeholder scan:** no TBD/TODO; every code step shows concrete code. Two explicit "confirm the field/var name against the file" notes (RankingJob fields in Task 8; selected-profile template var in Task 8) — these are verification prompts, not placeholders, because the surrounding code is complete.

**Type consistency:** `ProfileBootstrapJob` fields, `DraftProfile(..., llm_succeeded, pubmed_status)`, `create_profile_with_bootstrap(...)` signature, `merge_bootstrap_into_profile(profile, draft)`, `run_profile_bootstrap_tick(*, session_factory, pubmed, ollama, embed_client)`, and `_ranking_counts(session, profile_id)` are used identically across the tasks that define and call them.

**Known cross-plan dependency:** migration `0046`/`down_revision="0045"` requires ranking-v2.1's `0042–0045` to be merged first (stated in the header and Task 1).
