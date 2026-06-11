# First-Run Value Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make call-manager deliver its core promise by default — a newly created profile is bootstrapped asynchronously without freezing the browser, gets ranked on its own with visible progress, subscribes its creator to the digest, and becomes the dashboard's default — without touching ranking-model internals.

**Architecture:** A DB-backed `profile_bootstrap_jobs` queue drained by a scheduler tick (mirroring `WorkspaceSetupJob`/`run_phase0_tick`). Profile + job + subscription + default are inserted in one atomic transaction (mirroring `create_workspace`). The profile is read-only while its bootstrap job is non-terminal, so the worker is the sole writer and owns the single embed+backfill. The worker wraps all post-claim work in try/except so failures transition the job immediately. Bootstrap success/failure is detected via explicit outcome fields on `DraftProfile`. A per-user `users.default_profile_id` makes the ranked view sticky.

**Tech Stack:** Python 3.12, FastAPI + Jinja2 + HTMX, SQLAlchemy 2.x + Alembic, APScheduler, PostgreSQL 16, pytest (+ testcontainers Postgres).

**Spec:** [docs/superpowers/specs/2026-06-11-first-run-value-loop-design.md](../specs/2026-06-11-first-run-value-loop-design.md) (revised through R2).

**MIGRATION NUMBERING:** ranking-v2.1 has now **merged to master** (commit `7951e36`); the current Alembic head is `0045`. This plan's migration is therefore a clean **linear** revision: file `0046_first_run_value_loop.py` with `down_revision = "0045"`. Single head, no `alembic merge` needed. (The earlier Codex-R2 branch-off-`0041` workaround is obsolete now that `0042–0045` exist on disk.)

**Test commands:** `uv run pytest <path> -v`. Lint/format: `uv run ruff check . && uv run ruff format .`.

---

## File Structure

**Created:**
- `src/call_manager/profiles/create.py` — `merge_bootstrap_into_profile()` + `create_profile_with_bootstrap()` (Tasks 3, 4).
- `src/call_manager/profiles/bootstrap_worker.py` — worker + stuck-job reaper (Task 5).
- `alembic/versions/0046_first_run_value_loop.py` — schema migration (Task 1).
- `src/call_manager/web/templates/partials/bootstrap_banner.html` — bootstrap progress/completeness banner (Task 7).
- `src/call_manager/web/templates/partials/ranking_status_strip.html` — ranking-status strip (Task 8).
- Tests: `tests/test_migration_0046_first_run_value_loop.py`, `tests/test_profiles_bootstrap_outcome.py`, `tests/test_profiles_create_service.py`, `tests/test_web_profiles_new_async.py`, `tests/test_profiles_bootstrap_worker.py`, `tests/test_web_profiles_edit_guards.py`, `tests/test_web_bootstrap_status.py`, `tests/test_web_ranking_status_strip.py`, `tests/test_web_calls_sticky_default.py`.

**Modified:**
- `src/call_manager/db/models.py` — `ProfileBootstrapJob` + `User.default_profile_id` (Task 1).
- `src/call_manager/profiles/seed.py`, `bootstrap.py` — outcome fields (Task 2).
- `src/call_manager/web/routes/profiles.py` — async create + seed caps + edit guards + bootstrap-status route + editable flag (Tasks 4, 6, 7).
- `src/call_manager/scheduler.py`, `config.py` — bootstrap tick (Task 5).
- `src/call_manager/web/routes/calls.py` — ranking-status route + sticky default (Tasks 8, 9).
- `src/call_manager/web/templates/profile_edit.html`, `calls_index.html` — embed the fragments (Tasks 7, 8).

---

## Phase 1 — Schema foundation (migration 0046)

### Task 1: `ProfileBootstrapJob` model + `users.default_profile_id` + migration 0046

**Files:**
- Modify: `src/call_manager/db/models.py`
- Create: `alembic/versions/0046_first_run_value_loop.py`
- Test: `tests/test_migration_0046_first_run_value_loop.py`

- [ ] **Step 1: Write the failing migration round-trip test**

```python
# tests/test_migration_0046_first_run_value_loop.py
import uuid

from sqlalchemy import inspect, text

from call_manager.db.models import ProfileBootstrapJob, Profile, ProfileKind, User


def _mk_profile(session):
    u = User(email="creator@ki.se")
    session.add(u)
    session.flush()
    p = Profile(name="Grp", kind=ProfileKind.ORGANIZATIONAL_UNIT.value, level="group",
                description="", structured={}, created_by=u.id)
    session.add(p)
    session.flush()
    return p.id, u.id


def test_bootstrap_table_and_default_column_exist(pg_session_factory):
    with pg_session_factory() as s:
        insp = inspect(s.bind)
        assert "profile_bootstrap_jobs" in insp.get_table_names()
        assert "default_profile_id" in {c["name"] for c in insp.get_columns("users")}


def test_bootstrap_job_unique_per_profile(pg_session_factory):
    with pg_session_factory() as s:
        pid, _ = _mk_profile(s)
        s.add(ProfileBootstrapJob(profile_id=pid, status="queued", seed={"name": "Grp"}))
        s.commit()
        dup = s.execute(
            text("INSERT INTO profile_bootstrap_jobs (id, profile_id, status) "
                 "VALUES (:i, :p, 'queued') ON CONFLICT DO NOTHING RETURNING id"),
            {"i": str(uuid.uuid4()), "p": pid},
        ).first()
        assert dup is None


def test_default_profile_id_set_null_on_profile_delete(pg_session_factory):
    with pg_session_factory() as s:
        pid, uid = _mk_profile(s)
        s.get(User, uid).default_profile_id = pid
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

Add to `User` (near the other columns; `BigInteger` matches `Profile.id`):

```python
    default_profile_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True
    )
