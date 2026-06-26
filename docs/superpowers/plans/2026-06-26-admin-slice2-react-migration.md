# Admin Slice 2 — dashboard + drafting + golden-set — React migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the three remaining admin pages (`/admin` ops dashboard, `/admin/drafting`, `/admin/golden-set`) and their six ops/CRUD actions from Jinja2+HTMX to the React SPA, then finish the admin Jinja teardown — leaving auth as the only remaining server-rendered surface.

**Architecture:** Standard strangler-fig slice. Add two typed `/api` modules (`web/api/admin.py`, `web/api/admin_golden_set.py`) that port the pure query-builders out of the doomed `web/routes/admin.py`; add React routes under a new nested `/admin` layout route behind the existing `RequireAdmin` guard; then delete the Jinja modules/templates/tests and flip the manifest. The bare `/admin` prefix is registered LAST because `is_spa_route` matches exact-or-beneath — registering `/admin` subsumes every `/admin/*` sub-prefix.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x / Pydantic v2 / pytest (testcontainers Postgres); React 19 / Vite / TanStack Query / openapi-fetch / react-router-dom / vitest + RTL (`pnpm`).

**Spec:** `docs/superpowers/specs/2026-06-26-admin-slice2-react-migration-design.md` (read it for rationale; this plan is the build order).

## Global Constraints

Every task implicitly includes these. Exact values copied from the spec:

- **No DB schema change. No new dependency.** Strict parity port. Blessed deviations: golden-set FK→`400` and duplicate-pair→`409` (Jinja 500s on both) and the banner-UX JSON translation.
- **Errors via `raise HTTPException(status_code, detail)`** → the global `/api` envelope handler (`main.py`) maps `/api` paths to `{"error":{"code","message"}}`. NEVER hand-build a JSON error body. Codes: 400=bad_request, 401=unauthorized, 403=forbidden, 404=not_found, 409=conflict, 413=payload_too_large.
- **Every endpoint is `require_admin`** (`Depends(require_admin)`, param annotated `# noqa: B008`): `403` for an authenticated non-admin, `401` for unauthenticated.
- **`OriginCSRFMiddleware` allows an ABSENT `Origin`** — TestClient mutation tests pass without an `Origin` header. Do NOT add one.
- **CSS Modules + `--app-*` tokens; NO bare element selectors** in any `.module.css` (scope under a hashed class). Known-good tokens: `--app-border/-muted/-text/-accent/-sidebar/-link/-heading`; `var(--app-danger, #e5484d)` keeps the inline fallback.
- **`tsconfig` has `noUnusedLocals: true` + `include: ["src"]`** — no unused imports/locals anywhere under `src/` (the frontend has NO eslint; `tsc --noEmit` is the gate).
- **openapi-fetch path-param types:** integer path params type as `number`, string path params as `string`. Query hooks set `retry: false`.
- **Manifest is prefix-not-`:param`:** `frontend/routes.json` `spa_routes` holds prefixes. The three new React routes + the manifest change land in ONE commit (`router.test.tsx` is a set-equality drift guard).
- **Two OpenAPI regens:** `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh` → `frontend/src/api/schema.gen.ts`. Run once after the `/api` endpoints exist (Task 5) and once AFTER teardown deletes the Jinja routes (Task 11). The `bge-m3` override is mandatory — the mxbai `.env` default logs a stdout WARNING that corrupts the codegen.
- **Two routes regens:** `uv run python scripts/gen_routes.py` → `frontend/src/routes.gen.ts` + `src/call_manager/web/spa_routes.py`. Run when `spa_routes` changes (Task 10) and when `backend_prefixes` changes (Task 11). `--check` verifies sync.
- **Whole-suite testcontainers Postgres flakes under load** (connection refused = infra, not a real failure) — run the targeted files named in each task, not the whole suite.
- **Backend test fixtures:** `monkeypatch`, `pg_url`, `pg_session_factory`, `profile_factory` (all in the `tests/` root conftest). Admin auth: create an `is_admin=True` User, then `client.cookies.set("session", app.state.session_signer.make(user_id=uid))`.
- **Frontend test conventions:** vitest + RTL; `renderWithProviders` from `../test-utils`; component POST assertions wrapped in `await waitFor(...)`; hooks tested via `renderHook` from `@testing-library/react`; mocks via `vi.spyOn(clientMod.api, "GET"/"POST"/"DELETE")`.
- **Date display divergence (blessed, matches Slice-1 `AdminPendingUsers`):** the `/api` returns raw ISO datetimes; React renders with `new Date(x).toLocaleString()` (browser-local), NOT the Jinja `display_dt` Europe/Stockholm filter. Internal Stockholm users → browser-local ≈ Stockholm. Date-format assertions from the Jinja tests are NOT migrated.

---

### Task 1: Backend — `GET /api/admin/dashboard` read-model

The single big read-model the `/admin` ops dashboard renders. Ports the two pure query-builders `_digest_panel_context` and `_embedding_coverage_context` verbatim out of the doomed `web/routes/admin.py` into the new module (the old copies stay alive until Task 11 deletes that module — transient duplication by design).

**Files:**
- Create: `src/call_manager/web/api/admin.py`
- Modify: `src/call_manager/web/api/__init__.py` (register `admin.router`)
- Test: `tests/test_api_admin_dashboard.py`

**Interfaces:**
- Consumes: `require_admin` (`call_manager.auth.dependencies`); models `Call, DigestRun, Profile, RankingJob, RankingJobStatus, Source, User`; `_week_start` (`call_manager.digest.send.week_start`); `BACKFILL_DAYS` (`call_manager.ranking.backfill`); `load_last_report() -> GoldenSetReport | None` with fields `n_fixture:int, n_compared:int, tau:float|None` (`call_manager.ranking.eval`); `app.state.embedding_calibration_warning: str | None`; `app.state.settings.embedding_model`.
- Produces: `router` (an `APIRouter`, no prefix — the `/api` prefix comes from the parent in `__init__.py`); `GET /api/admin/dashboard` → `AdminDashboardOut`; the module-level helpers `_digest_panel_context(session) -> dict` and `_embedding_coverage_context(session, *, current_model) -> dict` that Tasks 2-3 import within the same module.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_admin_dashboard.py`:

```python
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.db.models import (
    Call,
    DigestRun,
    Profile,
    ProfileKind,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from call_manager.main import create_app


def _setup(monkeypatch, pg_url, *, model="bge-m3"):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("EMBEDDING_MODEL", model)
    return create_app()


def _admin(app, sf, *, email="root@ki.se"):
    with sf() as s:
        u = User(status="active", email=email, is_admin=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid


def _nonadmin(app, sf, *, email="alice@ki.se"):
    with sf() as s:
        u = User(status="active", email=email, is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c


def test_dashboard_requires_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c = _nonadmin(app, pg_session_factory)
    assert c.get("/api/admin/dashboard").status_code == 403


def test_dashboard_requires_auth(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    r = TestClient(app).get("/api/admin/dashboard")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_dashboard_lists_sources_with_call_counts(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        src.last_fetched_at = datetime(2026, 4, 16, 3, 15, tzinfo=UTC)
        s.add(Call(source_id=src.id, external_id="X", url="https://x", title="T"))
        s.commit()
    body = c.get("/api/admin/dashboard").json()
    row = next(x for x in body["sources"] if x["slug"] == "vinnova")
    assert row["name"] == "Vinnova"
    assert row["call_count"] == 1
    assert row["last_fetched_at"].startswith("2026-04-16T03:15")


def test_dashboard_queue_counts_default_zero(monkeypatch, pg_url, pg_session_factory, profile_factory):
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c0 = Call(source_id=src.id, external_id="Q", url="https://x/q", title="T")
        s.add(c0)
        s.flush()
        s.add(RankingJob(call_id=c0.id, profile_id=profile_id, status=RankingJobStatus.PENDING))
        s.commit()
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    counts = c.get("/api/admin/dashboard").json()["queue_counts"]
    assert counts == {"pending": 1, "running": 0, "done": 0, "failed": 0}


def test_dashboard_rerank_estimate_is_product(monkeypatch, pg_url, pg_session_factory, profile_factory):
    profile_factory()
    profile_factory(name="Group B")
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        for i in range(3):
            s.add(Call(source_id=src.id, external_id=f"E{i}", url=f"https://x/e{i}",
                       title="T", published_at=datetime.now(UTC) - timedelta(days=5)))
        s.commit()
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    body = c.get("/api/admin/dashboard").json()
    assert body["active_profiles_count"] == 2
    assert body["recent_calls_count"] == 3
    assert body["rerank_estimate"] == 6
    assert body["backfill_days"] == 90


def test_dashboard_digest_panel(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, uid = _admin(app, pg_session_factory)
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        s.add(DigestRun(user_id=uid, sent_at=now, included_call_ids=[1, 2]))
        s.add(User(status="active", email="older@ki.se", digest_disabled_at=now - timedelta(days=3)))
        s.add(User(status="active", email="newer@ki.se", digest_disabled_at=now - timedelta(hours=1)))
        s.commit()
    d = c.get("/api/admin/dashboard").json()["digest"]
    assert d["runs_this_week"] == 1
    assert d["last_sent_at"] is not None
    assert d["disabled_users"] == 2
    emails = [r["email"] for r in d["disabled_user_rows"]]
    assert emails == ["newer@ki.se", "older@ki.se"]  # most-recent-first
    assert set(d["disabled_user_rows"][0]) == {"id", "email", "disabled_at"}


def test_dashboard_embedding_coverage(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    vec = [0.0] * 1024
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add(Call(source_id=src.id, external_id="c1", url="https://x/1", title="C1",
                   embedding=vec, embedding_model="bge-m3", published_at=datetime.now(UTC)))
        s.add(Call(source_id=src.id, external_id="c2", url="https://x/2", title="C2",
                   embedding=vec, embedding_model="bge-old", published_at=datetime.now(UTC)))
        s.add(Call(source_id=src.id, external_id="c3", url="https://x/3", title="C3",
                   embedding=None, embedding_model=None, published_at=datetime.now(UTC)))
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        s.add(Profile(name="P1", kind=ProfileKind.ORGANIZATIONAL_UNIT, description="d",
                      structured={}, created_by=owner.id, embedding=vec, embedding_model="bge-m3"))
        s.add(Profile(name="P2", kind=ProfileKind.ORGANIZATIONAL_UNIT, description="d",
                      structured={}, created_by=owner.id, embedding=None, embedding_model=None))
        s.commit()
    cov = c.get("/api/admin/dashboard").json()["embedding_coverage"]
    assert cov["call_embedded"] == 1 and cov["call_total"] == 3 and cov["call_stale"] == 2
    assert cov["profile_embedded"] == 1 and cov["profile_total"] == 2 and cov["profile_stale"] == 1
    assert cov["current_model"] == "bge-m3"


def test_dashboard_coverage_excludes_blank_profiles_counts_areas_only(
    monkeypatch, pg_url, pg_session_factory
):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    with pg_session_factory() as s:
        owner = User(status="active", email="owner2@ki.se")
        s.add(owner)
        s.flush()
        # blank description, no areas -> excluded from total entirely
        s.add(Profile(name="PB", kind=ProfileKind.ORGANIZATIONAL_UNIT, description="   ",
                      structured={}, created_by=owner.id, embedding=None, embedding_model=None))
        # areas-only (blank description) -> embeddable -> counted, stale
        s.add(Profile(name="PA", kind=ProfileKind.ORGANIZATIONAL_UNIT, description="",
                      structured={"areas": ["genomics"]}, created_by=owner.id,
                      embedding=None, embedding_model=None))
        s.commit()
    cov = c.get("/api/admin/dashboard").json()["embedding_coverage"]
    assert cov["profile_total"] == 1  # blank excluded, areas-only included
    assert cov["profile_stale"] == 1


def test_dashboard_golden_set_report_null(monkeypatch, pg_url, pg_session_factory):
    # load_last_report reads a sidecar file whose path is fixed at import time,
    # so monkeypatch the function (the endpoint lazy-imports it) for determinism.
    monkeypatch.setattr("call_manager.ranking.eval.load_last_report", lambda: None)
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    assert c.get("/api/admin/dashboard").json()["golden_set_report"] is None


def test_dashboard_golden_set_report_present(monkeypatch, pg_url, pg_session_factory):
    from call_manager.ranking.eval import GoldenSetReport

    monkeypatch.setattr(
        "call_manager.ranking.eval.load_last_report",
        lambda: GoldenSetReport(n_fixture=5, n_compared=4, tau=0.812),
    )
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    rep = c.get("/api/admin/dashboard").json()["golden_set_report"]
    assert rep == {"n_fixture": 5, "n_compared": 4, "tau": 0.812}


def test_dashboard_calibration_warning(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url, model="mxbai-embed-large")
    assert app.state.embedding_calibration_warning is not None
    c, _ = _admin(app, pg_session_factory)
    w = c.get("/api/admin/dashboard").json()["calibration_warning"]
    assert w is not None and "EMBEDDING_HARD_FAIL_THRESHOLD" in w


def test_dashboard_no_calibration_warning_known_model(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url, model="bge-m3")
    c, _ = _admin(app, pg_session_factory)
    assert c.get("/api/admin/dashboard").json()["calibration_warning"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_admin_dashboard.py -q`
Expected: FAIL / errors — `/api/admin/dashboard` is `404` (route not registered yet).

- [ ] **Step 3: Create the module**

Create `src/call_manager/web/api/admin.py` (`_digest_panel_context` and `_embedding_coverage_context` are copied VERBATIM from `web/routes/admin.py`):

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, or_, select, text

from call_manager.auth.dependencies import require_admin
from call_manager.db.models import (
    Call,
    DigestRun,
    Profile,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from call_manager.digest.send import week_start as _week_start
from call_manager.ranking.backfill import BACKFILL_DAYS

router = APIRouter()


def _digest_panel_context(session) -> dict:
    now = datetime.now(UTC)
    week_start_dt = _week_start(now)

    last_run = session.execute(
        select(DigestRun).order_by(DigestRun.sent_at.desc()).limit(1)
    ).scalar_one_or_none()
    runs_this_week = session.execute(
        select(func.count(DigestRun.id)).where(DigestRun.sent_at >= week_start_dt)
    ).scalar_one()
    disabled_users = session.execute(
        select(func.count(User.id)).where(User.digest_disabled_at.is_not(None))
    ).scalar_one()
    disabled_rows = session.execute(
        select(User.id, User.email, User.digest_disabled_at)
        .where(User.digest_disabled_at.is_not(None))
        .order_by(User.digest_disabled_at.desc())
    ).all()
    return {
        "digest_last_sent_at": last_run.sent_at if last_run else None,
        "digest_runs_this_week": runs_this_week,
        "digest_disabled_users": disabled_users,
        "digest_disabled_user_rows": [
            {"id": r.id, "email": r.email, "digest_disabled_at": r.digest_disabled_at}
            for r in disabled_rows
        ],
    }


def _embedding_coverage_context(session, *, current_model: str) -> dict:
    non_blank_call = func.length(func.trim(Call.title)) > 0
    non_blank_profile = or_(
        func.length(func.trim(func.coalesce(Profile.description, ""))) > 0,
        func.jsonb_array_length(func.coalesce(Profile.structured["areas"], text("'[]'::jsonb")))
        > 0,
        func.jsonb_array_length(
            func.coalesce(Profile.structured["org"]["capabilities"], text("'[]'::jsonb"))
        )
        > 0,
        func.jsonb_array_length(
            func.coalesce(Profile.structured["org"]["patient_populations"], text("'[]'::jsonb"))
        )
        > 0,
    )
    call_stale = session.execute(
        select(func.count(Call.id)).where(
            non_blank_call,
            or_(Call.embedding.is_(None), Call.embedding_model != current_model),
        )
    ).scalar_one()
    call_total = session.execute(select(func.count(Call.id)).where(non_blank_call)).scalar_one()
    profile_stale = session.execute(
        select(func.count(Profile.id)).where(
            non_blank_profile,
            or_(Profile.embedding.is_(None), Profile.embedding_model != current_model),
        )
    ).scalar_one()
    profile_total = session.execute(
        select(func.count(Profile.id)).where(non_blank_profile)
    ).scalar_one()
    return {
        "embedding_coverage": {
            "call_stale": call_stale,
            "call_total": call_total,
            "call_embedded": call_total - call_stale,
            "profile_stale": profile_stale,
            "profile_total": profile_total,
            "profile_embedded": profile_total - profile_stale,
            "current_model": current_model,
        }
    }


class SourceOut(BaseModel):
    name: str
    slug: str
    last_fetched_at: datetime | None
    call_count: int


class QueueCountsOut(BaseModel):
    pending: int
    running: int
    done: int
    failed: int


class DisabledUserOut(BaseModel):
    id: int
    email: str
    disabled_at: datetime


class DigestPanelOut(BaseModel):
    last_sent_at: datetime | None
    runs_this_week: int
    disabled_users: int
    disabled_user_rows: list[DisabledUserOut]


class EmbeddingCoverageOut(BaseModel):
    call_embedded: int
    call_total: int
    call_stale: int
    profile_embedded: int
    profile_total: int
    profile_stale: int
    current_model: str


class GoldenSetReportOut(BaseModel):
    n_fixture: int
    n_compared: int
    tau: float | None


class AdminDashboardOut(BaseModel):
    sources: list[SourceOut]
    queue_counts: QueueCountsOut
    rerank_estimate: int
    active_profiles_count: int
    recent_calls_count: int
    backfill_days: int
    digest: DigestPanelOut
    embedding_coverage: EmbeddingCoverageOut
    golden_set_report: GoldenSetReportOut | None
    calibration_warning: str | None


@router.get("/admin/dashboard", response_model=AdminDashboardOut)
def admin_dashboard(
    request: Request,
    admin: User = Depends(require_admin),  # noqa: B008
):
    settings = request.app.state.settings
    with request.app.state.session_factory() as session:
        rows = session.execute(
            select(Source, func.count(Call.id).label("call_count"))
            .outerjoin(Call, Call.source_id == Source.id)
            .group_by(Source.id)
            .order_by(Source.name)
        ).all()
        sources = [
            SourceOut(name=src.name, slug=src.slug,
                      last_fetched_at=src.last_fetched_at, call_count=cc)
            for src, cc in rows
        ]
        queue_rows = session.execute(
            select(RankingJob.status, func.count(RankingJob.id)).group_by(RankingJob.status)
        ).all()
        counts = {status: count for status, count in queue_rows}
        queue_counts = QueueCountsOut(
            pending=counts.get(RankingJobStatus.PENDING.value, 0),
            running=counts.get(RankingJobStatus.RUNNING.value, 0),
            done=counts.get(RankingJobStatus.DONE.value, 0),
            failed=counts.get(RankingJobStatus.FAILED.value, 0),
        )
        active_profiles_count = session.execute(
            select(func.count(Profile.id)).where(Profile.is_active.is_(True))
        ).scalar_one()
        recent_cutoff = datetime.now(UTC) - timedelta(days=BACKFILL_DAYS)
        recent_calls_count = session.execute(
            select(func.count(Call.id)).where(Call.published_at >= recent_cutoff)
        ).scalar_one()

        digest_ctx = _digest_panel_context(session)
        coverage_ctx = _embedding_coverage_context(
            session, current_model=settings.embedding_model
        )
        digest = DigestPanelOut(
            last_sent_at=digest_ctx["digest_last_sent_at"],
            runs_this_week=digest_ctx["digest_runs_this_week"],
            disabled_users=digest_ctx["digest_disabled_users"],
            disabled_user_rows=[
                DisabledUserOut(id=r["id"], email=r["email"], disabled_at=r["digest_disabled_at"])
                for r in digest_ctx["digest_disabled_user_rows"]
            ],
        )
        coverage = EmbeddingCoverageOut(**coverage_ctx["embedding_coverage"])

        # Lazy import: keeps the /api module cheap and dodges any ranking->web cycle.
        from call_manager.ranking.eval import load_last_report

        report = load_last_report()
        golden = (
            GoldenSetReportOut(n_fixture=report.n_fixture, n_compared=report.n_compared,
                               tau=report.tau)
            if report is not None
            else None
        )
        calibration_warning = getattr(
            request.app.state, "embedding_calibration_warning", None
        )

    return AdminDashboardOut(
        sources=sources,
        queue_counts=queue_counts,
        rerank_estimate=active_profiles_count * recent_calls_count,
        active_profiles_count=active_profiles_count,
        recent_calls_count=recent_calls_count,
        backfill_days=BACKFILL_DAYS,
        digest=digest,
        embedding_coverage=coverage,
        golden_set_report=golden,
        calibration_warning=calibration_warning,
    )
```

- [ ] **Step 4: Register the router**

Modify `src/call_manager/web/api/__init__.py` — add `admin` to the import block (alphabetical, before `admin_users`) and add an `include_router` line:

```python
from call_manager.web.api import (
    admin,
    admin_users,
    calls,
    citation_audit,
    export,
    files,
    me,
    profiles,
    references,
    rubric,
    settings,
    turns,
    workspaces,
)
```

and immediately after `router.include_router(me.router)`:

```python
router.include_router(admin.router)
router.include_router(admin_users.router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_admin_dashboard.py -q`
Expected: PASS (all ~11 tests).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/api/admin.py src/call_manager/web/api/__init__.py tests/test_api_admin_dashboard.py
git commit -m "feat(api): GET /api/admin/dashboard read-model (admin Slice 2)"
```

---

### Task 2: Backend — `GET /api/admin/drafting` read-model

Read-only drafting panel. Ports `_drafting_panel_context` verbatim into `web/api/admin.py`. Preserves the builder's `used_in`-desc per-workspace order (list order IS the contract). Maps the `CapCheckResult` dataclass and the ORM/`Row` objects to declared Pydantic shapes.

**Files:**
- Modify: `src/call_manager/web/api/admin.py` (add `_drafting_panel_context` + models + endpoint)
- Test: `tests/test_api_admin_drafting.py`

**Interfaces:**
- Consumes: models `ChatTurn, User, Workspace, WorkspaceEvent`; `check_global_daily_token_cap(session, *, settings) -> CapCheckResult` and `_stockholm_day_start_utc()` (`call_manager.proposal_assist.turns.caps`, lazy-imported inside the builder as in the original); `app.state.settings.drafting_enabled`. `CapCheckResult` fields: `allowed:bool, used_in:int, used_out:int, cap_in:int, cap_out:int, reason:str|None`.
- Produces: `GET /api/admin/drafting` → `AdminDraftingOut`; `_drafting_panel_context(session, settings) -> dict`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_admin_drafting.py`:

```python
from fastapi.testclient import TestClient

from call_manager.db.models import ChatTurn, User, Workspace, WorkspaceEvent
from call_manager.main import create_app


def _setup(monkeypatch, pg_url, *, drafting_enabled=True):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ENABLED", "true" if drafting_enabled else "false")
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "1000")
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_OUTPUT", "500")
    return create_app()


def _admin(app, sf, *, email="root@ki.se"):
    with sf() as s:
        u = User(status="active", email=email, is_admin=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c


def test_drafting_requires_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="alice@ki.se", is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    assert c.get("/api/admin/drafting").status_code == 403


def test_drafting_kill_switch_and_cap(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url, drafting_enabled=True)
    c = _admin(app, pg_session_factory)
    body = c.get("/api/admin/drafting").json()
    assert body["drafting_enabled"] is True
    assert body["global_cap"]["cap_in"] == 1000
    assert body["global_cap"]["cap_out"] == 500
    assert body["global_cap"]["allowed"] is True


def test_drafting_kill_switch_off(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url, drafting_enabled=False)
    c = _admin(app, pg_session_factory)
    assert c.get("/api/admin/drafting").json()["drafting_enabled"] is False


def test_drafting_per_workspace_usage(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="VisibleInPanel")
        s.add(ws)
        s.flush()
        s.add(ChatTurn(workspace_id=ws.id, role="assistant",
                       content={"type": "text", "text": "x"}, tokens_in=42, tokens_out=7))
        s.commit()
    c = _admin(app, pg_session_factory)
    rows = c.get("/api/admin/drafting").json()["per_workspace"]
    row = next(r for r in rows if r["title"] == "VisibleInPanel")
    assert row["owner_email"] == "owner@ki.se"
    assert row["used_in"] == 42 and row["used_out"] == 7
    assert isinstance(row["id"], str)  # UUID serialized as string for /cockpit/:id links


def test_drafting_per_workspace_desc_by_used_in(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = User(status="active", email="o@ki.se")
        s.add(owner)
        s.flush()
        small = Workspace(owner_id=owner.id, title="Small")
        big = Workspace(owner_id=owner.id, title="Big")
        s.add_all([small, big])
        s.flush()
        s.add(ChatTurn(workspace_id=small.id, role="assistant",
                       content={"type": "text", "text": "x"}, tokens_in=5))
        s.add(ChatTurn(workspace_id=big.id, role="assistant",
                       content={"type": "text", "text": "x"}, tokens_in=500))
        s.commit()
    c = _admin(app, pg_session_factory)
    titles = [r["title"] for r in c.get("/api/admin/drafting").json()["per_workspace"]]
    assert titles.index("Big") < titles.index("Small")  # used_in desc


def test_drafting_recent_failed_events(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = User(status="active", email="o@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="WithFails")
        s.add(ws)
        s.flush()
        s.add(WorkspaceEvent(workspace_id=ws.id, kind="cloud_turn_failed", actor_id=owner.id,
                             payload={"error": "distinctive_test_error"}))
        s.commit()
    c = _admin(app, pg_session_factory)
    events = c.get("/api/admin/drafting").json()["recent_events"]
    assert any(e["kind"] == "cloud_turn_failed"
               and e["payload"]["error"] == "distinctive_test_error" for e in events)


def test_drafting_cap_exceeded_reason(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = User(status="active", email="o@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="OverCapWs")
        s.add(ws)
        s.flush()
        s.add(ChatTurn(workspace_id=ws.id, role="assistant",
                       content={"type": "text", "text": "x"}, tokens_in=1500))  # cap_in=1000
        s.commit()
    c = _admin(app, pg_session_factory)
    cap = c.get("/api/admin/drafting").json()["global_cap"]
    assert cap["allowed"] is False
    assert cap["reason"] == "input_cap_exceeded"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_admin_drafting.py -q`
Expected: FAIL — `/api/admin/drafting` is `404`.

- [ ] **Step 3: Add the drafting builder, models, and endpoint to `web/api/admin.py`**

Add `import uuid` to the top imports, and `ChatTurn, User, Workspace, WorkspaceEvent` to the `call_manager.db.models` import (User already imported — add `ChatTurn, Workspace, WorkspaceEvent`). Append:

```python
def _drafting_panel_context(session, settings) -> dict:
    from call_manager.proposal_assist.turns.caps import (
        _stockholm_day_start_utc,
        check_global_daily_token_cap,
    )

    global_cap = check_global_daily_token_cap(session, settings=settings)

    day_start_utc = _stockholm_day_start_utc()
    per_workspace_rows = list(
        session.execute(
            select(
                Workspace.id,
                Workspace.title,
                User.email.label("owner_email"),
                func.coalesce(func.sum(ChatTurn.tokens_in), 0).label("used_in"),
                func.coalesce(func.sum(ChatTurn.tokens_out), 0).label("used_out"),
            )
            .join(User, User.id == Workspace.owner_id)
            .outerjoin(
                ChatTurn,
                (ChatTurn.workspace_id == Workspace.id) & (ChatTurn.created_at >= day_start_utc),
            )
            .where(Workspace.soft_deleted_at.is_(None))
            .group_by(Workspace.id, Workspace.title, User.email)
            .order_by(func.coalesce(func.sum(ChatTurn.tokens_in), 0).desc())
        ).all()
    )

    recent_events = list(
        session.scalars(
            select(WorkspaceEvent)
            .where(WorkspaceEvent.kind.in_(["cloud_turn_failed", "cloud_turn_cancelled"]))
            .order_by(WorkspaceEvent.created_at.desc())
            .limit(50)
        ).all()
    )

    return {
        "drafting_enabled": settings.drafting_enabled,
        "global_cap": global_cap,
        "per_workspace": per_workspace_rows,
        "recent_events": recent_events,
    }


class GlobalCapOut(BaseModel):
    allowed: bool
    used_in: int
    used_out: int
    cap_in: int
    cap_out: int
    reason: str | None


class PerWorkspaceOut(BaseModel):
    id: uuid.UUID
    title: str
    owner_email: str
    used_in: int
    used_out: int


class RecentEventOut(BaseModel):
    created_at: datetime
    workspace_id: uuid.UUID
    kind: str
    payload: dict


class AdminDraftingOut(BaseModel):
    drafting_enabled: bool
    global_cap: GlobalCapOut
    per_workspace: list[PerWorkspaceOut]
    recent_events: list[RecentEventOut]


@router.get("/admin/drafting", response_model=AdminDraftingOut)
def admin_drafting(
    request: Request,
    admin: User = Depends(require_admin),  # noqa: B008
):
    settings = request.app.state.settings
    with request.app.state.session_factory() as session:
        ctx = _drafting_panel_context(session, settings)
        cap = ctx["global_cap"]
        per_workspace = [
            PerWorkspaceOut(id=row.id, title=row.title, owner_email=row.owner_email,
                            used_in=row.used_in, used_out=row.used_out)
            for row in ctx["per_workspace"]
        ]
        recent_events = [
            RecentEventOut(created_at=e.created_at, workspace_id=e.workspace_id,
                           kind=e.kind, payload=e.payload)
            for e in ctx["recent_events"]
        ]
        result = AdminDraftingOut(
            drafting_enabled=ctx["drafting_enabled"],
            global_cap=GlobalCapOut(allowed=cap.allowed, used_in=cap.used_in, used_out=cap.used_out,
                                    cap_in=cap.cap_in, cap_out=cap.cap_out, reason=cap.reason),
            per_workspace=per_workspace,
            recent_events=recent_events,
        )
    return result
```

Note: the Pydantic lists and `result` are built INSIDE the `with` block so the ORM `WorkspaceEvent` attributes resolve before the session closes (a `DetachedInstanceError` guard).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_admin_drafting.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/api/admin.py tests/test_api_admin_drafting.py
git commit -m "feat(api): GET /api/admin/drafting read-model (admin Slice 2)"
```

---

### Task 3: Backend — admin ops mutations

The four ops actions, translated from Jinja `303 ?ok=/?err=` redirects to JSON. Trigger actions return `{triggered: bool}` (rerank `{enqueued}`) — an unconfigured/unavailable scheduler is an infra STATE, not a client error. Genuine client errors stay envelope errors (`404` unknown slug/user).

**Files:**
- Modify: `src/call_manager/web/api/admin.py` (add 4 endpoints + `_scheduler` helper + models)
- Test: `tests/test_api_admin_ops.py`

**Interfaces:**
- Consumes: `HTTPException` (fastapi); `backfill_profile(*, session_factory, profile_id) -> int` (`call_manager.ranking.backfill`); `trigger_job_now(scheduler, job_id) -> bool` (`call_manager.scheduler`); `Source, Profile, User` models; `app.state.scheduler` (may be absent → `None`).
- Produces: `POST /api/admin/rerank` → `RerankOut{enqueued:int}`; `POST /api/admin/digest/send` → `TriggeredOut{triggered:bool}`; `POST /api/admin/digest/reenable` body `ReenableIn{user_id:int}` → `AdminActionOut{id:int}` (`404` unknown); `POST /api/admin/sources/{slug}/fetch` → `TriggeredOut{triggered:bool}` (`404` unknown slug).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_admin_ops.py`:

```python
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.db.models import Call, RankingJob, Source, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _admin(app, sf, *, email="root@ki.se"):
    with sf() as s:
        u = User(status="active", email=email, is_admin=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid


class _FakeJob:
    def __init__(self):
        self.next_run_time = "UNSET"

    def modify(self, **kwargs):
        self.next_run_time = kwargs["next_run_time"]


class _FakeScheduler:
    def __init__(self, jobs):
        self._jobs = dict(jobs)

    def get_job(self, job_id):
        return self._jobs.get(job_id)

    def shutdown(self, wait=True):
        return None


def test_rerank_enqueues_for_active_profiles(monkeypatch, pg_url, pg_session_factory, profile_factory):
    pid, _ = profile_factory()
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add(Call(source_id=src.id, external_id="R", url="https://x/r", title="T",
                   published_at=datetime.now(UTC) - timedelta(days=10)))
        s.commit()
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    r = c.post("/api/admin/rerank")
    assert r.status_code == 200
    assert r.json() == {"enqueued": 1}  # one ACTIVE PROFILE, not job count
    with pg_session_factory() as s:
        assert len(s.execute(select(RankingJob)).scalars().all()) == 1


def test_rerank_requires_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="na@ki.se", is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    assert c.post("/api/admin/rerank").status_code == 403


def test_send_digest_triggered(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    with c:
        real = app.state.scheduler
        app.state.scheduler = _FakeScheduler({"digest:weekly": _FakeJob()})
        try:
            r = c.post("/api/admin/digest/send")
        finally:
            app.state.scheduler = real
    assert r.status_code == 200 and r.json() == {"triggered": True}


def test_send_digest_unconfigured(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    with c:
        real = app.state.scheduler
        app.state.scheduler = _FakeScheduler({})
        try:
            r = c.post("/api/admin/digest/send")
        finally:
            app.state.scheduler = real
    assert r.status_code == 200 and r.json() == {"triggered": False}


def test_reenable_clears_and_returns_id(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    with pg_session_factory() as s:
        u = User(status="active", email="bounced@ki.se", digest_disabled_at=datetime.now(UTC))
        s.add(u)
        s.commit()
        s.refresh(u)
        tid = u.id
    r = c.post("/api/admin/digest/reenable", json={"user_id": tid})
    assert r.status_code == 200 and r.json() == {"id": tid}
    with pg_session_factory() as s:
        assert s.get(User, tid).digest_disabled_at is None


def test_reenable_already_enabled_noop(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    with pg_session_factory() as s:
        u = User(status="active", email="fine@ki.se", digest_disabled_at=None)
        s.add(u)
        s.commit()
        s.refresh(u)
        tid = u.id
    assert c.post("/api/admin/digest/reenable", json={"user_id": tid}).status_code == 200


def test_reenable_unknown_404(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    r = c.post("/api/admin/digest/reenable", json={"user_id": 999999})
    assert r.status_code == 404


def test_fetch_source_triggered(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    with c:
        real = app.state.scheduler
        app.state.scheduler = _FakeScheduler({"ingest:vinnova": _FakeJob()})
        try:
            r = c.post("/api/admin/sources/vinnova/fetch")
        finally:
            app.state.scheduler = real
    assert r.status_code == 200 and r.json() == {"triggered": True}


def test_fetch_source_job_missing(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    with c:
        real = app.state.scheduler
        app.state.scheduler = _FakeScheduler({})
        try:
            r = c.post("/api/admin/sources/vinnova/fetch")
        finally:
            app.state.scheduler = real
    assert r.status_code == 200 and r.json() == {"triggered": False}


def test_fetch_unknown_slug_404(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    assert c.post("/api/admin/sources/does-not-exist/fetch").status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_admin_ops.py -q`
Expected: FAIL — endpoints `404`.

- [ ] **Step 3: Add the ops endpoints to `web/api/admin.py`**

Add to the fastapi import: `HTTPException`. Add imports `from call_manager.ranking.backfill import BACKFILL_DAYS, backfill_profile` (extend the existing line) and `from call_manager.scheduler import trigger_job_now`. Append:

```python
class RerankOut(BaseModel):
    enqueued: int


class TriggeredOut(BaseModel):
    triggered: bool


class ReenableIn(BaseModel):
    user_id: int


class AdminActionOut(BaseModel):
    id: int


def _scheduler(request: Request):
    """The app's APScheduler, or None if absent (e.g. before lifespan)."""
    return getattr(request.app.state, "scheduler", None)


@router.post("/admin/rerank", response_model=RerankOut)
def admin_rerank(
    request: Request,
    admin: User = Depends(require_admin),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        active_profile_ids = (
            session.execute(select(Profile.id).where(Profile.is_active.is_(True))).scalars().all()
        )
    for pid in active_profile_ids:
        backfill_profile(session_factory=session_factory, profile_id=pid)
    return RerankOut(enqueued=len(active_profile_ids))


@router.post("/admin/digest/send", response_model=TriggeredOut)
def admin_send_digest(
    request: Request,
    admin: User = Depends(require_admin),  # noqa: B008
):
    return TriggeredOut(triggered=trigger_job_now(_scheduler(request), "digest:weekly"))


@router.post("/admin/digest/reenable", response_model=AdminActionOut)
def admin_reenable_user(
    request: Request,
    body: ReenableIn,
    admin: User = Depends(require_admin),  # noqa: B008
):
    with request.app.state.session_factory() as session:
        target = session.get(User, body.user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="unknown user")
        target.digest_disabled_at = None
        session.commit()
    return AdminActionOut(id=body.user_id)


@router.post("/admin/sources/{slug}/fetch", response_model=TriggeredOut)
def admin_fetch_source(
    slug: str,
    request: Request,
    admin: User = Depends(require_admin),  # noqa: B008
):
    with request.app.state.session_factory() as session:
        src = session.execute(select(Source).where(Source.slug == slug)).scalar_one_or_none()
    if src is None:
        raise HTTPException(status_code=404, detail="unknown source")
    return TriggeredOut(triggered=trigger_job_now(_scheduler(request), f"ingest:{slug}"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_admin_ops.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/api/admin.py tests/test_api_admin_ops.py
git commit -m "feat(api): admin ops mutations rerank/digest/fetch (admin Slice 2)"
```

---

### Task 4: Backend — golden-set `/api` module

Golden-set read + add + delete in a new module, reusing the unchanged `ranking/golden_set.py` service. The add path 400s on bad score; on `IntegrityError` it `session.rollback()`s (or the session is poisoned) then branches on SQLSTATE — FK violation (`23503`)→`400` "unknown call or profile", unique-pair violation (`23505`, the `uq_golden_set_triples_pair` constraint)→`409` "triple already exists"; any other constraint re-raises (sanitized 500). Both are parity improvements (Jinja 500s on both). Delete is `204` idempotent.

**Files:**
- Create: `src/call_manager/web/api/admin_golden_set.py`
- Modify: `src/call_manager/web/api/__init__.py` (register `admin_golden_set.router`)
- Test: `tests/test_api_admin_golden_set.py`

**Interfaces:**
- Consumes: `require_admin`; `User` model; from `call_manager.ranking.golden_set`: `add_triple(session, *, call_id, profile_id, expected_score, note=None, created_by=None) -> int` (calls `session.flush()` internally — FK violation raises here), `delete_triple(session, *, triple_id) -> None` (no-op if absent), `list_triples(session) -> list[TripleRow]`, `candidates_dismissed_high_score/_cosine_disagreement/_escalation_disagreement(session) -> list[CandidateRow]`. `TripleRow` fields: `id, call_id, profile_id, expected_score, note, call_title, profile_name`. `CandidateRow` fields: `call_id, profile_id, call_title, profile_name, aggregate_score, embed_similarity, reason`.
- Produces: `GET /api/admin/golden-set` → `AdminGoldenSetOut`; `POST /api/admin/golden-set` body `AddTripleIn` → `201 TripleCreatedOut{id}` (`400` bad score / FK; `409` duplicate pair); `DELETE /api/admin/golden-set/{triple_id}` → `204`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_admin_golden_set.py`:

```python
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.db.models import (
    Call,
    CallRanking,
    GoldenSetTriple,
    Profile,
    ProfileKind,
    Source,
    User,
)
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _admin(app, sf, *, email="root@ki.se"):
    with sf() as s:
        u = User(status="active", email=email, is_admin=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid


def _seed_call_profile(sf, uid):
    with sf() as s:
        p = Profile(name="P", kind=ProfileKind.ORGANIZATIONAL_UNIT, description="d",
                    structured={}, created_by=uid)
        s.add(p)
        s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(source_id=src.id, external_id="c1", url="https://x/1", title="C1",
                 published_at=datetime.now(UTC))
        s.add(c)
        s.commit()
        return c.id, p.id


def test_golden_set_requires_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="alice@ki.se", is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    assert c.get("/api/admin/golden-set").status_code == 403


def test_add_then_read_then_delete(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, uid = _admin(app, pg_session_factory)
    cid, pid = _seed_call_profile(pg_session_factory, uid)
    r = c.post("/api/admin/golden-set",
               json={"call_id": cid, "profile_id": pid, "expected_score": 65, "note": "x"})
    assert r.status_code == 201
    tid = r.json()["id"]
    body = c.get("/api/admin/golden-set").json()
    row = next(t for t in body["triples"] if t["id"] == tid)
    assert row["call_title"] == "C1" and row["expected_score"] == 65 and row["note"] == "x"
    assert set(row) == {"id", "call_id", "profile_id", "expected_score", "note",
                        "call_title", "profile_name"}
    assert c.request("DELETE", f"/api/admin/golden-set/{tid}").status_code == 204
    with pg_session_factory() as s:
        assert s.execute(select(GoldenSetTriple)).first() is None


def test_add_rejects_out_of_range_score(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, uid = _admin(app, pg_session_factory)
    cid, pid = _seed_call_profile(pg_session_factory, uid)
    r = c.post("/api/admin/golden-set",
               json={"call_id": cid, "profile_id": pid, "expected_score": 150})
    assert r.status_code == 400


def test_add_unknown_fk_is_400_not_500(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    r = c.post("/api/admin/golden-set",
               json={"call_id": 999999, "profile_id": 999999, "expected_score": 50})
    assert r.status_code == 400
    assert "unknown call or profile" in r.json()["error"]["message"]


def test_add_duplicate_pair_is_409(monkeypatch, pg_url, pg_session_factory):
    # A second triple for the same (call, profile) hits uq_golden_set_triples_pair.
    # It must be a 409 with a "duplicate" message — NOT the FK "unknown" 400.
    app = _setup(monkeypatch, pg_url)
    c, uid = _admin(app, pg_session_factory)
    cid, pid = _seed_call_profile(pg_session_factory, uid)
    first = c.post("/api/admin/golden-set",
                   json={"call_id": cid, "profile_id": pid, "expected_score": 65})
    assert first.status_code == 201
    dup = c.post("/api/admin/golden-set",
                 json={"call_id": cid, "profile_id": pid, "expected_score": 70})
    assert dup.status_code == 409
    assert "already exists" in dup.json()["error"]["message"]


def test_delete_absent_is_204_idempotent(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    assert c.request("DELETE", "/api/admin/golden-set/999999").status_code == 204


def test_escalation_disagreement_candidate_appears(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, uid = _admin(app, pg_session_factory)
    with pg_session_factory() as s:
        p = Profile(name="P", kind=ProfileKind.ORGANIZATIONAL_UNIT, description="d",
                    structured={}, created_by=uid)
        s.add(p)
        s.flush()
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        call = Call(source_id=src.id, external_id="esc", url="https://x/e", title="EscCall",
                    published_at=datetime.now(UTC))
        s.add(call)
        s.flush()
        s.add(CallRanking(call_id=call.id, profile_id=p.id, aggregate_score=20,
                          aggregate_reason="eligibility", rationale="r",
                          model="claude-sonnet-4-6", prompt_version="v5.0", raw_response={},
                          ranked_at=datetime.now(UTC), escalated=True, escalated_from_score=80))
        s.commit()
    cands = c.get("/api/admin/golden-set").json()["candidates"]
    esc = next(x for x in cands if x["call_title"] == "EscCall")
    assert esc["reason"] == "escalation_disagreement"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_admin_golden_set.py -q`
Expected: FAIL — endpoints `404`.

- [ ] **Step 3: Create the module**

Create `src/call_manager/web/api/admin_golden_set.py`:

```python
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from call_manager.auth.dependencies import require_admin
from call_manager.db.models import User
from call_manager.ranking.golden_set import (
    add_triple,
    candidates_cosine_disagreement,
    candidates_dismissed_high_score,
    candidates_escalation_disagreement,
    delete_triple,
    list_triples,
)

router = APIRouter()


class TripleOut(BaseModel):
    id: int
    call_id: int
    profile_id: int
    expected_score: int
    note: str | None
    call_title: str
    profile_name: str


class CandidateOut(BaseModel):
    call_id: int
    profile_id: int
    call_title: str
    profile_name: str
    aggregate_score: int
    embed_similarity: float | None
    reason: str


class AdminGoldenSetOut(BaseModel):
    triples: list[TripleOut]
    candidates: list[CandidateOut]


class AddTripleIn(BaseModel):
    call_id: int
    profile_id: int
    expected_score: int
    note: str | None = None


class TripleCreatedOut(BaseModel):
    id: int


@router.get("/admin/golden-set", response_model=AdminGoldenSetOut)
def golden_set_read(
    request: Request,
    admin: User = Depends(require_admin),  # noqa: B008
):
    with request.app.state.session_factory() as session:
        triples = list_triples(session)
        candidates = (
            candidates_dismissed_high_score(session)
            + candidates_cosine_disagreement(session)
            + candidates_escalation_disagreement(session)
        )
    return AdminGoldenSetOut(
        triples=[TripleOut(**asdict(t)) for t in triples],
        candidates=[CandidateOut(**asdict(c)) for c in candidates],
    )


@router.post("/admin/golden-set", response_model=TripleCreatedOut, status_code=201)
def golden_set_add(
    request: Request,
    body: AddTripleIn,
    admin: User = Depends(require_admin),  # noqa: B008
):
    if not 0 <= body.expected_score <= 100:
        raise HTTPException(status_code=400, detail="expected_score must be 0-100")
    note = body.note.strip() or None if body.note else None
    with request.app.state.session_factory() as session:
        try:
            triple_id = add_triple(
                session,
                call_id=body.call_id,
                profile_id=body.profile_id,
                expected_score=body.expected_score,
                note=note,
                created_by=admin.id,
            )
            session.commit()
        except IntegrityError as e:
            session.rollback()
            # Two constraints can fire here: the call/profile FKs vs
            # uq_golden_set_triples_pair. (The 0-100 CheckConstraint is pre-guarded
            # above.) Branch on SQLSTATE so a duplicate isn't mislabeled "unknown".
            sqlstate = getattr(getattr(e, "orig", None), "sqlstate", None)
            if sqlstate == "23505":  # unique_violation -> duplicate (call, profile)
                raise HTTPException(
                    status_code=409, detail="triple already exists for this call and profile"
                ) from None
            if sqlstate == "23503":  # foreign_key_violation -> unknown call/profile
                raise HTTPException(status_code=400, detail="unknown call or profile") from None
            raise  # unexpected constraint -> sanitized 500
    return TripleCreatedOut(id=triple_id)


@router.delete("/admin/golden-set/{triple_id}", status_code=204)
def golden_set_delete(
    request: Request,
    triple_id: int,
    admin: User = Depends(require_admin),  # noqa: B008
):
    with request.app.state.session_factory() as session:
        delete_triple(session, triple_id=triple_id)
        session.commit()
    return Response(status_code=204)
```

- [ ] **Step 4: Register the router**

Modify `src/call_manager/web/api/__init__.py` — add `admin_golden_set` to the import block (after `admin_users`) and add `router.include_router(admin_golden_set.router)` (place it right after the `admin_users` include).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_admin_golden_set.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/api/admin_golden_set.py src/call_manager/web/api/__init__.py tests/test_api_admin_golden_set.py
git commit -m "feat(api): golden-set read/add/delete (admin Slice 2)"
```

---

### Task 5: Frontend — regen OpenAPI types + dashboard/drafting/ops hooks

With all four backend reads/mutations live, regenerate the TS schema, then add the dashboard+drafting reads and the four ops mutations as TanStack Query hooks. (Golden-set hooks are Task 6 — the schema regen here covers them too, since Task 4 already added those paths.)

**Files:**
- Modify: `frontend/src/api/schema.gen.ts` (regenerated — do not hand-edit)
- Create: `frontend/src/api/adminDashboard.ts`
- Test: `frontend/src/api/adminDashboard.test.tsx`

**Interfaces:**
- Consumes: `api, unwrap` (`./client`); `components` (`./schema.gen`) — schemas `AdminDashboardOut`, `AdminDraftingOut`.
- Produces: `useAdminDashboard()`, `useAdminDrafting()` (both `retry:false`); `useRerank()`, `useSendDigest()`, `useReenableDigestUser()`, `useFetchSource()` (all invalidate `["admin-dashboard"]`); types `AdminDashboard`, `AdminDrafting`.

- [ ] **Step 1: Regenerate the OpenAPI types**

Run: `cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`
Expected: `frontend/src/api/schema.gen.ts` updates to include `/api/admin/dashboard`, `/api/admin/drafting`, `/api/admin/rerank`, `/api/admin/digest/send`, `/api/admin/digest/reenable`, `/api/admin/sources/{slug}/fetch`, `/api/admin/golden-set`, and schemas `AdminDashboardOut`/`AdminDraftingOut`/`AdminGoldenSetOut`/etc. (Verify: `grep -c "admin/dashboard" frontend/src/api/schema.gen.ts` ≥ 1.)

- [ ] **Step 2: Write the failing hook tests**

Create `frontend/src/api/adminDashboard.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import {
  useAdminDashboard,
  useAdminDrafting,
  useFetchSource,
  useReenableDigestUser,
  useRerank,
  useSendDigest,
} from "./adminDashboard";

afterEach(() => {
  vi.restoreAllMocks();
});

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

it("useAdminDashboard GETs the dashboard", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: {}, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useAdminDashboard(), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith("/api/admin/dashboard", {});
});

it("useAdminDrafting GETs the drafting panel", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: {}, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useAdminDrafting(), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith("/api/admin/drafting", {});
});

it("useRerank POSTs and invalidates the dashboard", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { enqueued: 2 }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useRerank(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync();
  expect(post).toHaveBeenCalledWith("/api/admin/rerank", {});
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-dashboard"] });
});

it("useSendDigest POSTs digest/send and invalidates the dashboard", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { triggered: true }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useSendDigest(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync();
  expect(post).toHaveBeenCalledWith("/api/admin/digest/send", {});
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-dashboard"] });
});

it("useReenableDigestUser POSTs the user_id body and invalidates the dashboard", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 9 }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useReenableDigestUser(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ user_id: 9 });
  expect(post).toHaveBeenCalledWith("/api/admin/digest/reenable", { body: { user_id: 9 } });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-dashboard"] });
});

it("useFetchSource POSTs the slug path param and invalidates the dashboard", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { triggered: true }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useFetchSource(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ slug: "vinnova" });
  expect(post).toHaveBeenCalledWith("/api/admin/sources/{slug}/fetch", {
    params: { path: { slug: "vinnova" } },
  });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-dashboard"] });
});
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd frontend && pnpm vitest run src/api/adminDashboard.test.tsx`
Expected: FAIL — `./adminDashboard` does not exist.

- [ ] **Step 4: Create the hooks**

Create `frontend/src/api/adminDashboard.ts`:

```ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type AdminDashboard = components["schemas"]["AdminDashboardOut"];
export type AdminDrafting = components["schemas"]["AdminDraftingOut"];

export function useAdminDashboard() {
  return useQuery({
    queryKey: ["admin-dashboard"],
    queryFn: async () => unwrap(await api.GET("/api/admin/dashboard", {})),
    retry: false,
  });
}

export function useAdminDrafting() {
  return useQuery({
    queryKey: ["admin-drafting"],
    queryFn: async () => unwrap(await api.GET("/api/admin/drafting", {})),
    retry: false,
  });
}

export function useRerank() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/admin/rerank", {})),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-dashboard"] }),
  });
}

export function useSendDigest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/admin/digest/send", {})),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-dashboard"] }),
  });
}

export function useReenableDigestUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { user_id: number }) =>
      unwrap(await api.POST("/api/admin/digest/reenable", { body: { user_id: v.user_id } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-dashboard"] }),
  });
}

export function useFetchSource() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { slug: string }) =>
      unwrap(await api.POST("/api/admin/sources/{slug}/fetch", { params: { path: { slug: v.slug } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-dashboard"] }),
  });
}
```

- [ ] **Step 5: Run tests + typecheck**

Run: `cd frontend && pnpm vitest run src/api/adminDashboard.test.tsx && pnpm exec tsc --noEmit`
Expected: tests PASS, tsc 0 errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/schema.gen.ts frontend/src/api/adminDashboard.ts frontend/src/api/adminDashboard.test.tsx
git commit -m "feat(spa): admin dashboard/drafting/ops hooks + OpenAPI regen (admin Slice 2)"
```

---

### Task 6: Frontend — golden-set hooks

**Files:**
- Create: `frontend/src/api/adminGoldenSet.ts`
- Test: `frontend/src/api/adminGoldenSet.test.tsx`

**Interfaces:**
- Consumes: `api, unwrap`; schema `AdminGoldenSetOut` (already in `schema.gen.ts` from Task 5's regen).
- Produces: `useAdminGoldenSet()` (`retry:false`); `useAddTriple()`, `useDeleteTriple()` (both invalidate `["admin-golden-set"]`); type `AdminGoldenSet`.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/api/adminGoldenSet.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import { useAddTriple, useAdminGoldenSet, useDeleteTriple } from "./adminGoldenSet";

afterEach(() => {
  vi.restoreAllMocks();
});

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

it("useAdminGoldenSet GETs the golden set", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: { triples: [], candidates: [] }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useAdminGoldenSet(), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith("/api/admin/golden-set", {});
});

it("useAddTriple POSTs the body and invalidates", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 1 }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useAddTriple(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ call_id: 3, profile_id: 4, expected_score: 65, note: "x" });
  expect(post).toHaveBeenCalledWith("/api/admin/golden-set", {
    body: { call_id: 3, profile_id: 4, expected_score: 65, note: "x" },
  });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-golden-set"] });
});

it("useDeleteTriple DELETEs the path param and invalidates the golden set", async () => {
  const del = vi.spyOn(clientMod.api, "DELETE").mockResolvedValue({ data: undefined, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useDeleteTriple(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ triple_id: 7 });
  expect(del).toHaveBeenCalledWith("/api/admin/golden-set/{triple_id}", {
    params: { path: { triple_id: 7 } },
  });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-golden-set"] });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && pnpm vitest run src/api/adminGoldenSet.test.tsx`
Expected: FAIL — module missing.

- [ ] **Step 3: Create the hooks**

Create `frontend/src/api/adminGoldenSet.ts`:

```ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type AdminGoldenSet = components["schemas"]["AdminGoldenSetOut"];

export function useAdminGoldenSet() {
  return useQuery({
    queryKey: ["admin-golden-set"],
    queryFn: async () => unwrap(await api.GET("/api/admin/golden-set", {})),
    retry: false,
  });
}

export function useAddTriple() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { call_id: number; profile_id: number; expected_score: number; note?: string }) =>
      unwrap(await api.POST("/api/admin/golden-set", { body: v })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-golden-set"] }),
  });
}

export function useDeleteTriple() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { triple_id: number }) =>
      unwrap(await api.DELETE("/api/admin/golden-set/{triple_id}", { params: { path: { triple_id: v.triple_id } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-golden-set"] }),
  });
}
```

- [ ] **Step 4: Run tests + typecheck**

Run: `cd frontend && pnpm vitest run src/api/adminGoldenSet.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS, tsc 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/adminGoldenSet.ts frontend/src/api/adminGoldenSet.test.tsx
git commit -m "feat(spa): admin golden-set hooks (admin Slice 2)"
```

---

### Task 7: Frontend — `AdminDashboard` component

Read-only panels + four ops action buttons (rerank with a confirm step), inline result banners reflecting `{triggered}`/`{enqueued}` with the human messages ported VERBATIM from the Jinja `_ok_msgs`/`_err_msgs`. With `retry:false`, a single failed sub-query 500s the whole read-model, so the page renders a CLEAN error state (not a blank shell). Tested in isolation (not via the router) with mocked hooks.

**Files:**
- Create: `frontend/src/routes/AdminDashboard.tsx`
- Create: `frontend/src/routes/AdminDashboard.module.css`
- Test: `frontend/src/routes/AdminDashboard.test.tsx`

**Interfaces:**
- Consumes: `useAdminDashboard, useRerank, useSendDigest, useReenableDigestUser, useFetchSource` (`../api/adminDashboard`).
- Produces: `AdminDashboard` (default-free named export `export function AdminDashboard()`), routed in Task 10.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/routes/AdminDashboard.test.tsx`:

```tsx
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { AdminDashboard } from "./AdminDashboard";

afterEach(() => {
  vi.restoreAllMocks();
});

const DASH = {
  sources: [{ name: "Vinnova", slug: "vinnova", last_fetched_at: null, call_count: 3 }],
  queue_counts: { pending: 2, running: 0, done: 5, failed: 1 },
  rerank_estimate: 6,
  active_profiles_count: 2,
  recent_calls_count: 3,
  backfill_days: 90,
  digest: {
    last_sent_at: null,
    runs_this_week: 0,
    disabled_users: 1,
    disabled_user_rows: [{ id: 9, email: "bounced@ki.se", disabled_at: "2026-06-01T00:00:00Z" }],
  },
  embedding_coverage: {
    call_embedded: 1, call_total: 3, call_stale: 2,
    profile_embedded: 1, profile_total: 2, profile_stale: 1, current_model: "bge-m3",
  },
  golden_set_report: null,
  calibration_warning: null,
};

function mockDash(data: unknown = DASH) {
  return vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data, error: undefined } as never);
}

it("renders source health, queue counts and coverage", async () => {
  mockDash();
  renderWithProviders(<AdminDashboard />);
  expect(await screen.findByText("Vinnova")).toBeInTheDocument();
  expect(screen.getByText("bounced@ki.se")).toBeInTheDocument();
  expect(screen.getByText(/1\/3 embedded/)).toBeInTheDocument();
  expect(screen.getByText(/up to ~6/)).toBeInTheDocument();
});

it("renders a clean error state when the read-model fails", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: undefined, error: { error: { code: "internal_error", message: "boom" } },
    response: { status: 500 },
  } as never);
  renderWithProviders(<AdminDashboard />);
  expect(await screen.findByRole("alert")).toHaveTextContent(/couldn't load/i);
});

it("fetch-now posts the slug and shows the ok banner", async () => {
  mockDash();
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { triggered: true }, error: undefined } as never);
  renderWithProviders(<AdminDashboard />);
  await userEvent.click(await screen.findByRole("button", { name: /fetch now/i }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/admin/sources/{slug}/fetch", {
      params: { path: { slug: "vinnova" } },
    }),
  );
  expect(await screen.findByText(/fetch triggered/i)).toBeInTheDocument();
});

it("fetch-now shows the err banner when not triggered", async () => {
  mockDash();
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { triggered: false }, error: undefined } as never);
  renderWithProviders(<AdminDashboard />);
  await userEvent.click(await screen.findByRole("button", { name: /fetch now/i }));
  expect(await screen.findByText(/couldn't trigger the fetch/i)).toBeInTheDocument();
});

it("rerank confirms before posting", async () => {
  mockDash();
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { enqueued: 2 }, error: undefined } as never);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  renderWithProviders(<AdminDashboard />);
  await userEvent.click(await screen.findByRole("button", { name: /re-rank everything/i }));
  expect(confirm).toHaveBeenCalled();
  expect(post).not.toHaveBeenCalled();  // declined -> no POST
});

it("reenable posts the user id", async () => {
  mockDash();
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 9 }, error: undefined } as never);
  renderWithProviders(<AdminDashboard />);
  await userEvent.click(await screen.findByRole("button", { name: /re-enable/i }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/admin/digest/reenable", { body: { user_id: 9 } }),
  );
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && pnpm vitest run src/routes/AdminDashboard.test.tsx`
Expected: FAIL — component missing.

- [ ] **Step 3: Create the component + styles**

Create `frontend/src/routes/AdminDashboard.tsx`:

```tsx
import { useState } from "react";
import { Link } from "react-router-dom";

import {
  useAdminDashboard,
  useFetchSource,
  useReenableDigestUser,
  useRerank,
  useSendDigest,
} from "../api/adminDashboard";
import styles from "./AdminDashboard.module.css";

// Ported verbatim from admin.html _ok_msgs / _err_msgs (admin.html:23-31).
const OK_FETCH = "Fetch triggered — the source will refresh shortly.";
const OK_DIGEST = "Weekly digest triggered — it will send shortly.";
const OK_REENABLE = "User re-enabled for the weekly digest.";
const OK_RERANK = "Re-ranking enqueued for all active profiles.";
const ERR_FETCH = "Couldn't trigger the fetch — the scheduler or this source's job is unavailable.";
const ERR_DIGEST = "Digest isn't configured (no email backend), so it can't be sent.";

type Banner = { kind: "ok" | "warn"; text: string } | null;

const fmtDt = (s: string | null) => (s ? new Date(s).toLocaleString() : null);

export function AdminDashboard() {
  const { data, isLoading, isError } = useAdminDashboard();
  const [banner, setBanner] = useState<Banner>(null);
  const rerank = useRerank();
  const sendDigest = useSendDigest();
  const reenable = useReenableDigestUser();
  const fetchSource = useFetchSource();

  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <main role="alert"><p>Couldn't load the admin dashboard.</p></main>;

  const d = data;

  const onFetch = (slug: string, name: string) =>
    fetchSource.mutate({ slug }, {
      onSuccess: (r) =>
        setBanner(r.triggered
          ? { kind: "ok", text: `${OK_FETCH} (${name})` }
          : { kind: "warn", text: `${ERR_FETCH} (${name})` }),
    });

  const onRerank = () => {
    if (!window.confirm(
      `This enqueues up to ~${d.rerank_estimate} ranking jobs and may take a while. Continue?`
    )) return;
    rerank.mutate(undefined, { onSuccess: () => setBanner({ kind: "ok", text: OK_RERANK }) });
  };

  const onSendDigest = () =>
    sendDigest.mutate(undefined, {
      onSuccess: (r) =>
        setBanner(r.triggered ? { kind: "ok", text: OK_DIGEST } : { kind: "warn", text: ERR_DIGEST }),
    });

  const onReenable = (userId: number) =>
    reenable.mutate({ user_id: userId },
      { onSuccess: () => setBanner({ kind: "ok", text: OK_REENABLE }) });

  return (
    <main className={styles.page}>
      <h1>Operational health</h1>
      {banner && (
        <div role={banner.kind === "ok" ? "status" : "alert"}
             className={banner.kind === "ok" ? styles.bannerOk : styles.bannerWarn}>
          {banner.text}
        </div>
      )}

      <section className={styles.card}>
        <h2>Source health</h2>
        <table className={styles.table}>
          <thead><tr><th>Source</th><th>Last fetched</th><th>Calls</th><th>Actions</th></tr></thead>
          <tbody>
            {d.sources.map((s) => (
              <tr key={s.slug}>
                <td>{s.name} <code className={styles.muted}>{s.slug}</code></td>
                <td>{fmtDt(s.last_fetched_at) ?? <span className={styles.muted}>never</span>}</td>
                <td><strong>{s.call_count}</strong></td>
                <td>
                  <button type="button" className={styles.secondary} disabled={fetchSource.isPending}
                          onClick={() => onFetch(s.slug, s.name)}>
                    Fetch now
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className={styles.card}>
        <h2>Ranking queue</h2>
        <div className={styles.cards}>
          <div className={styles.summary}><span>Pending</span><strong>{d.queue_counts.pending}</strong></div>
          <div className={styles.summary}><span>Running</span><strong>{d.queue_counts.running}</strong></div>
          <div className={styles.summary}><span>Done</span><strong>{d.queue_counts.done}</strong></div>
          <div className={styles.summary}><span>Failed</span><strong>{d.queue_counts.failed}</strong></div>
        </div>
        <button type="button" className={styles.secondary} disabled={rerank.isPending} onClick={onRerank}>
          Re-rank everything (last {d.backfill_days} days × active profiles)
        </button>
        <p className={styles.muted}>
          Enqueues up to ~{d.rerank_estimate} ranking jobs ({d.active_profiles_count} active
          profiles × {d.recent_calls_count} recent calls; upper bound).
        </p>
      </section>

      {d.calibration_warning && (
        <div role="alert" className={styles.bannerWarn}>
          <strong>Embedding thresholds uncalibrated.</strong> {d.calibration_warning}
        </div>
      )}

      <section className={styles.card}>
        <h2>Embedding coverage</h2>
        <dl className={styles.dl}>
          <dt>Calls</dt>
          <dd><strong>{d.embedding_coverage.call_embedded}/{d.embedding_coverage.call_total} embedded</strong>{" "}
            <span className={styles.muted}>({d.embedding_coverage.call_stale} stale, model <code>{d.embedding_coverage.current_model}</code>)</span></dd>
          <dt>Profiles</dt>
          <dd><strong>{d.embedding_coverage.profile_embedded}/{d.embedding_coverage.profile_total} embedded</strong>{" "}
            <span className={styles.muted}>({d.embedding_coverage.profile_stale} stale, model <code>{d.embedding_coverage.current_model}</code>)</span></dd>
        </dl>
      </section>

      <section className={styles.card}>
        <h2>Digest (weekly)</h2>
        <dl className={styles.dl}>
          <dt>Last digest sent</dt>
          <dd>{fmtDt(d.digest.last_sent_at) ?? <span className={styles.muted}>No digests sent</span>}</dd>
          <dt>Runs this week</dt><dd>{d.digest.runs_this_week}</dd>
          <dt>Disabled users (3+ bounces)</dt><dd>{d.digest.disabled_users}</dd>
        </dl>
        {d.digest.disabled_user_rows.length > 0 ? (
          <ul className={styles.disabled}>
            {d.digest.disabled_user_rows.map((u) => (
              <li key={u.id}>
                {u.email} <span className={styles.muted}>disabled {fmtDt(u.disabled_at)}</span>
                <button type="button" className={styles.secondary} disabled={reenable.isPending}
                        onClick={() => onReenable(u.id)}>Re-enable</button>
              </li>
            ))}
          </ul>
        ) : <p className={styles.muted}>No disabled users.</p>}
        <button type="button" className={styles.secondary} disabled={sendDigest.isPending} onClick={onSendDigest}>
          Send digest now
        </button>
      </section>

      <section className={styles.card}>
        <h2>Ranking quality — Kendall's τ (last run)</h2>
        <p><Link to="/admin/golden-set">Manage golden set →</Link></p>
        {d.golden_set_report ? (
          <dl className={styles.dl}>
            <dt>Curated triples</dt><dd>{d.golden_set_report.n_fixture}</dd>
            <dt>τ (n={d.golden_set_report.n_compared})</dt>
            <dd>{d.golden_set_report.tau !== null ? <strong>{d.golden_set_report.tau.toFixed(3)}</strong> : "—"}</dd>
          </dl>
        ) : (
          <p className={styles.muted}>Golden-set evaluation hasn't run yet — happens Mondays at 06:00 Europe/Stockholm.</p>
        )}
      </section>
    </main>
  );
}
```

Create `frontend/src/routes/AdminDashboard.module.css`:

```css
.page { max-width: 60rem; margin: 0 auto; padding: 1rem 0.5rem 3rem; }
.page h1 { color: var(--app-heading); }
.card { border: 1px solid var(--app-border); border-radius: 8px; padding: 1rem; margin: 1rem 0; }
.card h2 { color: var(--app-heading); font-size: 1rem; margin: 0 0 0.6rem; }
.table { width: 100%; border-collapse: collapse; }
.table th, .table td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--app-border); font-size: 0.9rem; }
.table th { color: var(--app-muted); font-weight: 600; }
.cards { display: flex; flex-wrap: wrap; gap: 0.8rem; margin-bottom: 0.8rem; }
.summary { display: flex; flex-direction: column; gap: 0.2rem; min-width: 6rem; }
.summary span { color: var(--app-muted); font-size: 0.8rem; }
.summary strong { font-size: 1.4rem; }
.dl { display: grid; grid-template-columns: max-content 1fr; gap: 0.3rem 1rem; margin: 0; }
.dl dt { color: var(--app-muted); }
.disabled { list-style: none; padding: 0; display: flex; flex-direction: column; gap: 0.4rem; }
.disabled li { display: flex; gap: 0.6rem; align-items: center; }
.secondary { background: var(--app-sidebar); color: var(--app-text); border: 1px solid var(--app-border); border-radius: 5px; padding: 0.3rem 0.7rem; cursor: pointer; }
.secondary:disabled { opacity: 0.5; cursor: default; }
.muted { color: var(--app-muted); }
.bannerOk { border: 1px solid var(--app-border); border-left: 3px solid var(--app-accent); padding: 0.5rem 0.8rem; border-radius: 4px; margin-bottom: 0.8rem; }
.bannerWarn { border: 1px solid var(--app-border); border-left: 3px solid var(--app-danger, #e5484d); padding: 0.5rem 0.8rem; border-radius: 4px; margin-bottom: 0.8rem; }
```

- [ ] **Step 4: Run tests + typecheck**

Run: `cd frontend && pnpm vitest run src/routes/AdminDashboard.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS, tsc 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/routes/AdminDashboard.tsx frontend/src/routes/AdminDashboard.module.css frontend/src/routes/AdminDashboard.test.tsx
git commit -m "feat(spa): AdminDashboard component (admin Slice 2)"
```

---

### Task 8: Frontend — `AdminDrafting` component

Read-only tables: kill-switch state, global cap, per-workspace usage (rows link to `/cockpit/:id`, now in-SPA), recent failed/cancelled events (payload rendered as text — React auto-escapes, so the Jinja XSS concern is structural).

**Files:**
- Create: `frontend/src/routes/AdminDrafting.tsx`
- Create: `frontend/src/routes/AdminDrafting.module.css`
- Test: `frontend/src/routes/AdminDrafting.test.tsx`

**Interfaces:**
- Consumes: `useAdminDrafting` (`../api/adminDashboard`).
- Produces: `AdminDrafting` (`export function AdminDrafting()`), routed in Task 10.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/routes/AdminDrafting.test.tsx`:

```tsx
import { screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { AdminDrafting } from "./AdminDrafting";

afterEach(() => {
  vi.restoreAllMocks();
});

const DRAFT = {
  drafting_enabled: true,
  global_cap: { allowed: false, used_in: 1500, used_out: 0, cap_in: 1000, cap_out: 500, reason: "input_cap_exceeded" },
  per_workspace: [
    { id: "11111111-1111-1111-1111-111111111111", title: "Big", owner_email: "o@ki.se", used_in: 1500, used_out: 0 },
  ],
  recent_events: [
    { created_at: "2026-06-01T00:00:00Z", workspace_id: "11111111-1111-1111-1111-111111111111", kind: "cloud_turn_failed", payload: { error: "boom_xyz" } },
  ],
};

function mockDraft(data: unknown = DRAFT) {
  return vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data, error: undefined } as never);
}

it("renders kill switch, cap, per-workspace and events", async () => {
  mockDraft();
  renderWithProviders(<AdminDrafting />);
  expect(await screen.findByText(/drafting enabled/i)).toBeInTheDocument();
  // "EXCEEDED" appears twice (input-row cell + overall-status line) — a /EXCEEDED/
  // regex would double-match and throw. Assert each specifically.
  expect(screen.getByText("EXCEEDED")).toBeInTheDocument(); // input-row cell (exact)
  expect(screen.getByText(/Overall status: EXCEEDED/)).toBeInTheDocument();
  const link = screen.getByRole("link", { name: "Big" });
  expect(link).toHaveAttribute("href", "/cockpit/11111111-1111-1111-1111-111111111111");
  expect(screen.getByText(/boom_xyz/)).toBeInTheDocument();
});

it("shows kill switch off", async () => {
  mockDraft({ ...DRAFT, drafting_enabled: false });
  renderWithProviders(<AdminDrafting />);
  expect(await screen.findByText(/drafting disabled/i)).toBeInTheDocument();
});

it("renders a clean error state on failure", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: undefined, error: { error: { code: "internal_error", message: "x" } }, response: { status: 500 },
  } as never);
  renderWithProviders(<AdminDrafting />);
  expect(await screen.findByRole("alert")).toHaveTextContent(/couldn't load/i);
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && pnpm vitest run src/routes/AdminDrafting.test.tsx`
Expected: FAIL — component missing.

- [ ] **Step 3: Create the component + styles**

Create `frontend/src/routes/AdminDrafting.tsx`:

```tsx
import { Link } from "react-router-dom";

import { useAdminDrafting } from "../api/adminDashboard";
import styles from "./AdminDrafting.module.css";

const fmtDt = (s: string) => new Date(s).toLocaleString();

export function AdminDrafting() {
  const { data, isLoading, isError } = useAdminDrafting();
  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <main role="alert"><p>Couldn't load the drafting panel.</p></main>;
  const d = data;
  const cap = d.global_cap;
  return (
    <main className={styles.page}>
      <h1>Drafting (B7/B8) panel</h1>

      <section className={styles.card}>
        <h2>Kill switch</h2>
        <p>{d.drafting_enabled ? <strong>Drafting enabled</strong> : <strong>Drafting disabled</strong>}</p>
        <p className={styles.muted}>Toggle via env <code>DRAFTING_ENABLED=true|false</code> and restart. UI toggling is a backlog item.</p>
      </section>

      <section className={styles.card}>
        <h2>Global daily cap (today, Europe/Stockholm)</h2>
        <table className={styles.table}>
          <thead><tr><th>Dimension</th><th>Used / Cap</th><th>Status</th></tr></thead>
          <tbody>
            <tr><td>Input tokens</td><td>{cap.used_in} / {cap.cap_in}</td>
              <td>{cap.reason === "input_cap_exceeded" ? <strong>EXCEEDED</strong> : "OK"}</td></tr>
            <tr><td>Output tokens</td><td>{cap.used_out} / {cap.cap_out}</td>
              <td>{cap.reason === "output_cap_exceeded" ? <strong>EXCEEDED</strong> : "OK"}</td></tr>
          </tbody>
        </table>
        <p>{cap.allowed ? "Overall status: OK" : <strong>Overall status: EXCEEDED ({cap.reason})</strong>}</p>
      </section>

      <section className={styles.card}>
        <h2>Per-workspace usage today</h2>
        <table className={styles.table}>
          <thead><tr><th>Title</th><th>Owner</th><th>Input</th><th>Output</th></tr></thead>
          <tbody>
            {d.per_workspace.length === 0 ? (
              <tr><td colSpan={4} className={styles.muted}>No workspaces.</td></tr>
            ) : d.per_workspace.map((w) => (
              <tr key={w.id}>
                <td><Link to={`/cockpit/${w.id}`}>{w.title}</Link></td>
                <td>{w.owner_email}</td><td>{w.used_in}</td><td>{w.used_out}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className={styles.card}>
        <h2>Recent failed / cancelled turns (last 50)</h2>
        {d.recent_events.length === 0 ? (
          <p className={styles.muted}>No recent failures.</p>
        ) : (
          <table className={styles.table}>
            <thead><tr><th>When</th><th>Workspace</th><th>Kind</th><th>Detail</th></tr></thead>
            <tbody>
              {d.recent_events.map((e, i) => (
                <tr key={i}>
                  <td>{fmtDt(e.created_at)}</td><td>{e.workspace_id}</td>
                  <td><code>{e.kind}</code></td>
                  <td><pre className={styles.pre}>{JSON.stringify(e.payload, null, 2)}</pre></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </main>
  );
}
```

Create `frontend/src/routes/AdminDrafting.module.css`:

```css
.page { max-width: 60rem; margin: 0 auto; padding: 1rem 0.5rem 3rem; }
.page h1 { color: var(--app-heading); }
.card { border: 1px solid var(--app-border); border-radius: 8px; padding: 1rem; margin: 1rem 0; }
.card h2 { color: var(--app-heading); font-size: 1rem; margin: 0 0 0.6rem; }
.table { width: 100%; border-collapse: collapse; }
.table th, .table td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--app-border); font-size: 0.9rem; vertical-align: top; }
.table th { color: var(--app-muted); font-weight: 600; }
.table a { color: var(--app-link); text-decoration: none; }
.pre { margin: 0; font-size: 0.75rem; white-space: pre-wrap; word-break: break-word; }
.muted { color: var(--app-muted); }
```

- [ ] **Step 4: Run tests + typecheck**

Run: `cd frontend && pnpm vitest run src/routes/AdminDrafting.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS, tsc 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/routes/AdminDrafting.tsx frontend/src/routes/AdminDrafting.module.css frontend/src/routes/AdminDrafting.test.tsx
git commit -m "feat(spa): AdminDrafting component (admin Slice 2)"
```

---

### Task 9: Frontend — `AdminGoldenSet` component

Triples table (+ delete), manual add form, candidates table (+ per-row inline add with a score input).

**Files:**
- Create: `frontend/src/routes/AdminGoldenSet.tsx`
- Create: `frontend/src/routes/AdminGoldenSet.module.css`
- Test: `frontend/src/routes/AdminGoldenSet.test.tsx`

**Interfaces:**
- Consumes: `useAdminGoldenSet, useAddTriple, useDeleteTriple` (`../api/adminGoldenSet`).
- Produces: `AdminGoldenSet` (`export function AdminGoldenSet()`), routed in Task 10.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/routes/AdminGoldenSet.test.tsx`:

```tsx
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { AdminGoldenSet } from "./AdminGoldenSet";

afterEach(() => {
  vi.restoreAllMocks();
});

const GS = {
  triples: [
    { id: 7, call_id: 3, profile_id: 4, expected_score: 65, note: "x", call_title: "C1", profile_name: "P" },
  ],
  candidates: [
    { call_id: 9, profile_id: 4, call_title: "Cand", profile_name: "P", aggregate_score: 20, embed_similarity: 0.71, reason: "escalation_disagreement" },
  ],
};

function mockGs(data: unknown = GS) {
  return vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data, error: undefined } as never);
}

it("renders triples and candidates", async () => {
  mockGs();
  renderWithProviders(<AdminGoldenSet />);
  expect(await screen.findByText("C1")).toBeInTheDocument();
  expect(screen.getByText("Cand")).toBeInTheDocument();
  expect(screen.getByText("escalation_disagreement")).toBeInTheDocument();
});

it("deletes a triple", async () => {
  mockGs();
  const del = vi.spyOn(clientMod.api, "DELETE").mockResolvedValue({ data: undefined, error: undefined } as never);
  renderWithProviders(<AdminGoldenSet />);
  await userEvent.click(await screen.findByRole("button", { name: /delete/i }));
  await waitFor(() =>
    expect(del).toHaveBeenCalledWith("/api/admin/golden-set/{triple_id}", { params: { path: { triple_id: 7 } } }),
  );
});

it("adds a triple from the manual form", async () => {
  mockGs();
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 1 }, error: undefined } as never);
  renderWithProviders(<AdminGoldenSet />);
  await userEvent.type(await screen.findByPlaceholderText("call_id"), "3");
  await userEvent.type(screen.getByPlaceholderText("profile_id"), "4");
  await userEvent.type(screen.getByPlaceholderText(/expected score/i), "65");
  await userEvent.click(screen.getByRole("button", { name: /^add$/i }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/admin/golden-set", {
      body: { call_id: 3, profile_id: 4, expected_score: 65, note: undefined },
    }),
  );
});

it("adds a candidate inline with its score", async () => {
  mockGs();
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 2 }, error: undefined } as never);
  renderWithProviders(<AdminGoldenSet />);
  await userEvent.type(await screen.findByPlaceholderText("0–100"), "80");
  await userEvent.click(screen.getByRole("button", { name: /add candidate/i }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/admin/golden-set", {
      body: { call_id: 9, profile_id: 4, expected_score: 80, note: undefined },
    }),
  );
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && pnpm vitest run src/routes/AdminGoldenSet.test.tsx`
Expected: FAIL — component missing.

- [ ] **Step 3: Create the component + styles**

Create `frontend/src/routes/AdminGoldenSet.tsx`:

```tsx
import { useState } from "react";