```

Add near `WorkspaceSetupJob` (reuse the already-imported `JSONB`, `UUID`, `BigInteger`, `SmallInteger`, `Text`, `Integer`, `DateTime`, `func`, `text`, `ForeignKey`, `CheckConstraint`, `UniqueConstraint`):

```python
class ProfileBootstrapJob(Base):
    __tablename__ = "profile_bootstrap_jobs"
    __table_args__ = (
        UniqueConstraint("profile_id", name="uq_profile_bootstrap_jobs_profile"),
        CheckConstraint("status IN ('queued','running','done','failed')",
                        name="ck_profile_bootstrap_jobs_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    seed: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # CLEARED on terminal completion
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
"""First-run value loop: profile_bootstrap_jobs + users.default_profile_id.

Revision ID: 0046
Revises: 0045
Create Date: 2026-06-11

Linear revision after ranking-v2.1 (0045) merged to master.
See docs/superpowers/plans/2026-06-11-first-run-value-loop.md Task 1.
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
        sa.CheckConstraint("status IN ('queued','running','done','failed')",
                           name="ck_profile_bootstrap_jobs_status"),
    )
    op.add_column("users", sa.Column("default_profile_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key("fk_users_default_profile_id", "users", "profiles",
                          ["default_profile_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint("fk_users_default_profile_id", "users", type_="foreignkey")
    op.drop_column("users", "default_profile_id")
    op.drop_table("profile_bootstrap_jobs")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_migration_0046_first_run_value_loop.py -v`
Expected: PASS. (If it fails on `alembic upgrade head` with "Can't locate revision 0045", confirm the head: `uv run alembic heads` should show `0045`.)

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check . && uv run ruff format .
git add src/call_manager/db/models.py alembic/versions/0046_first_run_value_loop.py tests/test_migration_0046_first_run_value_loop.py
git commit -m "feat(profiles): schema for first-run value loop (bootstrap jobs + default profile)"
```

---

## Phase 2 — Bootstrap outcome detection

### Task 2: explicit `llm_succeeded` (non-blank) + `pubmed_status` on `DraftProfile`

**Files:**
- Modify: `src/call_manager/profiles/seed.py`, `src/call_manager/profiles/bootstrap.py`
- Test: `tests/test_profiles_bootstrap_outcome.py`

- [ ] **Step 1: Write the failing test (includes the parseable-but-empty case — Codex R2 #2)**

```python
# tests/test_profiles_bootstrap_outcome.py
from call_manager.profiles.bootstrap import bootstrap_profile
from call_manager.profiles.pubmed import PubMedError
from call_manager.profiles.seed import Member, SeedInput
from call_manager.ranking.ollama_client import LLMError


class _Pub:
    def __init__(self, n): self._n = n
    def fetch_publications(self, members): return [object()] * self._n


class _PubFail:
    def fetch_publications(self, members): raise PubMedError("down")


def _ollama(content):
    class _O:
        def chat(self, *, messages): return {"choices": [{"message": {"content": content}}]}
    return _O()


class _LLMFail:
    def chat(self, *, messages): raise LLMError("model down")


def _seed(members):
    return SeedInput(name="G", kind="organizational_unit", members=members, mission_text="x")


_GOOD = '{"description": "A solid two-paragraph draft.", "structured": {}}'


def test_llm_success_with_pubs_is_ok():
    d = bootstrap_profile(seed=_seed([Member(orcid="0000-0002-1825-0097")]),
                          pubmed=_Pub(3), ollama=_ollama(_GOOD))
    assert d.llm_succeeded is True
    assert d.pubmed_status == "ok"
    assert d.publications_fetched == 3


def test_pubmed_empty_unavailable_not_queried():
    m = [Member(orcid="0000-0002-1825-0097")]
    assert bootstrap_profile(seed=_seed(m), pubmed=_Pub(0), ollama=_ollama(_GOOD)).pubmed_status == "empty"
    assert bootstrap_profile(seed=_seed(m), pubmed=_PubFail(), ollama=_ollama(_GOOD)).pubmed_status == "unavailable"
    assert bootstrap_profile(seed=_seed([]), pubmed=_Pub(0), ollama=_ollama(_GOOD)).pubmed_status == "not_queried"


def test_llm_exception_is_failure():
    d = bootstrap_profile(seed=_seed([]), pubmed=_Pub(0), ollama=_LLMFail())
    assert d.llm_succeeded is False


def test_parseable_but_blank_description_is_failure():
    # Parseable JSON with an empty description must NOT count as success.
    d = bootstrap_profile(seed=_seed([]), pubmed=_Pub(0),
                          ollama=_ollama('{"description": "", "structured": {}}'))
    assert d.llm_succeeded is False
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_profiles_bootstrap_outcome.py -v`
Expected: FAIL — `AttributeError: ... 'llm_succeeded'`.

- [ ] **Step 3: Add fields to `DraftProfile` in `src/call_manager/profiles/seed.py`**

```python
    # Explicit outcomes (bootstrap never raises). Worker branches on these.
    llm_succeeded: bool = True
    pubmed_status: str = "ok"  # 'ok' | 'empty' | 'unavailable' | 'not_queried'
```

- [ ] **Step 4: Set the outcomes in `src/call_manager/profiles/bootstrap.py`**

Replace the PubMed block to record an explicit status:

```python
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
            _logger.warning("profiles.bootstrap.pubmed_failed",
                            extra={"event": "bootstrap_pubmed_failed", "error": str(e)})
    if publications:
        publications_skipped = 0
    elif seed.members:
        publications_skipped = len(seed.members)
```

In the LLM block, success requires a parseable **non-blank** description:

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
            llm_succeeded = bool(description and description.strip())
    except (LLMError, BootstrapParseError, KeyError, IndexError, TypeError, AttributeError) as e:
        _logger.warning("profiles.bootstrap.llm_failed",
                        extra={"event": "bootstrap_llm_failed", "error": str(e)})
```

Update the `return`:

```python
    return DraftProfile(
        description=description, structured=structured,
        publications_fetched=len(publications), publications_skipped=publications_skipped,
        llm_succeeded=llm_succeeded, pubmed_status=pubmed_status,
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_profiles_bootstrap_outcome.py tests/test_profiles_bootstrap.py -v`
Expected: PASS.

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check . && uv run ruff format .
git add src/call_manager/profiles/seed.py src/call_manager/profiles/bootstrap.py tests/test_profiles_bootstrap_outcome.py
git commit -m "feat(profiles): explicit bootstrap outcome (non-blank llm_succeeded, pubmed_status)"
```

---

## Phase 3 — Atomic create + worker + edit guards

### Task 3: bootstrap-merge helper (carries rubric — Codex R2 #10)

**Files:**
- Create: `src/call_manager/profiles/create.py`
- Test: `tests/test_profiles_create_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_profiles_create_service.py
from call_manager.profiles.create import merge_bootstrap_into_profile
from call_manager.profiles.seed import DraftProfile


class _P:
    def __init__(self, kind, description, structured):
        self.kind, self.description, self.structured = kind, description, structured


def test_merge_fills_empty_and_keeps_user_values():
    p = _P("organizational_unit", "", {"areas": ["oncology"], "must_include": ["x"]})
    draft = DraftProfile(
        description="Drafted.",
        structured={"areas": ["genomics"], "must_include": ["y"], "budget_min": 100000,
                    "trl_max": 7, "hard_exclusions": ["defence"],
                    "eligibility": {"career_stage": "senior"}},
        publications_fetched=2, publications_skipped=0)
    merge_bootstrap_into_profile(p, draft)
    assert p.description == "Drafted."
    assert p.structured["areas"] == ["oncology"]            # user wins
    assert p.structured["must_include"] == ["x"]            # user wins
    assert p.structured["budget_min"] == 100000            # filled from draft
    assert p.structured["trl_max"] == 7                    # filled from draft
    assert p.structured["hard_exclusions"] == ["defence"]  # filled from draft
    assert p.structured["eligibility"]["career_stage"] == "senior"


def test_merge_does_not_overwrite_nonempty_description():
    p = _P("organizational_unit", "User wrote this.", {"areas": []})
    draft = DraftProfile(description="LLM draft.", structured={"areas": ["x"]},
                         publications_fetched=0, publications_skipped=0)
    merge_bootstrap_into_profile(p, draft)
    assert p.description == "User wrote this."
    assert p.structured["areas"] == ["x"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_profiles_create_service.py -v`
Expected: FAIL — `ModuleNotFoundError: ... 'call_manager.profiles.create'`.

- [ ] **Step 3: Create `src/call_manager/profiles/create.py`**

```python
from __future__ import annotations

from call_manager.profiles.fields import MAX_LIST_ENTRIES, MAX_TAG_LEN, sanitize_structured
from call_manager.profiles.seed import DraftProfile

_NAMESPACES = ("eligibility", "org", "funding", "geo")
# Rubric fields the LLM suggests but the create form does NOT collect — without
# these the worker would lose the pre-fill the old synchronous path provided.
_RUBRIC_SCALARS = ("budget_min", "budget_max", "trl_min", "trl_max")
_RUBRIC_LISTS = ("must_include", "hard_exclusions")


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

    # Rubric scalars/lists (budget/TRL/must_include/hard_exclusions): fill where blank.
    for key in _RUBRIC_SCALARS:
        if current.get(key) in (None, "") and draft.structured.get(key) is not None:
            current[key] = draft.structured.get(key)
    for key in _RUBRIC_LISTS:
        if not current.get(key) and draft.structured.get(key):
            current[key] = [str(v) for v in draft.structured.get(key, [])][:MAX_LIST_ENTRIES]

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
git commit -m "feat(profiles): non-destructive bootstrap-merge helper (carries rubric suggestions)"
```

### Task 4: atomic create service + async route + seed-cap rejection

**Files:**
- Modify: `src/call_manager/profiles/create.py`, `src/call_manager/web/routes/profiles.py`
- Test: `tests/test_profiles_create_service.py`, `tests/test_web_profiles_new_async.py`

- [ ] **Step 1: Write the failing tests (atomic success, atomic rollback, preserve-existing-default — Codex R2 added coverage)**

```python
# tests/test_profiles_create_service.py  (append)
import pytest

from call_manager.db.models import ProfileBootstrapJob, Profile, Subscription, User


def test_create_atomic_success(pg_session_factory):
    from call_manager.profiles.create import create_profile_with_bootstrap
    with pg_session_factory() as s:
        u = User(email="c@ki.se")
        s.add(u)
        s.flush()
        p = create_profile_with_bootstrap(
            session=s, owner_id=u.id, name="Grp", kind="organizational_unit", level="group",
            structured={"areas": ["oncology"]},
            seed_payload={"name": "Grp", "members": [], "mission_text": "m"})
        s.commit()
        pid, uid = p.id, u.id
    with pg_session_factory() as s:
        assert s.get(Profile, pid).description == ""
        assert s.query(ProfileBootstrapJob).filter_by(profile_id=pid, status="queued").count() == 1
        assert s.get(Subscription, (uid, pid)) is not None
        assert s.get(User, uid).default_profile_id == pid


def test_create_rolls_back_on_failure(pg_session_factory):
    from call_manager.profiles.create import create_profile_with_bootstrap
    with pg_session_factory() as s:
        u = User(email="c2@ki.se")
        s.add(u)
        s.flush()
        uid = u.id
        # owner_id that doesn't exist → Subscription/default insert violates FK on commit.
        with pytest.raises(Exception):
            create_profile_with_bootstrap(
                session=s, owner_id=10_000_000, name="Bad", kind="organizational_unit",
                level="group", structured={}, seed_payload={"name": "Bad"})
            s.commit()
        s.rollback()
    with pg_session_factory() as s:
        assert s.query(Profile).filter_by(name="Bad").count() == 0


def test_existing_default_is_preserved(pg_session_factory):
    from call_manager.profiles.create import create_profile_with_bootstrap
    with pg_session_factory() as s:
        u = User(email="c3@ki.se")
        s.add(u)
        s.flush()
        first = create_profile_with_bootstrap(session=s, owner_id=u.id, name="First",
            kind="organizational_unit", level="group", structured={}, seed_payload={"name": "First"})
        s.commit()
        first_id, uid = first.id, u.id
    with pg_session_factory() as s:
        u = s.get(User, uid)
        create_profile_with_bootstrap(session=s, owner_id=uid, name="Second",
            kind="organizational_unit", level="group", structured={}, seed_payload={"name": "Second"})
        s.commit()
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id == first_id  # fill-if-null: not changed
```

```python
# tests/test_web_profiles_new_async.py
from fastapi.testclient import TestClient

from call_manager.db.models import ProfileBootstrapJob, Profile, Subscription, User
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    for k, v in {"DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
                 "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver"}.items():
        monkeypatch.setenv(k, v)
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


def test_create_does_no_llm_in_request_and_enqueues(monkeypatch, pg_url, pg_session_factory):
    # The create route must not call bootstrap at all — fail loudly if it does.
    def _boom(**_):
        raise AssertionError("bootstrap_profile must not run in the request")
    monkeypatch.setattr("call_manager.profiles.bootstrap.bootstrap_profile", _boom, raising=False)

    app = _app(monkeypatch, pg_url)
    c, uid = _client(app, pg_session_factory)
    r = c.post("/profiles/new", data={"name": "Grp", "kind": "organizational_unit",
                                      "level": "group", "mission_text": "m"}, follow_redirects=False)
    assert r.status_code == 303 and "/edit" in r.headers["location"]
    with pg_session_factory() as s:
        p = s.query(Profile).filter_by(name="Grp").one()
        assert p.description == ""
        assert s.query(ProfileBootstrapJob).filter_by(profile_id=p.id, status="queued").count() == 1
        assert s.get(Subscription, (uid, p.id)) is not None
        assert s.get(User, uid).default_profile_id == p.id


def test_rejects_oversized_mission(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, _ = _client(app, pg_session_factory)
    assert c.post("/profiles/new", data={"name": "Grp", "mission_text": "x" * 10_241}).status_code == 413


def test_rejects_too_many_members(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, _ = _client(app, pg_session_factory)
    many = "\n".join(f"0000-0002-1825-009{i % 10}" for i in range(51))
    r = c.post("/profiles/new", data={"name": "Grp", "orcids": many})
    assert r.status_code == 400
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_profiles_create_service.py tests/test_web_profiles_new_async.py -v`
Expected: FAIL — `ImportError: ... 'create_profile_with_bootstrap'`.

- [ ] **Step 3: Add the service to `src/call_manager/profiles/create.py`**

```python
from call_manager.db.models import Profile, ProfileBootstrapJob, Subscription, User


def create_profile_with_bootstrap(
    *, session, owner_id: int, name: str, kind: str, level: str | None,
    structured: dict, seed_payload: dict,
) -> Profile:
    """Atomic: insert the profile (empty description), enqueue its bootstrap
    job, subscribe the creator, and seed their default profile if unset.
    Mirrors create_workspace — the CALLER commits, so any failure rolls all of
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

- [ ] **Step 4: Add seed-cap REJECTION to `_parse_members` callers in `src/call_manager/web/routes/profiles.py`**

Add a validating parser used by the create route (reject, do not truncate — Codex R2 #9). Add near `_parse_members`:

```python
_MAX_MEMBERS = 50
_MAX_SEED_LINE = 500


def _parse_members_strict(orcids: str, names_affiliations: str) -> list[Member]:
    raw_lines = [ln.strip() for ln in (orcids.splitlines() + names_affiliations.splitlines()) if ln.strip()]
    if len(raw_lines) > _MAX_MEMBERS:
        raise HTTPException(status_code=400, detail=f"too many members (max {_MAX_MEMBERS})")
    if any(len(ln) > _MAX_SEED_LINE for ln in raw_lines):
        raise HTTPException(status_code=400, detail=f"member line too long (max {_MAX_SEED_LINE} chars)")
    return _parse_members(orcids, names_affiliations)
```

- [ ] **Step 5: Rewrite `POST /profiles/new` in `src/call_manager/web/routes/profiles.py`**

Add the mission cap near the other caps at the top of the handler:

```python
    if len(mission_text) > 10_240:
        raise HTTPException(status_code=413, detail="mission text too large (max 10 KB)")
```

Replace the body from `members = _parse_members(...)` through the `return`:

```python
    members = _parse_members_strict(orcids, names_affiliations)
    seed_payload = {
        "name": name, "kind": kind, "level": level_val,
        "members": [{"orcid": m.orcid, "name": m.name, "affiliation": m.affiliation} for m in members],
        "pasted_grants": pasted_grants, "mission_text": mission_text,
    }
    try:
        with session_factory() as session:
            profile = create_profile_with_bootstrap(
                session=session, owner_id=user.id, name=name, kind=kind,
                level=level_val, structured={**user_structured}, seed_payload=seed_payload)
            session.commit()
            session.refresh(profile)
            new_id = profile.id
    except IntegrityError as e:
        raise HTTPException(status_code=409, detail="profile name already exists") from e

    return RedirectResponse(url=f"/profiles/{new_id}/edit", status_code=303)
```

Add `from call_manager.profiles.create import create_profile_with_bootstrap`. Drop the `background: BackgroundTasks` param from `profile_new_submit` and the now-unused inline-bootstrap imports **only if** ruff flags them and the edit handler doesn't use them (keep `backfill_profile`/`refresh_profile_embedding` — the edit path needs them).

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_profiles_create_service.py tests/test_web_profiles_new_async.py tests/test_web_profiles.py -v`
Expected: PASS. (Update any `test_web_profiles.py` test that asserted synchronous-bootstrap-on-create to the new enqueue behavior.)

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check . && uv run ruff format .
git add src/call_manager/profiles/create.py src/call_manager/web/routes/profiles.py tests/test_profiles_create_service.py tests/test_web_profiles_new_async.py
git commit -m "feat(profiles): atomic async profile creation + auto-subscribe + default + seed-cap rejection"
```

### Task 5: bootstrap worker (try/except retry, embed-before-backfill) + reaper + scheduler wiring

**Files:**
- Create: `src/call_manager/profiles/bootstrap_worker.py`
- Modify: `src/call_manager/config.py`, `src/call_manager/scheduler.py`
- Test: `tests/test_profiles_bootstrap_worker.py`

- [ ] **Step 1: Write the failing tests (success+order, llm-fail, backfill-fail retry→failed, degraded pubmed, reaper, scheduler registration — Codex R2 #3 + added coverage)**

```python
# tests/test_profiles_bootstrap_worker.py
from datetime import UTC, datetime, timedelta

from call_manager.db.models import ProfileBootstrapJob, Profile, ProfileKind, User
from call_manager.profiles import bootstrap_worker as bw
from call_manager.profiles.bootstrap_worker import (
    requeue_stuck_bootstrap_jobs, run_profile_bootstrap_tick,
)
from call_manager.profiles.seed import DraftProfile


class _Embed:
    model = "fake-embed"
    def embed(self, text): return [0.0] * 8


def _enqueue(sf, *, name="Grp"):
    with sf() as s:
        u = User(email=f"{name}@ki.se")
        s.add(u)
        s.flush()
        p = Profile(name=name, kind=ProfileKind.ORGANIZATIONAL_UNIT.value, level="group",
                    description="", structured={"areas": []}, created_by=u.id)
        s.add(p)
        s.flush()
        s.add(ProfileBootstrapJob(profile_id=p.id, status="queued",
                                  seed={"name": name, "kind": "organizational_unit", "members": []}))
        s.commit()
        return p.id


def _patch_draft(monkeypatch, *, llm=True, pubmed="not_queried"):
    monkeypatch.setattr(bw, "bootstrap_profile",
                        lambda **_: DraftProfile("Drafted." if llm else "", {"areas": []}, 0, 0,
                                                 llm_succeeded=llm, pubmed_status=pubmed))


def test_success_embeds_before_backfill_and_clears_seed(monkeypatch, pg_session_factory):
    pid = _enqueue(pg_session_factory)
    order = []
    _patch_draft(monkeypatch, llm=True, pubmed="ok")
    monkeypatch.setattr(bw, "refresh_profile_embedding", lambda *a, **k: order.append("embed"))
    monkeypatch.setattr(bw, "backfill_profile", lambda **k: order.append("backfill") or 0)
    run_profile_bootstrap_tick(session_factory=pg_session_factory, pubmed=object(),
                               ollama=object(), embed_client=_Embed())
    with pg_session_factory() as s:
        job = s.query(ProfileBootstrapJob).filter_by(profile_id=pid).one()
        assert s.get(Profile, pid).description == "Drafted."
        assert job.status == "done" and job.pubmed_status == "ok" and job.seed is None
    assert order == ["embed", "backfill"]


def test_degraded_pubmed_still_succeeds(monkeypatch, pg_session_factory):
    pid = _enqueue(pg_session_factory)
    _patch_draft(monkeypatch, llm=True, pubmed="unavailable")
    monkeypatch.setattr(bw, "refresh_profile_embedding", lambda *a, **k: None)
    calls = {"n": 0}
    monkeypatch.setattr(bw, "backfill_profile", lambda **k: calls.__setitem__("n", 1) or 0)
    run_profile_bootstrap_tick(session_factory=pg_session_factory, pubmed=object(),
                               ollama=object(), embed_client=_Embed())
    with pg_session_factory() as s:
        job = s.query(ProfileBootstrapJob).filter_by(profile_id=pid).one()
        assert job.status == "done" and job.pubmed_status == "unavailable"
    assert calls["n"] == 1


def test_llm_failure_exhausts_to_failed_no_backfill(monkeypatch, pg_session_factory):
    pid = _enqueue(pg_session_factory)
    _patch_draft(monkeypatch, llm=False)
    monkeypatch.setattr(bw, "refresh_profile_embedding", lambda *a, **k: None)
    called = {"n": 0}
    monkeypatch.setattr(bw, "backfill_profile", lambda **k: called.__setitem__("n", 1) or 0)
    for _ in range(bw.MAX_BOOTSTRAP_ATTEMPTS):
        run_profile_bootstrap_tick(session_factory=pg_session_factory, pubmed=object(),
                                   ollama=object(), embed_client=_Embed())
    with pg_session_factory() as s:
        assert s.query(ProfileBootstrapJob).filter_by(profile_id=pid).one().status == "failed"
    assert called["n"] == 0


def test_backfill_exception_transitions_job_not_stuck_running(monkeypatch, pg_session_factory):
    pid = _enqueue(pg_session_factory)
    _patch_draft(monkeypatch, llm=True, pubmed="ok")
    monkeypatch.setattr(bw, "refresh_profile_embedding", lambda *a, **k: None)
    def _raise(**k): raise RuntimeError("backfill boom")
    monkeypatch.setattr(bw, "backfill_profile", _raise)
    run_profile_bootstrap_tick(session_factory=pg_session_factory, pubmed=object(),
                               ollama=object(), embed_client=_Embed())
    with pg_session_factory() as s:
        job = s.query(ProfileBootstrapJob).filter_by(profile_id=pid).one()
        assert job.status == "queued"          # not stuck 'running'
        assert "backfill boom" in (job.last_error or "")


def test_reaper_requeues_then_dead_letters(pg_session_factory):
    pid = _enqueue(pg_session_factory)
    stale = datetime.now(UTC) - bw.STUCK_BOOTSTRAP_AFTER - timedelta(minutes=1)
    with pg_session_factory() as s:
        job = s.query(ProfileBootstrapJob).filter_by(profile_id=pid).one()
        job.status, job.started_at, job.attempts = "running", stale, 2
        s.commit()
    with pg_session_factory() as s:
        requeue_stuck_bootstrap_jobs(s)
        s.commit()
    with pg_session_factory() as s:
        job = s.query(ProfileBootstrapJob).filter_by(profile_id=pid).one()
        assert job.status == "queued" and job.started_at is None and job.attempts == 2
    with pg_session_factory() as s:
        job = s.query(ProfileBootstrapJob).filter_by(profile_id=pid).one()
        job.status, job.started_at, job.attempts = "running", stale, bw.MAX_BOOTSTRAP_ATTEMPTS
        s.commit()
    with pg_session_factory() as s:
        requeue_stuck_bootstrap_jobs(s)
        s.commit()
    with pg_session_factory() as s:
        assert s.query(ProfileBootstrapJob).filter_by(profile_id=pid).one().status == "failed"


def test_scheduler_registers_bootstrap_tick(monkeypatch):
    from unittest.mock import MagicMock
    from apscheduler.schedulers.background import BackgroundScheduler  # noqa: F401
    from call_manager.config import Settings
    from call_manager.proposal_assist.drafting.factory import build_workspace_controller
    from call_manager.scheduler import build_scheduler
    for k, v in {"DATABASE_URL": "postgresql+psycopg://x:y@localhost/z",
                 "SESSION_SECRET": "test-secret-at-least-16-chars", "ALLOWED_EMAIL_DOMAINS": "ki.se",
                 "APP_BASE_URL": "http://testserver", "DRAFTING_ADAPTER": "fake"}.items():
        monkeypatch.setenv(k, v)
    settings = Settings()

    class _NullAlerter:
        def record_failure(self, *a, **k): pass
        def record_success(self, *a, **k): pass

    def session_factory(): return None
    wc = build_workspace_controller(settings=settings, session_factory=session_factory)
    sched = build_scheduler(session_factory=session_factory, settings=settings, alerter=_NullAlerter(),
                            ollama_url="http://localhost:11434", gemma_model="g", ranking_tick_seconds=60,
                            ranking_queue_batch=10, embedding_pregate_threshold=0.30,
                            embed_client=MagicMock(model="bge-m3"), workspace_controller=wc, start=False)
    assert sched.get_job("profile_bootstrap_tick") is not None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_profiles_bootstrap_worker.py -v`
Expected: FAIL — `ModuleNotFoundError: ... 'bootstrap_worker'`.

- [ ] **Step 3: Create `src/call_manager/profiles/bootstrap_worker.py`**

```python
"""Bootstrap-job worker — drains profile_bootstrap_jobs.

Mirrors run_phase0_tick: claim one queued job (FOR UPDATE SKIP LOCKED), run the
never-raising bootstrap, branch on the explicit outcome. ALL post-claim work is
wrapped so any exception transitions the job immediately (queued/failed) instead
of leaving it stuck 'running'. attempts is counted once at claim; exhaustion and
the reaper use `attempts >= MAX` (no +1)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

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
    return SeedInput(name=payload.get("name", ""), kind=payload.get("kind", "organizational_unit"),
                     level=payload.get("level"), members=members,
                     pasted_grants=payload.get("pasted_grants", ""),
                     mission_text=payload.get("mission_text", ""))


def requeue_stuck_bootstrap_jobs(session, *, max_attempts: int = MAX_BOOTSTRAP_ATTEMPTS) -> int:
    """Recover jobs stuck 'running' past STUCK_BOOTSTRAP_AFTER (worker crash).
    attempts was already counted at claim → dead-letter at `attempts >= max`,
    do NOT re-increment. Caller commits."""
    cutoff = datetime.now(UTC) - STUCK_BOOTSTRAP_AFTER
    dead = session.execute(
        update(ProfileBootstrapJob).where(
            ProfileBootstrapJob.status == "running",
            ProfileBootstrapJob.started_at.is_not(None),
            ProfileBootstrapJob.started_at < cutoff,
            ProfileBootstrapJob.attempts >= max_attempts,
        ).values(status="failed", last_error="dead-lettered: stuck running (worker crash)",
                 seed=None, completed_at=datetime.now(UTC))
    ).rowcount
    session.execute(
        update(ProfileBootstrapJob).where(
            ProfileBootstrapJob.status == "running",
            ProfileBootstrapJob.started_at.is_not(None),
            ProfileBootstrapJob.started_at < cutoff,
        ).values(status="queued", started_at=None)
    )
    return dead


def _finalize_failure(session_factory, job_id, attempts: int, msg: str) -> None:
    with session_factory() as s:
        job = s.get(ProfileBootstrapJob, job_id)
        if job is None:
            return
        if attempts >= MAX_BOOTSTRAP_ATTEMPTS:
            job.status, job.completed_at, job.last_error, job.seed = "failed", datetime.now(UTC), msg, None
        else:
            job.status, job.started_at, job.last_error = "queued", None, msg
        s.commit()


def run_profile_bootstrap_tick(*, session_factory: sessionmaker, pubmed, ollama, embed_client) -> None:
    with session_factory() as s:
        requeue_stuck_bootstrap_jobs(s)
        s.commit()

    with session_factory() as s:
        job = s.execute(
            select(ProfileBootstrapJob).where(ProfileBootstrapJob.status == "queued")
            .order_by(ProfileBootstrapJob.queued_at).with_for_update(skip_locked=True).limit(1)
        ).scalar_one_or_none()
        if job is None:
            return
        job.status, job.started_at, job.attempts = "running", datetime.now(UTC), job.attempts + 1
        job_id, profile_id, attempts = job.id, job.profile_id, job.attempts
        seed_payload = dict(job.seed or {})
        s.commit()

    try:
        draft = bootstrap_profile(seed=_seed_from_payload(seed_payload), pubmed=pubmed, ollama=ollama)
        if not draft.llm_succeeded:
            _finalize_failure(session_factory, job_id, attempts, "llm draft failed or empty")
            return
        with session_factory() as s:
            merge_bootstrap_into_profile(s.get(Profile, profile_id), draft)
            refresh_profile_embedding(s, embed_client, profile_id)
            s.commit()
        backfill_profile(session_factory=session_factory, profile_id=profile_id)
        with session_factory() as s:
            job = s.get(ProfileBootstrapJob, job_id)
            job.status, job.completed_at = "done", datetime.now(UTC)
            job.pubmed_status, job.publications_fetched, job.seed = (
                draft.pubmed_status, draft.publications_fetched, None)
            s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("profiles.bootstrap.worker_failed",
                       extra={"event": "bootstrap_worker_failed", "profile_id": profile_id, "error": str(e)})
        _finalize_failure(session_factory, job_id, attempts, f"{type(e).__name__}: {e}")
```

- [ ] **Step 4: Add the config tick to `src/call_manager/config.py`**

```python
    profile_bootstrap_tick_seconds: int = Field(default=30, alias="PROFILE_BOOTSTRAP_TICK_SECONDS")
```

- [ ] **Step 5: Register the tick in `src/call_manager/scheduler.py`** (after the Phase-0 tick; `embed_client` is already in scope)

```python
    def _profile_bootstrap_tick() -> None:
        from call_manager.profiles.bootstrap_worker import run_profile_bootstrap_tick
        from call_manager.profiles.pubmed import PubMedClient
        from call_manager.ranking.ollama_client import OllamaClient
        try:
            run_profile_bootstrap_tick(
                session_factory=session_factory, pubmed=PubMedClient(),
                ollama=OllamaClient(base_url=settings.ollama_url, model=settings.gemma_model),
                embed_client=embed_client)
        except Exception as e:  # noqa: BLE001
            _logger.warning("profile_bootstrap.tick_failed",
                            extra={"event": "profile_bootstrap_tick_failed", "error": str(e)})

    sched.add_job(_profile_bootstrap_tick, trigger=IntervalTrigger(seconds=settings.profile_bootstrap_tick_seconds),
                  id="profile_bootstrap_tick", max_instances=1, coalesce=True)
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_profiles_bootstrap_worker.py -v`
Expected: PASS.

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check . && uv run ruff format .
git add src/call_manager/profiles/bootstrap_worker.py src/call_manager/config.py src/call_manager/scheduler.py tests/test_profiles_bootstrap_worker.py
git commit -m "feat(profiles): bootstrap worker with try/except retry, embed-before-backfill, reaper, scheduler tick"
```

### Task 6: edit-route guards (block while drafting; skip backfill on blank description)

**Files:**
- Modify: `src/call_manager/web/routes/profiles.py` (`profile_edit_submit`)
- Test: `tests/test_web_profiles_edit_guards.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_web_profiles_edit_guards.py
from fastapi.testclient import TestClient

from call_manager.db.models import ProfileBootstrapJob, Profile, ProfileKind, User
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    for k, v in {"DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
                 "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver"}.items():
        monkeypatch.setenv(k, v)
    return create_app()


def _owner_profile(app, pg_session_factory, job_status):
    with pg_session_factory() as s:
        u = User(email="owner@ki.se", is_admin=False)
        s.add(u)
        s.flush()
        p = Profile(name="Grp", kind=ProfileKind.ORGANIZATIONAL_UNIT.value, level="group",
                    description="", structured={"areas": []}, created_by=u.id)
        s.add(p)
        s.flush()
        s.add(ProfileBootstrapJob(profile_id=p.id, status=job_status))
        s.commit()
        uid, pid = u.id, p.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, pid


def test_edit_blocked_while_bootstrap_active(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid = _owner_profile(app, pg_session_factory, "running")
    r = c.post(f"/profiles/{pid}/edit", data={"description": "x", "budget_currency": "SEK"},
               follow_redirects=False)
    assert r.status_code == 409


def test_blank_description_save_does_not_backfill(monkeypatch, pg_url, pg_session_factory):
    called = {"n": 0}
    monkeypatch.setattr("call_manager.web.routes.profiles.backfill_profile",
                        lambda **k: called.__setitem__("n", called["n"] + 1) or 0)
    app = _app(monkeypatch, pg_url)
    c, pid = _owner_profile(app, pg_session_factory, "done")  # terminal → editable
    r = c.post(f"/profiles/{pid}/edit", data={"description": "   ", "budget_currency": "SEK"},
               follow_redirects=False)
    assert r.status_code == 303
    assert called["n"] == 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_web_profiles_edit_guards.py -v`
Expected: FAIL — edit returns 303 even with an active job and schedules backfill regardless.

- [ ] **Step 3: Add the guards to `profile_edit_submit`**

After the owner-or-admin check, add the active-bootstrap guard:

```python
        active_job = session.execute(
            select(ProfileBootstrapJob.id).where(
                ProfileBootstrapJob.profile_id == profile_id,
                ProfileBootstrapJob.status.in_(("queued", "running")))
        ).scalar_one_or_none()
        if active_job is not None:
            raise HTTPException(status_code=409, detail="profile is still drafting — try again shortly")
```

Extend the models import with `ProfileBootstrapJob`. Gate the background backfill on a non-blank description — replace the unconditional `background.add_task(_refresh_and_backfill_in_background, ...)`:

```python
    if description.strip():
        embed_client = request.app.state.embed_client
        background.add_task(_refresh_and_backfill_in_background, session_factory=session_factory,
                            embed_client=embed_client, profile_id=profile_id, refresh_embedding=description_changed)
```

- [ ] **Step 4: Run the tests**

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

### Task 7: bootstrap banner + whole-form lock + reload-on-terminal (Codex R2 #4)

**Files:**
- Modify: `src/call_manager/web/routes/profiles.py` (`profile_edit_form` GET; new `GET /profiles/{id}/bootstrap-status`)
- Create: `src/call_manager/web/templates/partials/bootstrap_banner.html`
- Modify: `src/call_manager/web/templates/profile_edit.html`
- Test: `tests/test_web_bootstrap_status.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_web_bootstrap_status.py
from fastapi.testclient import TestClient

from call_manager.db.models import ProfileBootstrapJob, Profile, ProfileKind, User
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    for k, v in {"DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
                 "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver"}.items():
        monkeypatch.setenv(k, v)
    return create_app()


def _owner_profile(app, pg_session_factory, status, **jobkw):
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


def test_edit_page_running_is_disabled_and_polls(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid = _owner_profile(app, pg_session_factory, "running")
    body = c.get(f"/profiles/{pid}/edit").text
    assert "Drafting" in body
    assert "disabled" in body  # the wrapping fieldset
    assert "/bootstrap-status" in body  # poll element mounted


def test_edit_page_done_unavailable_wording_and_editable(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid = _owner_profile(app, pg_session_factory, "done", pubmed_status="unavailable", publications_fetched=0)
    body = c.get(f"/profiles/{pid}/edit").text
    assert "PubMed was unavailable" in body
    assert "/bootstrap-status" not in body  # no poll once terminal


def test_status_route_sets_refresh_when_terminal(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid = _owner_profile(app, pg_session_factory, "done", pubmed_status="ok", publications_fetched=2)
    r = c.get(f"/profiles/{pid}/bootstrap-status")
    assert r.headers.get("HX-Refresh") == "true"


def test_status_route_keeps_polling_while_active(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid = _owner_profile(app, pg_session_factory, "running")
    r = c.get(f"/profiles/{pid}/bootstrap-status")
    assert r.status_code == 200
    assert "HX-Refresh" not in r.headers
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_web_bootstrap_status.py -v`
Expected: FAIL — route 404 / banner+fieldset not present.

- [ ] **Step 3: Create `src/call_manager/web/templates/partials/bootstrap_banner.html`**

```html
{% if job and job.status in ("queued", "running") %}
<div class="banner banner-info">
  <span class="spinner" aria-hidden="true"></span>
  Drafting your profile from PubMed and past grants — this can take a minute on the local model.
</div>
<div hx-get="/profiles/{{ profile.id }}/bootstrap-status" hx-trigger="every 3s" hx-swap="none"></div>
{% elif job and job.status == "done" %}
<div class="banner banner-success">
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

- [ ] **Step 4: Wire `profile_edit_form` GET + the status route in `src/call_manager/web/routes/profiles.py`**

In `profile_edit_form`, load the job and compute `editable`, then pass both to the template:

```python
        job = session.execute(
            select(ProfileBootstrapJob).where(ProfileBootstrapJob.profile_id == profile_id)
        ).scalar_one_or_none()
        editable = job is None or job.status in ("done", "failed")
        session.expunge_all()
    return request.app.state.templates.TemplateResponse(
        request=request, name="profile_edit.html",
        context={"profile": p, "user": user, "job": job, "editable": editable})
```

Add the status route:

```python
from fastapi import Response  # add to imports


@router.get("/profiles/{profile_id}/bootstrap-status")
def profile_bootstrap_status(request: Request, profile_id: int,
                             user: User = Depends(get_current_user)):  # noqa: B008
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        p = session.get(Profile, profile_id)
        if p is None or p.is_active is False:
            raise HTTPException(status_code=404, detail="profile not found")
        if p.created_by != user.id and not user.is_admin:
            raise HTTPException(status_code=403, detail="not authorized")
        status = session.scalar(
            select(ProfileBootstrapJob.status).where(ProfileBootstrapJob.profile_id == profile_id))
    if status in (None, "done", "failed"):
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return Response(status_code=200)  # still drafting — keep polling, no swap
```

- [ ] **Step 5: Edit `src/call_manager/web/templates/profile_edit.html`**

Render the banner above the form and wrap the form body in a fieldset disabled while not editable:

```html
{% include "partials/bootstrap_banner.html" %}
<form method="post" action="/profiles/{{ profile.id }}/edit">
  <fieldset {% if not editable %}disabled{% endif %}>
    {# ... existing form fields ... #}
  </fieldset>
</form>
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_web_bootstrap_status.py -v`
Expected: PASS.

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check . && uv run ruff format .
git add src/call_manager/web/routes/profiles.py src/call_manager/web/templates/partials/bootstrap_banner.html src/call_manager/web/templates/profile_edit.html tests/test_web_bootstrap_status.py
git commit -m "feat(profiles): bootstrap banner, whole-form lock while drafting, reload-on-terminal"
```

### Task 8: ranking-status strip — poll, drain-refresh, unresolved-failed count (Codex R2 #5/#6/#8)

**Files:**
- Modify: `src/call_manager/web/routes/calls.py` (new `GET /calls/ranking-status`)
- Create: `src/call_manager/web/templates/partials/ranking_status_strip.html`
- Modify: `src/call_manager/web/templates/calls_index.html`
- Test: `tests/test_web_ranking_status_strip.py`

- [ ] **Step 1: Write the failing tests (seed real Source+Call rows for the FK)**

```python
# tests/test_web_ranking_status_strip.py
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.db.models import (
    Call, CallRanking, RankingJob, RankingJobStatus, Profile, ProfileKind, Source, User,
)
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    for k, v in {"DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
                 "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver"}.items():
        monkeypatch.setenv(k, v)
    return create_app()


def _setup(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="u@ki.se")
        s.add(u)
        s.flush()
        p = Profile(name="Grp", kind=ProfileKind.ORGANIZATIONAL_UNIT.value, level="group",
                    description="d", structured={}, created_by=u.id)
        s.add(p)
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        calls = [Call(source_id=src.id, external_id=f"E{i}", url=f"https://x/{i}", title=f"C{i}",
                      published_at=datetime(2026, 4, i + 1, tzinfo=UTC)) for i in range(3)]
        s.add_all(calls)
        s.flush()
        ids = (p.id, u.id, [c.id for c in calls])
        s.commit()
    pid, uid, call_ids = ids
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, pid, call_ids


def test_strip_empty_when_nothing_pending_or_failed(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid, _ = _setup(app, pg_session_factory)
    r = c.get(f"/calls/ranking-status?profile={pid}")
    assert r.status_code == 200 and "Ranking" not in r.text and "couldn't be scored" not in r.text


def test_strip_shows_pending_and_unresolved_failed(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid, call_ids = _setup(app, pg_session_factory)
    with pg_session_factory() as s:
        s.add(RankingJob(call_id=call_ids[0], profile_id=pid, status=RankingJobStatus.PENDING))
        s.add(RankingJob(call_id=call_ids[1], profile_id=pid, status=RankingJobStatus.FAILED))
        # A FAILED job that was later scored (has a CallRanking) must NOT count.
        s.add(RankingJob(call_id=call_ids[2], profile_id=pid, status=RankingJobStatus.FAILED))
        s.add(CallRanking(call_id=call_ids[2], profile_id=pid, aggregate_score=70,
                          rationale="ok", model="g", prompt_version="v"))
        s.commit()
    r = c.get(f"/calls/ranking-status?profile={pid}")
    assert "Ranking 1 call" in r.text
    assert "1 call couldn't be scored" in r.text  # only the unresolved one
    assert "poll=1" in r.text                       # self-poll armed


def test_drain_poll_triggers_refresh(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, pid, _ = _setup(app, pg_session_factory)  # nothing pending
    r = c.get(f"/calls/ranking-status?profile={pid}&poll=1")
    assert r.headers.get("HX-Refresh") == "true"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_web_ranking_status_strip.py -v`
Expected: FAIL — route 404.

- [ ] **Step 3: Create `src/call_manager/web/templates/partials/ranking_status_strip.html`**

```html
<div class="ranking-status-strip"
     {% if pending > 0 %}hx-get="/calls/ranking-status?profile={{ profile_id }}&poll=1"
     hx-trigger="every 5s" hx-swap="outerHTML"{% endif %}>
  {% if pending > 0 %}Ranking {{ pending }} call{{ "s" if pending != 1 else "" }} for this profile…{% endif %}
  {% if failed > 0 %}<span class="ranking-status-failed">{{ failed }} call{{ "s" if failed != 1 else "" }} couldn't be scored automatically.</span>{% endif %}
</div>
```

- [ ] **Step 4: Add the route + count helper in `src/call_manager/web/routes/calls.py`**

```python
from fastapi import Response  # add to imports
from sqlalchemy import and_, func, select  # ensure func + and_ imported
from call_manager.db.models import CallRanking, RankingJob, RankingJobStatus  # add to imports


def _ranking_counts(session, profile_id: int) -> tuple[int, int]:
    pending = session.scalar(
        select(func.count()).select_from(RankingJob).where(
            RankingJob.profile_id == profile_id,
            RankingJob.status.in_((RankingJobStatus.PENDING, RankingJobStatus.RUNNING)))) or 0
    # Unresolved failures only: a FAILED job with no CallRanking yet for that pair.
    scored = (select(CallRanking.call_id).where(
        CallRanking.profile_id == profile_id, CallRanking.call_id == RankingJob.call_id))
    failed = session.scalar(
        select(func.count()).select_from(RankingJob).where(
            RankingJob.profile_id == profile_id, RankingJob.status == RankingJobStatus.FAILED,
            ~scored.exists())) or 0
    return pending, failed


@router.get("/calls/ranking-status", response_class=HTMLResponse)
def ranking_status(request: Request, profile: int = Query(...), poll: int = Query(default=0),
                   user: User = Depends(get_current_user)):  # noqa: B008
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        pending, failed = _ranking_counts(session, profile)
    if pending == 0 and poll == 1:
        # Drained while polling → reload the feed so the new scores show.
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return request.app.state.templates.TemplateResponse(
        request=request, name="partials/ranking_status_strip.html",
        context={"pending": pending, "failed": failed, "profile_id": profile})
```

- [ ] **Step 5: Mount the strip on the profile feed in `src/call_manager/web/templates/calls_index.html`**

Just above the feed list, when a profile is selected (use the template's existing selected-profile id variable — confirm its name in the template):

```html
{% if selected_profile_id %}
<div hx-get="/calls/ranking-status?profile={{ selected_profile_id }}" hx-trigger="load" hx-swap="outerHTML"></div>
{% endif %}
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_web_ranking_status_strip.py -v`
Expected: PASS.

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check . && uv run ruff format .
git add src/call_manager/web/routes/calls.py src/call_manager/web/templates/partials/ranking_status_strip.html src/call_manager/web/templates/calls_index.html tests/test_web_ranking_status_strip.py
git commit -m "feat(calls): ranking-status strip (poll, drain-refresh, unresolved-failed count)"
```

---

## Phase 5 — Sticky default profile

### Task 9: per-user default resolution, validate-before-persist (Codex R2 #7)

**Files:**
- Modify: `src/call_manager/web/routes/calls.py` (`calls_index`)
- Test: `tests/test_web_calls_sticky_default.py`

- [ ] **Step 1: Write the failing tests (incl. the invalid-id-preserves-default case)**

```python
# tests/test_web_calls_sticky_default.py
from fastapi.testclient import TestClient

from call_manager.db.models import Profile, ProfileKind, User
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    for k, v in {"DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
                 "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver"}.items():
        monkeypatch.setenv(k, v)
    return create_app()


def _client(app, pg_session_factory, set_default=False):
    with pg_session_factory() as s:
        u = User(email="u@ki.se")
        s.add(u)
        s.flush()
        p = Profile(name="Grp", kind=ProfileKind.ORGANIZATIONAL_UNIT.value, level="group",
                    description="d", structured={}, created_by=u.id)
        s.add(p)
        s.flush()
        if set_default:
            u.default_profile_id = p.id
        s.commit()
        uid, pid = u.id, p.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid, pid


def test_valid_selection_persists_default(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _client(app, pg_session_factory)
    c.get(f"/?profile={pid}")
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id == pid


def test_bare_root_uses_stored_default(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _client(app, pg_session_factory, set_default=True)
    assert str(pid) in c.get("/").text


def test_explicit_empty_clears_default(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _client(app, pg_session_factory, set_default=True)
    c.get("/?profile=")
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id is None


def test_invalid_id_does_not_destroy_default(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _client(app, pg_session_factory, set_default=True)
    c.get("/?profile=99999")
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id == pid  # unchanged
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_web_calls_sticky_default.py -v`
Expected: FAIL — selection isn't persisted; bare `/` doesn't use the default; `?profile=99999` overwrites it.

- [ ] **Step 3: Implement validate-before-persist in `calls_index`**

Capture absent-vs-empty, then resolve+validate inside the existing single session. The key ordering rule (Codex R2 #7): **validate the requested id is real and active BEFORE writing it as the default.** Replace the opening of `calls_index`:

```python
    raw_profile = profile  # None = absent; "" = explicit "no profile"
    requested_id = _parse_optional_profile_id(profile)
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        # --- resolve the effective profile, persisting the default only after validation ---
        if raw_profile is None:  # bare / or return visit
            stored = session.scalar(select(User.default_profile_id).where(User.id == user.id))
            profile_id = session.scalar(
                select(Profile.id).where(Profile.id == stored, Profile.is_active.is_(True))
            ) if stored is not None else None
        elif raw_profile == "":  # explicit "— No profile —"
            u = session.get(User, user.id)
            if u is not None and u.default_profile_id is not None:
                u.default_profile_id = None
                session.commit()
            profile_id = None
        else:  # an id was requested — validate BEFORE persisting
            profile_id = session.scalar(
                select(Profile.id).where(Profile.id == requested_id, Profile.is_active.is_(True)))
            if profile_id is not None:
                u = session.get(User, user.id)
                if u is not None and u.default_profile_id != profile_id:
                    u.default_profile_id = profile_id
                    session.commit()
            # invalid/inactive request → profile_id stays None; stored default untouched
        profile = profile_id  # type: ignore[assignment]
```

Keep the rest of the handler. The existing `profile_obj` validation lower down may now be redundant for the selected id (already validated), but leaving it is harmless; ensure no second `session_factory()` block is opened — fold the above into the existing `with` block.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_web_calls_sticky_default.py tests/test_web_calls.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check . && uv run ruff format .
git add src/call_manager/web/routes/calls.py tests/test_web_calls_sticky_default.py
git commit -m "feat(calls): per-user sticky default profile (validate before persist)"
```

---

## Final verification

- [ ] **Run the full suite + lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all green. The modules most likely to need expectation updates: `test_web_profiles.py`/`test_profiles_e2e.py` (synchronous-create assertions) and `test_scheduler.py` (job set now includes `profile_bootstrap_tick`).

- [ ] **Manual smoke (optional; needs a running app + Ollama)**

Create a profile → immediate redirect to a disabled edit form with the "Drafting…" banner → after a tick the page reloads itself into an editable form with the completeness line → the dashboard shows "Ranking N calls…" for the new profile, reloads when scoring drains, and lands on the profile by default on the next bare-`/` visit.

---

## Self-Review (plan author)

**Spec coverage (R2):** async bootstrap (T2–T5); A2 edit-blocking + single backfill (T5/T6); worker try/except retry + reaper attempts (T5); non-blank `llm_succeeded` (T2); rubric-carrying merge (T3); atomic create + auto-subscribe + default (T4); seed-cap **rejection** (T4); progress banner + whole-form lock + reload-on-terminal (T7); ranking strip poll + drain-refresh + unresolved-failed count + FK-seeded test (T8); sticky default validate-before-persist (T9); migration branch off 0041 (T1). Added tests: atomic rollback, preserve-existing-default, degraded PubMed, embed-before-backfill order, reaper, backfill-failure, scheduler registration.

**Placeholder scan:** no TBD/TODO; every code step shows concrete code. Two "confirm the variable/column name against the file" prompts (selected-profile var in `calls_index.html`; that `Source` slug `vinnova` is migration-seeded) — verification prompts, not placeholders.

**Type consistency:** `ProfileBootstrapJob` fields; `DraftProfile(..., llm_succeeded, pubmed_status)`; `merge_bootstrap_into_profile(profile, draft)`; `create_profile_with_bootstrap(*, session, owner_id, name, kind, level, structured, seed_payload)`; `run_profile_bootstrap_tick(*, session_factory, pubmed, ollama, embed_client)`; `requeue_stuck_bootstrap_jobs(session)`; `_ranking_counts(session, profile_id)`; `RankingJobStatus` enum members — all used identically across defining and calling tasks.

**Cross-plan dependency:** resolved — ranking-v2.1 (`0045`) is merged to master, so migration `0046` chains linearly off `0045` (header + Task 1). No `alembic merge` needed.