import type { AdminGoldenSet as GoldenSet } from "../api/adminGoldenSet";
import { useAddTriple, useAdminGoldenSet, useDeleteTriple } from "../api/adminGoldenSet";
import styles from "./AdminGoldenSet.module.css";

type Candidate = GoldenSet["candidates"][number];

function CandidateRow({ c }: { c: Candidate }) {
  const [score, setScore] = useState("");
  const add = useAddTriple();
  return (
    <tr>
      <td>{c.call_title}</td>
      <td>{c.profile_name}</td>
      <td>{c.aggregate_score}</td>
      <td>{c.embed_similarity !== null ? c.embed_similarity.toFixed(3) : "-"}</td>
      <td><code>{c.reason}</code></td>
      <td>
        <form className={styles.inline} onSubmit={(e) => {
          e.preventDefault();
          add.mutate({ call_id: c.call_id, profile_id: c.profile_id, expected_score: Number(score) },
            { onSuccess: () => setScore("") });
        }}>
          <input type="number" min={0} max={100} placeholder="0–100" required
                 value={score} onChange={(e) => setScore(e.target.value)} />
          <button type="submit" disabled={add.isPending}>Add candidate</button>
        </form>
      </td>
    </tr>
  );
}

export function AdminGoldenSet() {
  const { data, isLoading, isError } = useAdminGoldenSet();
  const del = useDeleteTriple();
  const add = useAddTriple();
  const [callId, setCallId] = useState("");
  const [profileId, setProfileId] = useState("");
  const [score, setScore] = useState("");
  const [note, setNote] = useState("");

  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <main role="alert"><p>Couldn't load the golden set.</p></main>;

  const submitManual = (e: React.FormEvent) => {
    e.preventDefault();
    add.mutate(
      { call_id: Number(callId), profile_id: Number(profileId), expected_score: Number(score), note: note.trim() || undefined },
      { onSuccess: () => { setCallId(""); setProfileId(""); setScore(""); setNote(""); } },
    );
  };

  return (
    <main className={styles.page}>
      <h1>Golden set</h1>

      <section className={styles.card}>
        <h2>Curated triples ({data.triples.length})</h2>
        {data.triples.length === 0 ? <p className={styles.muted}>No triples yet.</p> : (
          <table className={styles.table}>
            <thead><tr><th>Call</th><th>Profile</th><th>Expected score</th><th>Note</th><th></th></tr></thead>
            <tbody>
              {data.triples.map((t) => (
                <tr key={t.id}>
                  <td>{t.call_title}</td><td>{t.profile_name}</td><td>{t.expected_score}</td><td>{t.note ?? ""}</td>
                  <td><button type="button" className={styles.secondary} disabled={del.isPending}
                              onClick={() => del.mutate({ triple_id: t.id })}>Delete</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className={styles.card}>
        <h2>Add triple manually</h2>
        <form className={styles.form} onSubmit={submitManual}>
          <input type="number" placeholder="call_id" required value={callId} onChange={(e) => setCallId(e.target.value)} />
          <input type="number" placeholder="profile_id" required value={profileId} onChange={(e) => setProfileId(e.target.value)} />
          <input type="number" min={0} max={100} placeholder="Expected score (0–100)" required value={score} onChange={(e) => setScore(e.target.value)} />
          <input type="text" placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
          <button type="submit" disabled={add.isPending}>Add</button>
        </form>
      </section>

      <section className={styles.card}>
        <h2>Candidates ({data.candidates.length})</h2>
        {data.candidates.length === 0 ? (
          <p className={styles.muted}>No candidates — no dismissed-high-score, cosine-disagreement, or escalation-disagreement pairs found.</p>
        ) : (
          <table className={styles.table}>
            <thead><tr><th>Call</th><th>Profile</th><th>Score</th><th>Cosine</th><th>Signal</th><th>Add</th></tr></thead>
            <tbody>
              {data.candidates.map((c) => <CandidateRow key={`${c.call_id}-${c.profile_id}`} c={c} />)}
            </tbody>
          </table>
        )}
      </section>
    </main>
  );
}
```

Create `frontend/src/routes/AdminGoldenSet.module.css`:

```css
.page { max-width: 60rem; margin: 0 auto; padding: 1rem 0.5rem 3rem; }
.page h1 { color: var(--app-heading); }
.card { border: 1px solid var(--app-border); border-radius: 8px; padding: 1rem; margin: 1rem 0; }
.card h2 { color: var(--app-heading); font-size: 1rem; margin: 0 0 0.6rem; }
.table { width: 100%; border-collapse: collapse; }
.table th, .table td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--app-border); font-size: 0.9rem; }
.table th { color: var(--app-muted); font-weight: 600; }
.form { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
.inline { display: inline-flex; gap: 0.3rem; align-items: center; }
.form input, .inline input { padding: 0.25rem 0.4rem; border: 1px solid var(--app-border); border-radius: 4px; background: var(--app-sidebar); color: var(--app-text); }
.form button, .inline button { background: var(--app-accent); color: #fff; border: none; border-radius: 5px; padding: 0.3rem 0.7rem; cursor: pointer; }
.secondary { background: var(--app-sidebar); color: var(--app-text); border: 1px solid var(--app-border); border-radius: 5px; padding: 0.3rem 0.7rem; cursor: pointer; }
.secondary:disabled, .form button:disabled, .inline button:disabled { opacity: 0.5; cursor: default; }
.muted { color: var(--app-muted); }
```

- [ ] **Step 4: Run tests + typecheck**

Run: `cd frontend && pnpm vitest run src/routes/AdminGoldenSet.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS, tsc 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/routes/AdminGoldenSet.tsx frontend/src/routes/AdminGoldenSet.module.css frontend/src/routes/AdminGoldenSet.test.tsx
git commit -m "feat(spa): AdminGoldenSet component (admin Slice 2)"
```

---

### Task 10: Frontend — admin navigation (layout route + AdminNav + ActivityBar entry + manifest)

The routing-coupled bundle. Restructure `router.tsx` so every `/admin/*` route nests under one `<AdminLayout>` (which renders `<RequireAdmin>` once + `<AdminNav>` + `<Outlet/>`) — wiring the Slice-1 `AdminUsers`/`AdminPending` pages into in-SPA nav WITHOUT touching their components. Add the three new page routes, the ActivityBar Admin entry (admin-gated), flip the manifest `/admin/users`→`/admin`, regen the route artifacts, and update the drift guard. These land together because `router.test.tsx` is a set-equality check.

> NOTE: bare `/admin`, `/admin/drafting`, `/admin/golden-set` Jinja routes still exist until Task 11, and explicit routes beat the SPA catch-all — so in a running app these three pages keep rendering Jinja between this commit and Task 11. The React pages are reachable only after teardown. This is invisible in any merged state (the SDD runs straight through); their component tests already pass in isolation.

**Files:**
- Create: `frontend/src/routes/AdminLayout.tsx`
- Create: `frontend/src/routes/AdminNav.tsx`
- Create: `frontend/src/routes/AdminNav.module.css`
- Modify: `frontend/src/router.tsx`
- Modify: `frontend/src/shell/nav-items.ts` (add the Admin item)
- Modify: `frontend/src/shell/ActivityBar.tsx` (render Admin item only for admins)
- Modify: `frontend/src/shell/ActivityBar.test.tsx` (APPEND admin-gating tests — the file already has 11 nav/active-route tests that MUST be preserved)
- Modify: `frontend/routes.json` (`spa_routes`: `/admin/users` → `/admin`)
- Modify (regenerated): `frontend/src/routes.gen.ts`, `src/call_manager/web/spa_routes.py`
- Modify: `frontend/src/router.test.tsx` (no code change expected — set-equality still holds; verify)
- Test (create): `frontend/src/routes/AdminNav.test.tsx`

**Interfaces:**
- Consumes: `RequireAdmin` (`./RequireAdmin`); `AdminDashboard, AdminDrafting, AdminGoldenSet, AdminUsers, AdminPendingUsers`; `useSession` (`../api/session`, `capabilities.admin`).
- Produces: `AdminLayout`, `AdminNav`; nested `/admin` route subtree; ActivityBar Admin entry.

- [ ] **Step 1: Create `AdminNav` + its test (failing first)**

Create `frontend/src/routes/AdminNav.test.tsx`:

```tsx
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it } from "vitest";

import { render } from "@testing-library/react";
import { AdminNav } from "./AdminNav";

it("renders all admin section links", () => {
  render(<MemoryRouter><AdminNav /></MemoryRouter>);
  for (const label of ["Operational health", "Drafting", "Golden set", "Users", "Pending"]) {
    expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
  }
  expect(screen.getByRole("link", { name: "Operational health" })).toHaveAttribute("href", "/admin");
  expect(screen.getByRole("link", { name: "Golden set" })).toHaveAttribute("href", "/admin/golden-set");
});
```

Run: `cd frontend && pnpm vitest run src/routes/AdminNav.test.tsx` → FAIL.

Create `frontend/src/routes/AdminNav.tsx`:

```tsx
import { NavLink } from "react-router-dom";

import styles from "./AdminNav.module.css";

const LINKS: { to: string; label: string }[] = [
  { to: "/admin", label: "Operational health" },
  { to: "/admin/drafting", label: "Drafting" },
  { to: "/admin/golden-set", label: "Golden set" },
  { to: "/admin/users", label: "Users" },
  { to: "/admin/users/pending", label: "Pending" },
];

export function AdminNav() {
  return (
    <nav className={styles.nav} aria-label="Admin sections">
      {LINKS.map((l) => (
        <NavLink key={l.to} to={l.to} end={l.to === "/admin"}
                 className={({ isActive }) => (isActive ? `${styles.link} ${styles.active}` : styles.link)}>
          {l.label}
        </NavLink>
      ))}
    </nav>
  );
}
```

Create `frontend/src/routes/AdminNav.module.css`:

```css
.nav { display: flex; flex-wrap: wrap; gap: 0.3rem; padding: 0.6rem 0.5rem; border-bottom: 1px solid var(--app-border); max-width: 60rem; margin: 0 auto; }
.link { color: var(--app-link); text-decoration: none; padding: 0.25rem 0.6rem; border-radius: 5px; font-size: 0.9rem; }
.active { background: var(--app-sidebar); color: var(--app-text); }
```

Run: `cd frontend && pnpm vitest run src/routes/AdminNav.test.tsx` → PASS.

- [ ] **Step 2: Create `AdminLayout`**

Create `frontend/src/routes/AdminLayout.tsx`:

```tsx
import { Outlet } from "react-router-dom";

import { AdminNav } from "./AdminNav";
import { RequireAdmin } from "./RequireAdmin";

// One guard + one subnav for the whole /admin subtree. Slice-1 AdminUsers /
// AdminPendingUsers move under this layout WITHOUT their own RequireAdmin
// wrappers (collapsed here).
export function AdminLayout() {
  return (
    <RequireAdmin>
      <AdminNav />
      <Outlet />
    </RequireAdmin>
  );
}
```

- [ ] **Step 3: Restructure `router.tsx`**

In `frontend/src/router.tsx`: add imports for `AdminLayout`, `AdminDashboard`, `AdminDrafting`, `AdminGoldenSet`; remove the now-unused `RequireAdmin` import (it moves into `AdminLayout`). Replace the two flat admin route entries:

```tsx
      { path: "admin/users", element: <RequireAdmin><AdminUsers /></RequireAdmin> },
      { path: "admin/users/pending", element: <RequireAdmin><AdminPendingUsers /></RequireAdmin> },
```

with a single nested subtree:

```tsx
      {
        path: "admin",
        element: <AdminLayout />,
        children: [
          { index: true, element: <AdminDashboard /> },
          { path: "drafting", element: <AdminDrafting /> },
          { path: "golden-set", element: <AdminGoldenSet /> },
          { path: "users", element: <AdminUsers /> },
          { path: "users/pending", element: <AdminPendingUsers /> },
        ],
      },
```

The import block becomes (keep alphabetical grouping that matches the file's existing style):

```tsx
import { AdminDashboard } from "./routes/AdminDashboard";
import { AdminDrafting } from "./routes/AdminDrafting";
import { AdminGoldenSet } from "./routes/AdminGoldenSet";
import { AdminLayout } from "./routes/AdminLayout";
import { AdminPendingUsers } from "./routes/AdminPendingUsers";
import { AdminUsers } from "./routes/AdminUsers";
```

(Drop the `import { RequireAdmin } from "./routes/RequireAdmin";` line — it's no longer referenced in `router.tsx`. `noUnusedLocals` would otherwise fail tsc.)

- [ ] **Step 4: Flip the manifest + regen route artifacts**

In `frontend/routes.json`, change `spa_routes` so `/admin/users` becomes `/admin`:

```json
  "spa_routes": ["/", "/cockpit", "/calls", "/me", "/profiles", "/admin"],
```

(Leave `backend_prefixes` unchanged for now — bare `/admin` Jinja still exists until Task 11; its removal is Task 11, doc-only.)

Run: `cd /home/parlar_ai/dev/call-monitor && uv run python scripts/gen_routes.py`
Expected: `frontend/src/routes.gen.ts` and `src/call_manager/web/spa_routes.py` regenerate with `/admin` replacing `/admin/users` in `SPA_ROUTES`.

- [ ] **Step 5: Verify the router drift guard + gen-routes check pass**

Run: `cd frontend && pnpm vitest run src/router.test.tsx`
Expected: PASS — every router leaf (`/admin`, `/admin/drafting`, `/admin/golden-set`, `/admin/users`, `/admin/users/pending`) collapses via `toOwningPrefix` to `/admin`, matching `SPA_ROUTES`.

Run: `cd /home/parlar_ai/dev/call-monitor && uv run pytest tests/test_gen_routes.py -q`
Expected: PASS (artifacts in sync).

- [ ] **Step 6: Add the ActivityBar Admin entry (admin-gated)**

`frontend/src/shell/ActivityBar.test.tsx` ALREADY EXISTS with 11 nav/active-route tests (Calls/Workspaces/Profiles/Settings links, current-workspace chip logic, References absence). Do NOT overwrite it. APPEND the imports it lacks (`waitFor` to the existing `@testing-library/react` import; add `afterEach`, `vi` to the existing `vitest` import; add `import * as clientMod from "../api/client";`), an `afterEach(() => vi.restoreAllMocks());`, the `mockMe` helper, and the two admin-gating tests below.

The existing 11 tests render `<ActivityBar/>` WITHOUT mocking `/api/me`. After Step 7 adds `useSession()` to the component, those tests trigger an unmocked `/api/me` fetch that fails in jsdom → `retry:false` → `session` undefined → `isAdmin` false → the Admin item is hidden. They assert only the always-present nav items, so they keep passing unchanged. `restoreAllMocks` is a no-op for the spy-free existing tests.

Add to the file:

```tsx
import { waitFor } from "@testing-library/react";
import { afterEach, vi } from "vitest";

import * as clientMod from "../api/client";

afterEach(() => {
  vi.restoreAllMocks();
});

function mockMe(admin: boolean) {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { user: { id: 1, email: "a@ki.se", status: "active", default_profile_id: null }, capabilities: { admin } },
    error: undefined,
  } as never);
}

it("shows the Admin entry for an admin", async () => {
  mockMe(true);
  renderWithProviders(<ActivityBar />);
  await waitFor(() => expect(screen.getByRole("link", { name: "Admin" })).toBeInTheDocument());
});

it("hides the Admin entry for a non-admin", async () => {
  mockMe(false);
  renderWithProviders(<ActivityBar />);
  // Settings always renders; wait for it, then assert Admin is absent.
  await waitFor(() => expect(screen.getByRole("link", { name: "Settings" })).toBeInTheDocument());
  expect(screen.queryByRole("link", { name: "Admin" })).not.toBeInTheDocument();
});
```

(Merge the new imports into the existing import lines rather than duplicating them — `noUnusedLocals` and duplicate-import would otherwise fail tsc. The existing file already imports `screen` from `@testing-library/react` and `expect, it` from `vitest`.)

Run: `cd frontend && pnpm vitest run src/shell/ActivityBar.test.tsx` → the two new tests FAIL (no Admin entry yet); the existing 11 still pass.

Add the Admin item to `frontend/src/shell/nav-items.ts` `ACTIVITY_BOTTOM` (append after Settings):

```ts
  { id: "admin", label: "Admin", icon: "shield", kind: "spa", to: "/admin" },
```

(`renderWithProviders` must mount the ActivityBar inside a router. Confirm `renderWithProviders` wraps in a router/`MemoryRouter`; the existing `RequireAdmin.test.tsx` uses it for routed components, so it does. The `Tooltip` from `@mantine/core` requires a MantineProvider — `renderWithProviders` already supplies it for the shell tests.)

Modify `frontend/src/shell/ActivityBar.tsx` to gate the admin item on `capabilities.admin`. Add `import { useSession } from "../api/session";`, read the session, and filter the bottom group:

```tsx
export function ActivityBar() {
  const { pathname } = useLocation();
  const { data: session } = useSession();
  const isAdmin = Boolean(session?.capabilities.admin);
  const wsId = cockpitIdFromPath(pathname);
  const onCockpitList = pathname.startsWith("/cockpit") && !wsId;
  const bottom = ACTIVITY_BOTTOM.filter((it) => it.id !== "admin" || isAdmin);
  return (
    <nav className={styles.activitybar} aria-label="Primary">
      <div className={styles.group}>
        {ACTIVITY_TOP.map((it) =>
          it.id === "workspaces" ? (
            <Item key={it.id} item={it} active={onCockpitList} />
          ) : (
            <Item key={it.id} item={it} />
          ),
        )}
        {wsId && (
          <Item
            item={{
              id: "active-ws",
              label: "Current workspace",
              icon: "edit",
              kind: "spa",
              to: `/cockpit/${wsId}`,
            }}
          />
        )}
      </div>
      <div className={styles.spacer} />
      <div className={styles.group}>
        {bottom.map((it) => (
          <Item key={it.id} item={it} />
        ))}
      </div>
    </nav>
  );
}
```

Run: `cd frontend && pnpm vitest run src/shell/ActivityBar.test.tsx` → PASS.

- [ ] **Step 7: Full frontend gate**

Run: `cd frontend && pnpm exec tsc --noEmit && pnpm vitest run && pnpm build`
Expected: tsc 0, all vitest green, build succeeds.

- [ ] **Step 8: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/routes/AdminLayout.tsx frontend/src/routes/AdminNav.tsx frontend/src/routes/AdminNav.module.css frontend/src/routes/AdminNav.test.tsx frontend/src/router.tsx frontend/src/shell/nav-items.ts frontend/src/shell/ActivityBar.tsx frontend/src/shell/ActivityBar.test.tsx frontend/routes.json frontend/src/routes.gen.ts src/call_manager/web/spa_routes.py
git commit -m "feat(spa): nested /admin layout + AdminNav + ActivityBar entry + manifest flip (admin Slice 2)"
```

---

### Task 11: Teardown — delete Jinja admin, repoint auth tests, post-teardown regens

Delete the two doomed Jinja modules + three templates, drop their `include_router` lines, migrate the surface-absent guard, repoint the `test_web_auth` tests (NOT 1:1 — see below), mark the now-dormant `_redirect_html_401` HTML branch, and run the post-teardown OpenAPI + routes regens.

**Files:**
- Delete: `src/call_manager/web/routes/admin.py`, `src/call_manager/web/routes/golden_set.py`
- Delete: `src/call_manager/web/templates/admin.html`, `admin_drafting.html`, `golden_set.html`
- Delete: `tests/test_web_admin.py`, `test_web_admin_ranking.py`, `test_web_admin_digest_panel.py`, `test_web_admin_embedding_coverage.py`, `test_web_admin_drafting_panel.py`, `test_web_admin_levers.py`, `test_web_admin_calibration_banner.py`, `test_web_golden_set_crud.py`
- Modify: `src/call_manager/main.py` (drop `admin`/`golden_set` imports + `include_router` lines; add the dormant-branch comment)
- Modify: `frontend/routes.json` (drop `/admin` from `backend_prefixes` — doc-only)
- Modify (regenerated): `frontend/src/api/schema.gen.ts`, `frontend/src/routes.gen.ts`, `src/call_manager/web/spa_routes.py`
- Modify: `tests/test_admin_users_jinja_surface_absent.py` (extend for Slice-2 paths)
- Modify: `tests/test_web_auth.py` (2 retired + 1 repointed+renamed + 1 new)

**Interfaces:**
- Consumes: nothing new.
- Produces: the final admin Jinja teardown.

- [ ] **Step 1: Coverage-mapping check before deleting the 8 Jinja test files**

Before deleting, confirm every behavior in the 8 Jinja admin test files is now covered by a `tests/test_api_admin_*.py` test (the profiles re-embed lesson — never delete a test file without migrating its coverage). Walk each Jinja test and point to its `/api` equivalent. Behaviors that were Jinja-template-only and intentionally NOT migrated (record them as such, do not re-add):
  - Stockholm `display_dt` time-format assertions (`05:15` vs `03:15 UTC`) → React renders browser-local (blessed divergence).
  - `?ok=/?err=` redirect-location + banner-CSS-class + autoescape assertions (`test_web_admin_levers.py` banner tests) → replaced by the `{triggered}`/`{enqueued}` JSON contract + the `_ok_msgs`/`_err_msgs` ported into `AdminDashboard.test.tsx`.
  - `data-confirm` / form-`action` HTML assertions → replaced by the React confirm + POST-target tests.
  - Jinja `tojson` HTML-escape assertion → structural (React escapes text).
If any NON-template behavior lacks an `/api` test, ADD it to the relevant `tests/test_api_admin_*.py` before proceeding.

- [ ] **Step 2: Delete the Jinja modules, templates, and migrated test files**

```bash
cd /home/parlar_ai/dev/call-monitor
git rm src/call_manager/web/routes/admin.py src/call_manager/web/routes/golden_set.py
git rm src/call_manager/web/templates/admin.html src/call_manager/web/templates/admin_drafting.html src/call_manager/web/templates/golden_set.html
git rm tests/test_web_admin.py tests/test_web_admin_ranking.py tests/test_web_admin_digest_panel.py tests/test_web_admin_embedding_coverage.py tests/test_web_admin_drafting_panel.py tests/test_web_admin_levers.py tests/test_web_admin_calibration_banner.py tests/test_web_golden_set_crud.py
```

- [ ] **Step 3: Drop the router wiring + mark the dormant auth branch**

In `src/call_manager/main.py`: remove `admin,` and `golden_set,` from the `from call_manager.web.routes import (...)` block (Task-research line ~67-74), and delete the two lines `app.include_router(admin.router)` and `app.include_router(golden_set.router)` (~523-524).

Add a one-line comment marking the now-dormant HTML-redirect branch. Immediately above the `if exc.status_code == 401 and wants_html:` block (`main.py`:356), insert:

```python
        # DORMANT after admin Slice 2: no protected non-/api HTML route can 401
        # any more — every surviving non-/api route is public (auth/register/
        # oauth/health) or an SPA path served at 200. Kept until the auth slice
        # (which removes this whole non-/api branch). Not dead-code-removed here
        # to keep this slice's diff scoped to admin.
        if exc.status_code == 401 and wants_html:
```

- [ ] **Step 4: Run the post-teardown OpenAPI + routes regens**

Route deletions change the OpenAPI schema (the Slice-E lesson):

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh
```

Drop `/admin` from `frontend/routes.json` `backend_prefixes` (doc-only — `is_backend_route` is not consulted by serving/proxy, `spa_routes.py`:6-9):

```json
  "backend_prefixes": ["/api", "/healthz", "/login", "/logout", "/register",
    "/auth", "/workspaces", "/static"],
```

Then regen the route artifacts:

```bash
uv run python scripts/gen_routes.py
```

- [ ] **Step 5: Extend the surface-absent guard**

Replace the body of `tests/test_admin_users_jinja_surface_absent.py` so it also asserts the Slice-2 Jinja routes are gone and the new `/api` routes present:

```python
def test_no_jinja_admin_routes_remain(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    from call_manager.main import create_app

    app = create_app()
    paths = {r.path for r in app.routes}
    # Jinja user-mgmt routes (Slice 1) gone:
    for p in [
        "/admin/users",
        "/admin/users/pending",
        "/admin/users/{user_id}/approve",
        "/admin/users/{user_id}/deny",
        "/admin/users/{user_id}/disable",
        "/admin/users/{user_id}/enable",
        "/admin/users/{user_id}/role",
        "/admin/users/{user_id}/reset-password",
    ]:
        assert p not in paths, p
    # Jinja Slice-2 routes gone:
    for p in [
        "/admin",
        "/admin/drafting",
        "/admin/rerank",
        "/admin/digest/send",
        "/admin/digest/reenable",
        "/admin/sources/{slug}/fetch",
        "/admin/golden-set",
        "/admin/golden-set/add",
        "/admin/golden-set/delete",
    ]:
        assert p not in paths, p
    # /api routes present:
    for p in [
        "/api/admin/users",
        "/api/admin/users/pending",
        "/api/admin/dashboard",
        "/api/admin/drafting",
        "/api/admin/rerank",
        "/api/admin/digest/send",
        "/api/admin/digest/reenable",
        "/api/admin/sources/{slug}/fetch",
        "/api/admin/golden-set",
        "/api/admin/golden-set/{triple_id}",
    ]:
        assert p in paths, p
```

- [ ] **Step 6: Repoint the `test_web_auth` tests (2 retired + 1 repointed+renamed + 1 new)**

In `tests/test_web_auth.py`: **delete** `test_html_401_get_threads_next` and `test_html_401_post_does_not_thread_next` (they GET/POST bare `/admin` + Accept text/html to exercise the `_redirect_html_401` HTML-redirect branch at `main.py`:356-375; with `/admin` gone there is no protected non-`/api` HTML route, and `/api/admin/dashboard` takes the envelope branch — a 303 is not expressible against `/api`; `safe_next`'s sanitizer stays covered by `test_auth_redirects.py`). **Replace** `test_json_401_unaffected_by_next` with a repointed+renamed envelope test and a new non-admin 403 test:

```python
def test_api_admin_requires_auth(monkeypatch, pg_url):
    """An unauthenticated /api caller gets the JSON 401 envelope, no redirect."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/api/admin/dashboard", follow_redirects=False)
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["error"]["code"] == "unauthorized"


def test_api_admin_forbidden_for_non_admin(monkeypatch, pg_url, pg_session_factory):
    """An authenticated non-admin gets 403 (the require_admin backstop)."""
    app, _ = _mk_app(monkeypatch, pg_url)
    from call_manager.db.models import User

    with pg_session_factory() as s:
        u = User(status="active", email="plain@ki.se", is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    with TestClient(app) as client:
        client.cookies.set("session", app.state.session_signer.make(user_id=uid))
        r = client.get("/api/admin/dashboard")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"
```

(Confirm `_mk_app` returns `(app, ...)` and that `app.state.session_signer` is reachable — `test_web_auth.py`'s existing helpers do this; mirror them. If `pg_session_factory` isn't already a param on neighboring tests in this file, it is a root-conftest fixture and can be added to the signature.)

- [ ] **Step 7: Run the affected backend + frontend gates**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/test_admin_users_jinja_surface_absent.py tests/test_web_auth.py tests/test_gen_routes.py tests/test_api_admin_dashboard.py tests/test_api_admin_drafting.py tests/test_api_admin_ops.py tests/test_api_admin_golden_set.py -q
```
Expected: PASS (run targeted files — whole-suite PG flakes under load).

```bash
cd frontend && pnpm exec tsc --noEmit && pnpm vitest run src/router.test.tsx && pnpm build
```
Expected: tsc 0, router drift guard green, build succeeds.

- [ ] **Step 8: Lint the whole repo (the Slice-1 lesson — CI runs `ruff check .`, tests/ included)**

Run: `cd /home/parlar_ai/dev/call-monitor && uv run ruff check . && uv run ruff format --check .`
Expected: clean. Fix any lint in the new test/source files before committing.

- [ ] **Step 9: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add -A
git commit -m "refactor(spa): delete Jinja admin + golden-set, repoint auth tests, post-teardown regen (admin Slice 2)"
```

---

## Self-Review

(Completed by the plan author against the spec — see the conversation. Coverage, placeholder, and type-consistency checks below.)

**Spec coverage:** dashboard read-model (Task 1) ✓; drafting read-model (Task 2) ✓; 4 ops mutations (Task 3) ✓; golden-set read/add/delete (Task 4) ✓; hooks (Tasks 5-6) ✓; three page components + clean error state + verbatim `_ok_msgs`/`_err_msgs` + rerank confirm (Tasks 7-9) ✓; nested `/admin` layout + AdminNav + ActivityBar admin entry + manifest flip + router drift guard (Task 10) ✓; teardown delete + surface-absent extend + `test_web_auth` 2-retired/1-repointed/1-new + dormant-branch comment + two post-teardown regens (Task 11) ✓; date-display divergence, FK→400 + rollback, banner-UX JSON, `enqueued`=active-profile count, `per_workspace` used_in-desc, backend_prefixes doc-only — all encoded.

**Type consistency:** schema names match across backend↔hooks↔components (`AdminDashboardOut`/`AdminDraftingOut`/`AdminGoldenSetOut`, `RerankOut{enqueued}`, `TriggeredOut{triggered}`, `AdminActionOut{id}`, `TripleCreatedOut{id}`); hook names match component imports (`useAdminDashboard/useRerank/useSendDigest/useReenableDigestUser/useFetchSource`, `useAdminGoldenSet/useAddTriple/useDeleteTriple`); query keys `["admin-dashboard"]`/`["admin-golden-set"]` consistent between hooks and the spec's invalidation contract.
