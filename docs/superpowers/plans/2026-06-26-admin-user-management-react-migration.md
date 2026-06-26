# Admin User-Management React Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the two admin user-management pages (`/admin/users`, `/admin/users/pending`) and their six mutations from Jinja2+HTMX to the React SPA, as Slice 1 of the two-slice admin migration.

**Architecture:** Standard strangler-fig slice — additive typed `/api/admin/users*` endpoints (new `web/api/admin_users.py`) → React routes under the existing AppShell behind a `RequireAdmin` guard → Jinja teardown (trim `web/routes/admin.py`, delete two templates, migrate tests, register `/admin/users` as an SPA prefix). Bare `/admin` and the rest stay Jinja for Slice 2.

**Tech Stack:** FastAPI + SQLAlchemy 2.x (psycopg3) + Pydantic v2; React 19 + react-router + TanStack Query + openapi-fetch; vitest + RTL; pytest + testcontainers Postgres.

**Spec:** `docs/superpowers/specs/2026-06-26-admin-user-management-react-migration-design.md`

## Global Constraints

(Every task's requirements implicitly include this section.)

- **No DB schema change. No new dependency. Strict parity port.**
- New backend module `web/api/admin_users.py`, registered in `web/api/__init__.py`. Response/request models namespaced `Admin*` (an unrelated `UserOut` already exists in the `/api` schema — re-verify no collision at the T4 regen).
- Errors via `raise HTTPException(status_code=…, detail=…)` → the global `/api` envelope `{"error":{"code","message"}}`; **never hand-build the envelope.** 400→`bad_request`, 403→`forbidden`, 404→`not_found`.
- All endpoints gated by `require_admin` (403 for an authenticated non-admin; 401 for unauthenticated).
- **CSRF:** `OriginCSRFMiddleware` allows an ABSENT Origin; TestClient mutation tests pass WITHOUT an Origin header — do not add one.
- **Parity-critical, preserve verbatim:** the row-locking `_lock_active_admin_count` guard; `session_epoch` bumps on disable / reset-password / approve-with-password (and NOT on enable/deny/role/password-less-OAuth-approve); the `oidc_sub is None ⇒ initial_password required` approve branch; the status-transition gates; **`reset-password` has NO status gate (explicit non-gate — do not add one).**
- **Check ordering:** for `approve` and `role`, validate the `role` param **before** the DB lookup (invalid role on a nonexistent user → `400`, not `404`); for all mutations the `404` precedes state/guard checks. The request model **defaults `role` to `"member"`**.
- **Frontend:** hooks in `frontend/src/api/admin.ts`; query hooks set `retry: false`; styling = CSS Modules + `--app-*` tokens, **never bare element selectors** in a `.module.css` (scope under a hashed class). `tsconfig` has `noUnusedLocals: true` + `include: ["src"]` — tsc checks test files too; no unused imports/vars anywhere under `src/`.
- **Manifest is prefix-not-`:param`:** add `/admin/users` to `spa_routes`; the two React routes + the manifest prefix land in ONE commit (T8 — `router.test.tsx` is set-equality via `toOwningPrefix`). Bare `/admin` STAYS in `backend_prefixes`.
- **Two OpenAPI regens:** mid-slice (T4, after all `/api` endpoints exist — adds the new paths/models) and post-teardown (T9, after the Jinja routes are deleted — drops the 8 stale Jinja paths). Command: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh` (the override avoids the mxbai `.env` default logging a stdout WARNING that corrupts `schema.gen.ts`). Routes regen (T8): `uv run python scripts/gen_routes.py`.
- **Tests:** `/api` + admin tests live in `tests/` ROOT (psycopg3 `pg_url`/`pg_session_factory` fixtures). Whole-suite testcontainers Postgres is flaky under load — run the targeted files; a connection-refused on a targeted re-run is infra.

---

### Task 1: Backend read endpoints (`web/api/admin_users.py`)

**Files:**
- Create: `src/call_manager/web/api/admin_users.py`
- Modify: `src/call_manager/web/api/__init__.py` (register the router)
- Test: `tests/test_api_admin_users.py`

**Interfaces:**
- Produces: `router` (APIRouter) with `GET /api/admin/users?page=N` → `AdminUsersPageOut{users:[AdminUserOut{id,email,name,status,is_admin}], page, per_page, total}` and `GET /api/admin/users/pending?page=N` → `AdminPendingPageOut{users:[AdminPendingUserOut{id,email,name,created_at,google_linked}], page}`. Both `require_admin`. These model names are consumed by Tasks 2–4 (same module) and the frontend (Task 5).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_admin_users.py
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from call_manager.auth.passwords import verify_password
from call_manager.db.models import User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def _admin(app, sf, email="admin@ki.se"):
    with sf() as s:
        a = User(email=email, status="active", is_admin=True)
        s.add(a)
        s.commit()
        s.refresh(a)
        aid = a.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=aid))
    return c, aid


def _user(sf, *, email, status="active", is_admin=False, oidc_sub=None):
    with sf() as s:
        u = User(
            email=email,
            status=status,
            is_admin=is_admin,
            **({"oidc_iss": "i", "oidc_sub": oidc_sub} if oidc_sub else {}),
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def test_list_users_excludes_pending_with_pagination(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    _user(pg_session_factory, email="active@ki.se", status="active")
    _user(pg_session_factory, email="disabled@ki.se", status="disabled")
    _user(pg_session_factory, email="pend@ki.se", status="pending")
    r = c.get("/api/admin/users")
    assert r.status_code == 200
    body = r.json()
    emails = {u["email"] for u in body["users"]}
    assert "active@ki.se" in emails and "disabled@ki.se" in emails
    assert "pend@ki.se" not in emails  # pending excluded
    assert body["page"] == 1 and body["per_page"] == 50
    assert body["total"] == len(emails)  # admin + active + disabled, no pending
    row = next(u for u in body["users"] if u["email"] == "active@ki.se")
    assert set(row) == {"id", "email", "name", "status", "is_admin"}


def test_pending_list_shape(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    _user(pg_session_factory, email="local@ki.se", status="pending")
    _user(pg_session_factory, email="goog@ki.se", status="pending", oidc_sub="s1")
    r = c.get("/api/admin/users/pending")
    assert r.status_code == 200
    body = r.json()
    rows = {u["email"]: u for u in body["users"]}
    assert rows["local@ki.se"]["google_linked"] is False
    assert rows["goog@ki.se"]["google_linked"] is True
    assert body["page"] == 1


def test_reads_require_admin_403(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    uid = _user(pg_session_factory, email="member@ki.se", status="active")
    with TestClient(app) as c:
        c.cookies.set("session", app.state.session_signer.make(user_id=uid))
        assert c.get("/api/admin/users").status_code == 403
        assert c.get("/api/admin/users/pending").status_code == 403
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_api_admin_users.py -q`
Expected: FAIL (404 / module import error — endpoints not defined).

- [ ] **Step 3: Create the module**

```python
# src/call_manager/web/api/admin_users.py
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from call_manager.auth.dependencies import require_admin
from call_manager.db.models import User

router = APIRouter()

_PER_PAGE = 50


class AdminUserOut(BaseModel):
    id: int
    email: str
    name: str | None
    status: str
    is_admin: bool


class AdminUsersPageOut(BaseModel):
    users: list[AdminUserOut]
    page: int
    per_page: int
    total: int


class AdminPendingUserOut(BaseModel):
    id: int
    email: str
    name: str | None
    created_at: datetime
    google_linked: bool


class AdminPendingPageOut(BaseModel):
    users: list[AdminPendingUserOut]
    page: int


@router.get("/admin/users", response_model=AdminUsersPageOut)
def admin_users_list(
    request: Request,
    page: int = 1,
    admin: User = Depends(require_admin),  # noqa: B008
):
    page = max(1, page)
    with request.app.state.session_factory() as db:
        rows = (
            db.execute(
                select(User)
                .where(User.status != "pending")
                .order_by(User.email.asc())
                .limit(_PER_PAGE)
                .offset((page - 1) * _PER_PAGE)
            )
            .scalars()
            .all()
        )
        total = db.execute(
            select(func.count()).select_from(User).where(User.status != "pending")
        ).scalar_one()
        users = [
            AdminUserOut(
                id=u.id, email=u.email, name=u.name, status=u.status, is_admin=u.is_admin
            )
            for u in rows
        ]
    return AdminUsersPageOut(users=users, page=page, per_page=_PER_PAGE, total=total)


@router.get("/admin/users/pending", response_model=AdminPendingPageOut)
def admin_pending_list(
    request: Request,
    page: int = 1,
    admin: User = Depends(require_admin),  # noqa: B008
):
    page = max(1, page)
    with request.app.state.session_factory() as db:
        rows = (
            db.execute(
                select(User)
                .where(User.status == "pending")
                .order_by(User.created_at.asc())
                .limit(_PER_PAGE)
                .offset((page - 1) * _PER_PAGE)
            )
            .scalars()
            .all()
        )
        users = [
            AdminPendingUserOut(
                id=u.id,
                email=u.email,
                name=u.name,
                created_at=u.created_at,
                google_linked=u.oidc_sub is not None,
            )
            for u in rows
        ]
    return AdminPendingPageOut(users=users, page=page)
```

The import block above is exactly what the read endpoints need (ruff-clean for this task). Tasks 2–4 extend it (`UTC`, `HTTPException`, `WeakPassword`, `hash_password`) as they add the mutation endpoints — Task 2's Step 3 shows the full extended block.

- [ ] **Step 4: Register the router**

In `src/call_manager/web/api/__init__.py`, add to the import block and include it:

```python
from call_manager.web.api import (
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
```python
router.include_router(admin_users.router)
```
(Add the `include_router` line alongside the others, e.g. after `me.router`.)

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_api_admin_users.py -q && uv run ruff check src/call_manager/web/api/admin_users.py`
Expected: PASS, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/api/admin_users.py src/call_manager/web/api/__init__.py tests/test_api_admin_users.py
git commit -m "feat(api): GET /api/admin/users + /pending read-models (admin user mgmt)"
```

---

### Task 2: approve + deny mutations

**Files:**
- Modify: `src/call_manager/web/api/admin_users.py`
- Test: `tests/test_api_admin_users.py` (append)

**Interfaces:**
- Consumes: the Task-1 module/models.
- Produces: `POST /api/admin/users/{user_id}/approve` (body `AdminApproveIn{role="member", initial_password?}` → `AdminActionOut{id}`) and `POST /api/admin/users/{user_id}/deny` (no body → `AdminActionOut`). `AdminApproveIn` and `AdminActionOut` are reused by later tasks/frontend.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_api_admin_users.py`)

```python
def test_approve_member_and_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, aid = _admin(app, pg_session_factory)
    pid = _user(pg_session_factory, email="p1@ki.se", status="pending", oidc_sub="s1")
    assert c.post(f"/api/admin/users/{pid}/approve", json={"role": "member"}).status_code == 200
    with pg_session_factory() as s:
        u = s.get(User, pid)
        assert u.status == "active" and u.is_admin is False and u.approved_at is not None
        assert u.approved_by_user_id == aid
    pid2 = _user(pg_session_factory, email="p2@ki.se", status="pending", oidc_sub="s2")
    c.post(f"/api/admin/users/{pid2}/approve", json={"role": "admin"})
    with pg_session_factory() as s:
        assert s.get(User, pid2).is_admin is True


def test_approve_defaults_role_to_member(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    pid = _user(pg_session_factory, email="p3@ki.se", status="pending", oidc_sub="s3")
    assert c.post(f"/api/admin/users/{pid}/approve", json={}).status_code == 200
    with pg_session_factory() as s:
        assert s.get(User, pid).is_admin is False


def test_approve_rejects_non_pending(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, aid = _admin(app, pg_session_factory)  # admin is active, not pending
    r = c.post(f"/api/admin/users/{aid}/approve", json={"role": "member"})
    assert r.status_code == 400


def test_approve_invalid_role_on_nonexistent_is_400_not_404(monkeypatch, pg_url, pg_session_factory):
    # ordering lock: role is validated BEFORE the user lookup.
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    r = c.post("/api/admin/users/999999/approve", json={"role": "galaxy"})
    assert r.status_code == 400


def test_approve_404_for_unknown_user(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    r = c.post("/api/admin/users/999999/approve", json={"role": "member"})
    assert r.status_code == 404


def test_approve_local_requires_password_then_bumps_epoch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    with pg_session_factory() as s:
        p = User(email="local@ki.se", status="pending")  # no oidc, no password
        s.add(p)
        s.commit()
        s.refresh(p)
        pid, ep0 = p.id, p.session_epoch
    assert c.post(f"/api/admin/users/{pid}/approve", json={"role": "member"}).status_code == 400
    c.post(
        f"/api/admin/users/{pid}/approve",
        json={"role": "member", "initial_password": "issued-strong-pw"},
    )
    with pg_session_factory() as s:
        u = s.get(User, pid)
        assert u.status == "active" and verify_password(u.password_hash, "issued-strong-pw")
        assert u.session_epoch == ep0 + 1


def test_approve_oauth_without_password_ok(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    pid = _user(pg_session_factory, email="g@ki.se", status="pending", oidc_sub="s9")
    assert c.post(f"/api/admin/users/{pid}/approve", json={"role": "member"}).status_code == 200


def test_approve_weak_password_400(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    with pg_session_factory() as s:
        p = User(email="local2@ki.se", status="pending")
        s.add(p)
        s.commit()
        s.refresh(p)
        pid = p.id
    r = c.post(f"/api/admin/users/{pid}/approve", json={"role": "member", "initial_password": "short"})
    assert r.status_code == 400


def test_deny_only_pending(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    pid = _user(pg_session_factory, email="d@ki.se", status="pending", oidc_sub="sd")
    assert c.post(f"/api/admin/users/{pid}/deny").status_code == 200
    with pg_session_factory() as s:
        assert s.get(User, pid).status == "disabled"
    # denying a non-pending user -> 400
    uid = _user(pg_session_factory, email="active2@ki.se", status="active")
    assert c.post(f"/api/admin/users/{uid}/deny").status_code == 400


def test_deny_404(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    assert c.post("/api/admin/users/999999/deny").status_code == 404
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_api_admin_users.py -q -k "approve or deny"`
Expected: FAIL (endpoints not defined).

- [ ] **Step 3: Implement** — add to `admin_users.py` (and extend the import block):

Import block becomes:
```python
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from call_manager.auth.dependencies import require_admin
from call_manager.auth.passwords import WeakPassword, hash_password
from call_manager.db.models import User
```

Add the models and endpoints:
```python
class AdminApproveIn(BaseModel):
    role: str = "member"
    initial_password: str | None = None


class AdminActionOut(BaseModel):
    id: int


@router.post("/admin/users/{user_id}/approve", response_model=AdminActionOut)
def admin_approve_user(
    request: Request,
    user_id: int,
    body: AdminApproveIn,
    admin: User = Depends(require_admin),  # noqa: B008
):
    if body.role not in ("member", "admin"):
        raise HTTPException(status_code=400, detail="invalid role")
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if u.status != "pending":
            raise HTTPException(status_code=400, detail="user is not pending")
        if u.oidc_sub is None and not body.initial_password:
            raise HTTPException(status_code=400, detail="initial password required")
        if body.initial_password:
            try:
                u.password_hash = hash_password(body.initial_password)
            except WeakPassword as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            u.session_epoch = (u.session_epoch or 0) + 1  # credential issued -> revoke prior
        u.status = "active"
        u.is_admin = body.role == "admin"
        u.approved_by_user_id = admin.id
        u.approved_at = datetime.now(UTC)
        db.commit()
    return AdminActionOut(id=user_id)


@router.post("/admin/users/{user_id}/deny", response_model=AdminActionOut)
def admin_deny_user(
    request: Request,
    user_id: int,
    admin: User = Depends(require_admin),  # noqa: B008
):
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if u.status != "pending":
            raise HTTPException(status_code=400, detail="user is not pending")
        u.status = "disabled"
        db.commit()
    return AdminActionOut(id=user_id)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_api_admin_users.py -q && uv run ruff check src/call_manager/web/api/admin_users.py`
Expected: PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/api/admin_users.py tests/test_api_admin_users.py
git commit -m "feat(api): POST /api/admin/users/{id}/approve + /deny (parity order, epoch, role default)"
```

---

### Task 3: disable + role mutations + last-admin guard

**Files:**
- Modify: `src/call_manager/web/api/admin_users.py`
- Test: `tests/test_api_admin_users.py` (append)

**Interfaces:**
- Consumes: Task-1/2 module + `AdminActionOut`.
- Produces: `_lock_active_admin_count(db) -> int` (module-private), `POST .../disable` (no body), `POST .../role` (body `AdminRoleIn{role}`).

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_disable_bumps_epoch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    _user(pg_session_factory, email="admin2@ki.se", status="active", is_admin=True)  # keep >1 admin
    with pg_session_factory() as s:
        m = User(email="m@ki.se", status="active")
        s.add(m)
        s.commit()
        s.refresh(m)
        mid, before = m.id, m.session_epoch
    assert c.post(f"/api/admin/users/{mid}/disable").status_code == 200
    with pg_session_factory() as s:
        u = s.get(User, mid)
        assert u.status == "disabled" and u.session_epoch == before + 1


def test_disable_only_active_400(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    did = _user(pg_session_factory, email="dis@ki.se", status="disabled")
    assert c.post(f"/api/admin/users/{did}/disable").status_code == 400


def test_cannot_disable_last_active_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, aid = _admin(app, pg_session_factory)
    assert c.post(f"/api/admin/users/{aid}/disable").status_code == 400
    with pg_session_factory() as s:
        assert s.get(User, aid).status == "active"


def test_disable_404(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    assert c.post("/api/admin/users/999999/disable").status_code == 404


def test_change_role_to_admin_and_member(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)  # keeps an admin around for demote tests
    uid = _user(pg_session_factory, email="r@ki.se", status="active", is_admin=False)
    assert c.post(f"/api/admin/users/{uid}/role", json={"role": "admin"}).status_code == 200
    with pg_session_factory() as s:
        assert s.get(User, uid).is_admin is True
    assert c.post(f"/api/admin/users/{uid}/role", json={"role": "member"}).status_code == 200
    with pg_session_factory() as s:
        assert s.get(User, uid).is_admin is False


def test_change_role_demote_last_admin_guarded(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, aid = _admin(app, pg_session_factory)  # only one active admin
    r = c.post(f"/api/admin/users/{aid}/role", json={"role": "member"})
    assert r.status_code == 400
    with pg_session_factory() as s:
        assert s.get(User, aid).is_admin is True


def test_change_role_invalid_role_on_nonexistent_is_400(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    assert c.post("/api/admin/users/999999/role", json={"role": "galaxy"}).status_code == 400


def test_change_role_not_active_400(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    did = _user(pg_session_factory, email="rd@ki.se", status="disabled")
    assert c.post(f"/api/admin/users/{did}/role", json={"role": "admin"}).status_code == 400
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_api_admin_users.py -q -k "disable or role"`
Expected: FAIL.

- [ ] **Step 3: Implement** — add to `admin_users.py`:

```python
class AdminRoleIn(BaseModel):
    role: str


def _lock_active_admin_count(db) -> int:
    """Count active admins while LOCKING those rows, so two concurrent
    demote/disable transactions serialize and can't both pass the guard."""
    db.execute(
        select(User.id).where(User.is_admin.is_(True), User.status == "active").with_for_update()
    ).all()
    return db.execute(
        select(func.count())
        .select_from(User)
        .where(User.is_admin.is_(True), User.status == "active")
    ).scalar_one()


@router.post("/admin/users/{user_id}/disable", response_model=AdminActionOut)
def admin_disable_user(
    request: Request,
    user_id: int,
    admin: User = Depends(require_admin),  # noqa: B008
):
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if u.status != "active":
            raise HTTPException(status_code=400, detail="user is not active")
        if u.is_admin and _lock_active_admin_count(db) <= 1:
            raise HTTPException(status_code=400, detail="cannot disable the last active admin")
        u.status = "disabled"
        u.session_epoch = (u.session_epoch or 0) + 1
        db.commit()
    return AdminActionOut(id=user_id)


@router.post("/admin/users/{user_id}/role", response_model=AdminActionOut)
def admin_change_role(
    request: Request,
    user_id: int,
    body: AdminRoleIn,
    admin: User = Depends(require_admin),  # noqa: B008
):
    if body.role not in ("member", "admin"):
        raise HTTPException(status_code=400, detail="invalid role")
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if u.status != "active":
            raise HTTPException(status_code=400, detail="user is not active")
        demoting = u.is_admin and body.role == "member"
        if demoting and _lock_active_admin_count(db) <= 1:
            raise HTTPException(status_code=400, detail="cannot demote the last active admin")
        u.is_admin = body.role == "admin"
        db.commit()
    return AdminActionOut(id=user_id)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_api_admin_users.py -q && uv run ruff check src/call_manager/web/api/admin_users.py`
Expected: PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/api/admin_users.py tests/test_api_admin_users.py
git commit -m "feat(api): POST /api/admin/users/{id}/disable + /role (last-admin guard, epoch)"
```

---

### Task 4: enable + reset-password mutations + OpenAPI regen

**Files:**
- Modify: `src/call_manager/web/api/admin_users.py`
- Modify (generated): `frontend/src/api/schema.gen.ts`
- Test: `tests/test_api_admin_users.py` (append)

**Interfaces:**
- Consumes: Task-1..3 module.
- Produces: `POST .../enable` (no body), `POST .../reset-password` (body `AdminResetPasswordIn{new_password}`). After this task `schema.gen.ts` carries `Admin*` models + `/api/admin/users*` paths for the frontend.

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_enable_only_disabled(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    did = _user(pg_session_factory, email="en@ki.se", status="disabled")
    assert c.post(f"/api/admin/users/{did}/enable").status_code == 200
    with pg_session_factory() as s:
        assert s.get(User, did).status == "active"
    # enabling a non-disabled (pending) user -> 400
    pid = _user(pg_session_factory, email="enp@ki.se", status="pending", oidc_sub="sp")
    assert c.post(f"/api/admin/users/{pid}/enable").status_code == 400


def test_enable_404(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    assert c.post("/api/admin/users/999999/enable").status_code == 404


def test_reset_password_bumps_epoch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    with pg_session_factory() as s:
        u = User(email="rp@ki.se", status="active")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid, ep0 = u.id, u.session_epoch
    assert (
        c.post(f"/api/admin/users/{uid}/reset-password", json={"new_password": "fresh-strong-pw"}).status_code
        == 200
    )
    with pg_session_factory() as s:
        u = s.get(User, uid)
        assert verify_password(u.password_hash, "fresh-strong-pw") and u.session_epoch == ep0 + 1


def test_reset_rejects_weak_400(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    uid = _user(pg_session_factory, email="rp2@ki.se", status="active")
    assert c.post(f"/api/admin/users/{uid}/reset-password", json={"new_password": "short"}).status_code == 400


def test_reset_password_has_no_status_gate(monkeypatch, pg_url, pg_session_factory):
    # reset-password works for a DISABLED user (no status gate) — pins the non-gate invariant.
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    did = _user(pg_session_factory, email="rpd@ki.se", status="disabled")
    assert (
        c.post(f"/api/admin/users/{did}/reset-password", json={"new_password": "fresh-strong-pw"}).status_code
        == 200
    )


def test_reset_404(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    assert c.post("/api/admin/users/999999/reset-password", json={"new_password": "fresh-strong-pw"}).status_code == 404
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_api_admin_users.py -q -k "enable or reset"`
Expected: FAIL.

- [ ] **Step 3: Implement** — add to `admin_users.py`:

```python
class AdminResetPasswordIn(BaseModel):
    new_password: str


@router.post("/admin/users/{user_id}/enable", response_model=AdminActionOut)
def admin_enable_user(
    request: Request,
    user_id: int,
    admin: User = Depends(require_admin),  # noqa: B008
):
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if u.status != "disabled":
            raise HTTPException(status_code=400, detail="user is not disabled")
        u.status = "active"
        db.commit()
    return AdminActionOut(id=user_id)


@router.post("/admin/users/{user_id}/reset-password", response_model=AdminActionOut)
def admin_reset_password(
    request: Request,
    user_id: int,
    body: AdminResetPasswordIn,
    admin: User = Depends(require_admin),  # noqa: B008
):
    # NO status gate — parity: resets a password for a user in any status.
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        try:
            u.password_hash = hash_password(body.new_password)
        except WeakPassword as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        u.session_epoch = (u.session_epoch or 0) + 1  # revoke the target's sessions
        db.commit()
    return AdminActionOut(id=user_id)
```

- [ ] **Step 4: Run backend to verify pass**

Run: `uv run pytest tests/test_api_admin_users.py -q && uv run ruff check src/call_manager/web/api/admin_users.py`
Expected: PASS (the full file — ~30 tests), ruff clean.

- [ ] **Step 5: Regenerate the OpenAPI types**

Run: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`
Then verify the new models + paths landed and there's no collision:
Run: `grep -E "AdminUserOut|AdminPendingUserOut|AdminApproveIn|/api/admin/users" frontend/src/api/schema.gen.ts | head`
Expected: the `Admin*` schemas and `/api/admin/users*` paths are present.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/api/admin_users.py tests/test_api_admin_users.py frontend/src/api/schema.gen.ts
git commit -m "feat(api): POST enable + reset-password (no status gate) + regen OpenAPI types"
```

---

### Task 5: Frontend hooks (`frontend/src/api/admin.ts`)

**Files:**
- Create: `frontend/src/api/admin.ts`
- Test: `frontend/src/api/admin.test.tsx`

**Interfaces:**
- Consumes: `schema.gen.ts` (`AdminUserOut`, `AdminPendingUserOut`).
- Produces: `useAdminUsers(page)`, `useAdminPendingUsers(page)`, and six mutation hooks consumed by Tasks 6–7. Query keys: `["admin-users", page]` / `["admin-pending", page]`; mutations invalidate by the `["admin-users"]` / `["admin-pending"]` prefixes. **approve + deny invalidate BOTH** prefixes; the other four invalidate `["admin-users"]`.

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/src/api/admin.test.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import {
  useAdminPendingUsers,
  useAdminUsers,
  useApproveUser,
  useChangeRole,
  useDenyUser,
  useDisableUser,
  useEnableUser,
  useResetPassword,
} from "./admin";

afterEach(() => {
  vi.restoreAllMocks();
});

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

it("useAdminUsers GETs the page", async () => {
  const get = vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: { users: [], page: 1, per_page: 50, total: 0 }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useAdminUsers(2), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith("/api/admin/users", { params: { query: { page: 2 } } });
});

it("useAdminPendingUsers GETs the pending page", async () => {
  const get = vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: { users: [], page: 1 }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useAdminPendingUsers(1), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith("/api/admin/users/pending", { params: { query: { page: 1 } } });
});

it("useApproveUser POSTs role+password and invalidates both lists", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 7 }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useApproveUser(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ id: 7, role: "admin", initial_password: "pw-strong-1234" });
  expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/approve", {
    params: { path: { user_id: 7 } },
    body: { role: "admin", initial_password: "pw-strong-1234" },
  });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-pending"] });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-users"] });
});

it("useApproveUser defaults role to member", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 1 }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useApproveUser(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ id: 1 });
  expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/approve", {
    params: { path: { user_id: 1 } },
    body: { role: "member", initial_password: undefined },
  });
});

it("useDenyUser POSTs and invalidates both lists", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 3 }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useDenyUser(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ id: 3 });
  expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/deny", { params: { path: { user_id: 3 } } });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-pending"] });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-users"] });
});

it("useDisableUser invalidates the users list only", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 4 }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useDisableUser(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ id: 4 });
  expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/disable", { params: { path: { user_id: 4 } } });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-users"] });
  expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ["admin-pending"] });
});

it("useChangeRole POSTs the role body", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 5 }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useChangeRole(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ id: 5, role: "member" });
  expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/role", {
    params: { path: { user_id: 5 } },
    body: { role: "member" },
  });
});

it("useResetPassword POSTs the new password", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 6 }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useResetPassword(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ id: 6, new_password: "fresh-strong-pw" });
  expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/reset-password", {
    params: { path: { user_id: 6 } },
    body: { new_password: "fresh-strong-pw" },
  });
});

it("useEnableUser POSTs enable", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 8 }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useEnableUser(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ id: 8 });
  expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/enable", { params: { path: { user_id: 8 } } });
});
```

- [ ] **Step 2: Run to verify fail**

Run (from `frontend/`): `pnpm exec vitest run src/api/admin.test.tsx`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

```ts
// frontend/src/api/admin.ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type AdminUser = components["schemas"]["AdminUserOut"];
export type AdminPendingUser = components["schemas"]["AdminPendingUserOut"];

export function useAdminUsers(page: number) {
  return useQuery({
    queryKey: ["admin-users", page],
    queryFn: async () => unwrap(await api.GET("/api/admin/users", { params: { query: { page } } })),
    retry: false,
  });
}

export function useAdminPendingUsers(page: number) {
  return useQuery({
    queryKey: ["admin-pending", page],
    queryFn: async () =>
      unwrap(await api.GET("/api/admin/users/pending", { params: { query: { page } } })),
    retry: false,
  });
}

export function useApproveUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { id: number; role?: "member" | "admin"; initial_password?: string }) =>
      unwrap(
        await api.POST("/api/admin/users/{user_id}/approve", {
          params: { path: { user_id: v.id } },
          body: { role: v.role ?? "member", initial_password: v.initial_password },
        }),
      ),
    // approve moves a user pending->active: refresh both queues.
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-pending"] });
      qc.invalidateQueries({ queryKey: ["admin-users"] });
    },
  });
}

export function useDenyUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { id: number }) =>
      unwrap(await api.POST("/api/admin/users/{user_id}/deny", { params: { path: { user_id: v.id } } })),
    // deny moves a user pending->disabled: refresh both queues.
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-pending"] });
      qc.invalidateQueries({ queryKey: ["admin-users"] });
    },
  });
}

export function useDisableUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { id: number }) =>
      unwrap(await api.POST("/api/admin/users/{user_id}/disable", { params: { path: { user_id: v.id } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-users"] }),
  });
}

export function useEnableUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { id: number }) =>
      unwrap(await api.POST("/api/admin/users/{user_id}/enable", { params: { path: { user_id: v.id } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-users"] }),
  });
}

export function useChangeRole() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { id: number; role: "member" | "admin" }) =>
      unwrap(
        await api.POST("/api/admin/users/{user_id}/role", {
          params: { path: { user_id: v.id } },
          body: { role: v.role },
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-users"] }),
  });
}

export function useResetPassword() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { id: number; new_password: string }) =>
      unwrap(
        await api.POST("/api/admin/users/{user_id}/reset-password", {
          params: { path: { user_id: v.id } },
          body: { new_password: v.new_password },
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-users"] }),
  });
}
```

- [ ] **Step 4: Run to verify pass + tsc**

Run (from `frontend/`): `pnpm exec vitest run src/api/admin.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS, tsc 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/admin.ts frontend/src/api/admin.test.tsx
git commit -m "feat(spa): admin user-mgmt hooks (lists + 6 mutations, both-list invalidation)"
```

---

### Task 6: `RequireAdmin` guard + `AdminUsers` route

**Files:**
- Create: `frontend/src/routes/RequireAdmin.tsx`, `frontend/src/routes/RequireAdmin.test.tsx`
- Create: `frontend/src/routes/AdminUsers.tsx`, `frontend/src/routes/AdminUsers.module.css`, `frontend/src/routes/AdminUsers.test.tsx`

**Interfaces:**
- Consumes: `useSession` (`./api/session`), the Task-5 hooks.
- Produces: `<RequireAdmin>` (renders children only for `capabilities.admin`) and `<AdminUsers>` (the active/disabled table). Both consumed by Task 8 (router) and Task 7 reuses `RequireAdmin`.

- [ ] **Step 1: Write the failing tests**

```tsx
// frontend/src/routes/RequireAdmin.test.tsx
import { screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { RequireAdmin } from "./RequireAdmin";

afterEach(() => {
  vi.restoreAllMocks();
});

function mockMe(admin: boolean) {
  return vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { user: { id: 1, email: "a@ki.se", status: "active", default_profile_id: null }, capabilities: { admin } },
    error: undefined,
  } as never);
}

it("renders children for an admin", async () => {
  mockMe(true);
  renderWithProviders(
    <RequireAdmin>
      <div>secret panel</div>
    </RequireAdmin>,
  );
  expect(await screen.findByText("secret panel")).toBeInTheDocument();
});

it("blocks a non-admin", async () => {
  mockMe(false);
  renderWithProviders(
    <RequireAdmin>
      <div>secret panel</div>
    </RequireAdmin>,
  );
  expect(await screen.findByText(/not authorized/i)).toBeInTheDocument();
  expect(screen.queryByText("secret panel")).not.toBeInTheDocument();
});
```

```tsx
// frontend/src/routes/AdminUsers.test.tsx
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { AdminUsers } from "./AdminUsers";

afterEach(() => {
  vi.restoreAllMocks();
});

function mockUsers(users: unknown[], total = users.length) {
  return vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: { users, page: 1, per_page: 50, total }, error: undefined } as never);
}

it("renders active and disabled rows with the right actions", async () => {
  mockUsers([
    { id: 1, email: "act@ki.se", name: null, status: "active", is_admin: false },
    { id: 2, email: "dis@ki.se", name: null, status: "disabled", is_admin: false },
  ]);
  renderWithProviders(<AdminUsers />);
  expect(await screen.findByText("act@ki.se")).toBeInTheDocument();
  // active row -> Disable + Make admin + reset-password; disabled row -> Enable
  expect(screen.getByRole("button", { name: /disable/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /make admin/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /enable/i })).toBeInTheDocument();
});

it("clicking Make admin posts role=admin", async () => {
  mockUsers([{ id: 1, email: "act@ki.se", name: null, status: "active", is_admin: false }]);
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 1 }, error: undefined } as never);
  renderWithProviders(<AdminUsers />);
  await userEvent.click(await screen.findByRole("button", { name: /make admin/i }));
  expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/role", {
    params: { path: { user_id: 1 } },
    body: { role: "admin" },
  });
});
```

- [ ] **Step 2: Run to verify fail**

Run (from `frontend/`): `pnpm exec vitest run src/routes/RequireAdmin.test.tsx src/routes/AdminUsers.test.tsx`
Expected: FAIL (modules not found).

- [ ] **Step 3: Implement `RequireAdmin`**

```tsx
// frontend/src/routes/RequireAdmin.tsx
import type { ReactNode } from "react";

import { useSession } from "../api/session";

// Client-side admin gate. The /api endpoints 403 on their own (the backstop);
// this avoids a broken-page flash on direct navigation. Unauthenticated users
// never reach here — useSession's 401 redirects to /login via unwrap.
export function RequireAdmin({ children }: { children: ReactNode }) {
  const { data, isLoading, isError } = useSession();
  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <p role="alert">Couldn't load your session.</p>;
  if (!data.capabilities.admin) {
    return (
      <main role="alert">
        <h1>Not authorized</h1>
        <p>You need admin access to view this page.</p>
      </main>
    );
  }
  return <>{children}</>;
}
```

- [ ] **Step 4: Implement `AdminUsers` + CSS**

```tsx
// frontend/src/routes/AdminUsers.tsx
import { useState } from "react";
import { Link } from "react-router-dom";

import type { AdminUser } from "../api/admin";
import {
  useAdminUsers,
  useChangeRole,
  useDisableUser,
  useEnableUser,
  useResetPassword,
} from "../api/admin";
import styles from "./AdminUsers.module.css";

function UserRow({ u }: { u: AdminUser }) {
  const [pw, setPw] = useState("");
  const disable = useDisableUser();
  const enable = useEnableUser();
  const role = useChangeRole();
  const reset = useResetPassword();
  const busy = disable.isPending || enable.isPending || role.isPending || reset.isPending;
  const err = disable.error || enable.error || role.error || reset.error;
  return (
    <tr>
      <td>{u.email}</td>
      <td>{u.status}</td>
      <td>{u.is_admin ? "admin" : "member"}</td>
      <td className={styles.actions}>
        {u.status === "active" && (
          <>
            <button
              type="button"
              className={styles.secondary}
              disabled={busy}
              onClick={() => disable.mutate({ id: u.id })}
            >
              Disable
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => role.mutate({ id: u.id, role: u.is_admin ? "member" : "admin" })}
            >
              {u.is_admin ? "Make member" : "Make admin"}
            </button>
            <form
              className={styles.resetForm}
              onSubmit={(e) => {
                e.preventDefault();
                reset.mutate({ id: u.id, new_password: pw }, { onSuccess: () => setPw("") });
              }}
            >
              <input
                type="password"
                value={pw}
                onChange={(e) => setPw(e.target.value)}
                placeholder="new password"
                required
              />
              <button type="submit" className={styles.secondary} disabled={busy}>
                Reset password
              </button>
            </form>
          </>
        )}
        {u.status === "disabled" && (
          <button type="button" disabled={busy} onClick={() => enable.mutate({ id: u.id })}>
            Enable
          </button>
        )}
        {err && (
          <span role="alert" className={styles.err}>
            {(err as Error).message}
          </span>
        )}
      </td>
    </tr>
  );
}

export function AdminUsers() {
  const [page, setPage] = useState(1);
  const { data, isLoading, isError } = useAdminUsers(page);
  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <p role="alert">Couldn't load users.</p>;
  const { users, per_page, total } = data;
  return (
    <main className={styles.page}>
      <h1>Users</h1>
      <table className={styles.table}>
        <thead>
          <tr>
            <th>Email</th>
            <th>Status</th>
            <th>Role</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <UserRow key={u.id} u={u} />
          ))}
        </tbody>
      </table>
      <p className={styles.pager}>
        Showing page {page} ({total} total).{" "}
        {page > 1 && (
          <button type="button" onClick={() => setPage(page - 1)}>
            Prev
          </button>
        )}
        {total > page * per_page && (
          <button type="button" onClick={() => setPage(page + 1)}>
            Next
          </button>
        )}
      </p>
      <p className={styles.nav}>
        <Link to="/admin/users/pending">Pending approvals</Link>
        {" · "}
        {/* bare /admin is still Jinja (Slice 2) -> full-page nav, not a router Link */}
        <a href="/admin">Admin home</a>
      </p>
    </main>
  );
}
```

```css
/* frontend/src/routes/AdminUsers.module.css */
.page { max-width: 60rem; margin: 0 auto; padding: 1rem 0.5rem 3rem; }
.page h1 { color: var(--app-heading); }
.table { width: 100%; border-collapse: collapse; }
.table th, .table td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--app-border); font-size: 0.9rem; }
.table th { color: var(--app-muted); font-weight: 600; }
.actions { display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: center; }
.actions button { border-radius: 5px; padding: 0.3rem 0.7rem; background: var(--app-accent); color: #fff; border: none; cursor: pointer; }
.actions button:disabled { opacity: 0.5; cursor: default; }
.secondary { background: var(--app-sidebar) !important; color: var(--app-text) !important; border: 1px solid var(--app-border) !important; }
.resetForm { display: inline-flex; gap: 0.3rem; align-items: center; }
.resetForm input { padding: 0.25rem 0.4rem; border: 1px solid var(--app-border); border-radius: 4px; background: var(--app-sidebar); color: var(--app-text); }
.pager { margin-top: 1rem; color: var(--app-muted); font-size: 0.9rem; }
.pager button { margin-left: 0.4rem; }
.nav { font-size: 0.85rem; }
.nav a { color: var(--app-link); text-decoration: none; }
.err { color: var(--app-danger, #e5484d); font-size: 0.8rem; }
```

(Known-good tokens from `ProfilesList.module.css`: `--app-border`/`--app-muted`/`--app-text`/`--app-accent`/`--app-sidebar`/`--app-link`/`--app-heading`. `var(--app-danger, #e5484d)` carries an inline fallback, so it renders correctly whether or not the theme defines `--app-danger` — do not invent other tokens.)

- [ ] **Step 5: Run to verify pass + tsc**

Run (from `frontend/`): `pnpm exec vitest run src/routes/RequireAdmin.test.tsx src/routes/AdminUsers.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS, tsc 0.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/routes/RequireAdmin.tsx frontend/src/routes/RequireAdmin.test.tsx frontend/src/routes/AdminUsers.tsx frontend/src/routes/AdminUsers.module.css frontend/src/routes/AdminUsers.test.tsx
git commit -m "feat(spa): RequireAdmin guard + AdminUsers table route"
```

---

### Task 7: `AdminPendingUsers` route

**Files:**
- Create: `frontend/src/routes/AdminPendingUsers.tsx`, `frontend/src/routes/AdminPendingUsers.module.css`, `frontend/src/routes/AdminPendingUsers.test.tsx`

**Interfaces:**
- Consumes: Task-5 hooks (`useAdminPendingUsers`, `useApproveUser`, `useDenyUser`).
- Produces: `<AdminPendingUsers>` (the approval queue), consumed by Task 8.

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/src/routes/AdminPendingUsers.test.tsx
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { AdminPendingUsers } from "./AdminPendingUsers";

afterEach(() => {
  vi.restoreAllMocks();
});

function mockPending(users: unknown[]) {
  return vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: { users, page: 1 }, error: undefined } as never);
}

it("shows an initial-password input for local accounts, not for Google", async () => {
  mockPending([
    { id: 1, email: "local@ki.se", name: null, created_at: "2026-06-01T00:00:00Z", google_linked: false },
    { id: 2, email: "goog@ki.se", name: "G", created_at: "2026-06-01T00:00:00Z", google_linked: true },
  ]);
  renderWithProviders(<AdminPendingUsers />);
  expect(await screen.findByText("local@ki.se")).toBeInTheDocument();
  // one password input total (only the local account has one)
  expect(screen.getAllByPlaceholderText(/initial password/i)).toHaveLength(1);
  expect(screen.getByText(/google/i)).toBeInTheDocument();
});

it("approve-as-admin posts role=admin with the typed password", async () => {
  mockPending([
    { id: 1, email: "local@ki.se", name: null, created_at: "2026-06-01T00:00:00Z", google_linked: false },
  ]);
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 1 }, error: undefined } as never);
  renderWithProviders(<AdminPendingUsers />);
  await userEvent.type(await screen.findByPlaceholderText(/initial password/i), "issued-strong-pw");
  await userEvent.click(screen.getByRole("button", { name: /approve as admin/i }));
  expect(post).toHaveBeenCalledWith("/api/admin/users/{user_id}/approve", {
    params: { path: { user_id: 1 } },
    body: { role: "admin", initial_password: "issued-strong-pw" },
  });
});

it("renders an empty state", async () => {
  mockPending([]);
  renderWithProviders(<AdminPendingUsers />);
  expect(await screen.findByText(/no accounts awaiting approval/i)).toBeInTheDocument();
});
```

- [ ] **Step 2: Run to verify fail**

Run (from `frontend/`): `pnpm exec vitest run src/routes/AdminPendingUsers.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement + CSS**

```tsx
// frontend/src/routes/AdminPendingUsers.tsx
import { useState } from "react";
import { Link } from "react-router-dom";

import type { AdminPendingUser } from "../api/admin";
import { useAdminPendingUsers, useApproveUser, useDenyUser } from "../api/admin";
import styles from "./AdminPendingUsers.module.css";

function PendingRow({ u }: { u: AdminPendingUser }) {
  const [pw, setPw] = useState("");
  const approve = useApproveUser();
  const deny = useDenyUser();
  const busy = approve.isPending || deny.isPending;
  const err = approve.error || deny.error;
  // Local (non-Google) accounts need an admin-issued password to log in.
  const needsPw = !u.google_linked;
  const doApprove = (role: "member" | "admin") =>
    approve.mutate({ id: u.id, role, initial_password: needsPw ? pw : undefined });
  return (
    <article className={styles.card}>
      <p className={styles.head}>
        <strong>{u.email}</strong>
        {u.name ? ` — ${u.name}` : ""}
        {u.google_linked && <small className={styles.tag}> (Google)</small>}
        <small className={styles.tag}> · registered {new Date(u.created_at).toLocaleDateString()}</small>
      </p>
      {needsPw && (
        <input
          type="password"
          value={pw}
          onChange={(e) => setPw(e.target.value)}
          placeholder="initial password"
          required
        />
      )}
      <div className={styles.actions}>
        <button type="button" disabled={busy || (needsPw && !pw)} onClick={() => doApprove("member")}>
          Approve as member
        </button>
        <button type="button" disabled={busy || (needsPw && !pw)} onClick={() => doApprove("admin")}>
          Approve as admin
        </button>
        <button type="button" className={styles.secondary} disabled={busy} onClick={() => deny.mutate({ id: u.id })}>
          Deny
        </button>
      </div>
      {err && (
        <span role="alert" className={styles.err}>
          {(err as Error).message}
        </span>
      )}
    </article>
  );
}

export function AdminPendingUsers() {
  const { data, isLoading, isError } = useAdminPendingUsers(1);
  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <p role="alert">Couldn't load pending accounts.</p>;
  const users = data.users;
  return (
    <main className={styles.page}>
      <h1>Pending accounts</h1>
      {users.length === 0 && <p>No accounts awaiting approval.</p>}
      {users.map((u) => (
        <PendingRow key={u.id} u={u} />
      ))}
      <p className={styles.nav}>
        <Link to="/admin/users">All users</Link>
        {" · "}
        <a href="/admin">Admin home</a>
      </p>
    </main>
  );
}
```

```css
/* frontend/src/routes/AdminPendingUsers.module.css */
.page { max-width: 48rem; margin: 0 auto; padding: 1rem 0.5rem 3rem; }
.page h1 { color: var(--app-heading); }
.card { border: 1px solid var(--app-border); border-radius: 8px; padding: 1rem; margin: 0.75rem 0; background: var(--app-sidebar); }
.head { margin: 0 0 0.5rem; }
.tag { color: var(--app-muted); }
.card input { padding: 0.3rem 0.5rem; border: 1px solid var(--app-border); border-radius: 4px; margin-bottom: 0.5rem; }
.actions { display: flex; flex-wrap: wrap; gap: 0.4rem; }
.actions button { border-radius: 5px; padding: 0.35rem 0.8rem; background: var(--app-accent); color: #fff; border: none; cursor: pointer; }
.actions button:disabled { opacity: 0.5; cursor: default; }
.secondary { background: var(--app-sidebar) !important; color: var(--app-text) !important; border: 1px solid var(--app-border) !important; }
.nav { font-size: 0.85rem; margin-top: 1rem; }
.nav a { color: var(--app-link); text-decoration: none; }
.err { display: block; margin-top: 0.4rem; color: var(--app-danger, #e5484d); font-size: 0.8rem; }
```

- [ ] **Step 4: Run to verify pass + tsc**

Run (from `frontend/`): `pnpm exec vitest run src/routes/AdminPendingUsers.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS, tsc 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/routes/AdminPendingUsers.tsx frontend/src/routes/AdminPendingUsers.module.css frontend/src/routes/AdminPendingUsers.test.tsx
git commit -m "feat(spa): AdminPendingUsers approval queue route"
```

---

### Task 8: Wire routes + manifest + routes regen

**Files:**
- Modify: `frontend/src/router.tsx`
- Modify: `frontend/routes.json`
- Modify (generated): `frontend/src/routes.gen.ts`, `src/call_manager/web/spa_routes.py`
- Test: `frontend/src/router.test.tsx` (existing set-equality guard — must pass unchanged)

**Interfaces:**
- Consumes: `AdminUsers`, `AdminPendingUsers`, `RequireAdmin`.
- Produces: `/admin/users` SPA-owned prefix; the two React routes mounted behind `RequireAdmin`.

- [ ] **Step 1: Add `/admin/users` to the manifest**

In `frontend/routes.json`, add `"/admin/users"` to `spa_routes`:
```json
"spa_routes": ["/", "/cockpit", "/calls", "/me", "/profiles", "/admin/users"],
```
(Leave `backend_prefixes` unchanged — bare `/admin` stays a backend prefix.)

- [ ] **Step 2: Regenerate the route artifacts**

Run: `uv run python scripts/gen_routes.py`
This rewrites `frontend/src/routes.gen.ts` and `src/call_manager/web/spa_routes.py` to include `/admin/users`.

- [ ] **Step 3: Wire the router** — in `frontend/src/router.tsx`, add imports and the two child routes (behind `RequireAdmin`):

Imports:
```tsx
import { AdminPendingUsers } from "./routes/AdminPendingUsers";
import { AdminUsers } from "./routes/AdminUsers";
import { RequireAdmin } from "./routes/RequireAdmin";
```
Child routes (add alongside the others, before the `"*"` soft-404):
```tsx
      { path: "admin/users", element: <RequireAdmin><AdminUsers /></RequireAdmin> },
      { path: "admin/users/pending", element: <RequireAdmin><AdminPendingUsers /></RequireAdmin> },
```

- [ ] **Step 4: Run the drift guard + tsc + full FE suite**

Run (from `frontend/`): `pnpm exec vitest run src/router.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS — `leafPaths` yields `/admin/users` and `/admin/users/pending`, both collapsing via `toOwningPrefix` to the new `/admin/users` manifest entry; the set equals `SPA_ROUTES`.

Run: `uv run python scripts/gen_routes.py --check`
Expected: artifacts in sync.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/router.tsx frontend/routes.json frontend/src/routes.gen.ts src/call_manager/web/spa_routes.py
git commit -m "feat(spa): own /admin/users in the manifest + mount AdminUsers/Pending behind RequireAdmin"
```

---

### Task 9: Jinja teardown (trim routes, delete templates/tests, route-absence guard, final regen)

**Files:**
- Modify: `src/call_manager/web/routes/admin.py` (remove the 8 user-mgmt functions + `_lock_active_admin_count` + now-unused imports)
- Delete: `src/call_manager/web/templates/admin_users.html`, `src/call_manager/web/templates/admin_users_pending.html`
- Delete: `tests/test_web_admin_users.py`, `tests/test_web_admin_passwords.py`
- Create: `tests/test_admin_users_jinja_surface_absent.py`
- Modify (generated): `frontend/src/api/schema.gen.ts` (final regen — drops the 8 Jinja paths)

**Interfaces:**
- Consumes: nothing new — this is removal + a guard.
- Produces: the eight Jinja `/admin/users*` route paths are gone; `/api/admin/users*` remain; the rest of `/admin` (Slice 2) is untouched.

**Coverage check before deleting the two Jinja test files** — every behavior they assert is already covered in `tests/test_api_admin_users.py`:
`pending lists` → `test_pending_list_shape`; `approve member/admin` → `test_approve_member_and_admin`; `approve rejects non-pending` → `test_approve_rejects_non_pending`; `deny only pending` → `test_deny_only_pending`; `enable only disabled` → `test_enable_only_disabled`; `disable bumps epoch` → `test_disable_bumps_epoch`; `change-role demote guarded` → `test_change_role_demote_last_admin_guarded`; `cannot disable last admin` → `test_cannot_disable_last_active_admin`; `non-admin blocked` → `test_reads_require_admin_403`; `local approve requires pw + epoch` → `test_approve_local_requires_password_then_bumps_epoch`; `oauth approve w/o pw` → `test_approve_oauth_without_password_ok`; `reset bumps epoch` → `test_reset_password_bumps_epoch`; `reset rejects short` → `test_reset_rejects_weak_400`. No coverage is lost.

- [ ] **Step 1: Write the route-absence guard test**

```python
# tests/test_admin_users_jinja_surface_absent.py
def test_no_jinja_admin_user_routes_remain(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    from call_manager.main import create_app

    app = create_app()
    paths = {r.path for r in app.routes}
    # Jinja user-mgmt routes gone:
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
    # /api routes present:
    assert "/api/admin/users" in paths
    assert "/api/admin/users/pending" in paths
    assert "/api/admin/users/{user_id}/approve" in paths
    # Slice-2 admin routes still present (we only removed user-mgmt):
    assert "/admin" in paths
    assert "/admin/rerank" in paths
    assert "/admin/golden-set" in paths
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_admin_users_jinja_surface_absent.py -q`
Expected: FAIL (the Jinja `/admin/users*` routes still exist).

- [ ] **Step 3: Trim `web/routes/admin.py`**

Delete these route functions and the helper: `pending_users`, `list_users`, `approve_user`, `deny_user`, `disable_user`, `enable_user`, `change_role`, `reset_password`, and `_lock_active_admin_count`. Keep everything else (dashboard, drafting, rerank, digest, sources). Then remove imports that are now unused — **`WeakPassword`, `hash_password`** (only the deleted approve/reset used them). Verify with ruff:
```
uv run ruff check src/call_manager/web/routes/admin.py
```
Fix any F401 it reports. (`Form`, `quote`, `HTTPException`, `RedirectResponse`, `User`, `func`/`or_`/`select`/`text`, and the model imports remain in use by the retained Slice-2 routes — do **not** remove those.)

- [ ] **Step 4: Delete the templates and old Jinja tests**

```bash
git rm src/call_manager/web/templates/admin_users.html src/call_manager/web/templates/admin_users_pending.html
git rm tests/test_web_admin_users.py tests/test_web_admin_passwords.py
```

- [ ] **Step 5: Final OpenAPI regen** (drops the 8 stale Jinja paths from the schema)

Run: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`
Then confirm the Jinja user paths are gone but the `/api` ones stay:
```
grep -nE '"/admin/users' frontend/src/api/schema.gen.ts   # expect: no matches
grep -nE '"/api/admin/users' frontend/src/api/schema.gen.ts # expect: present
```

- [ ] **Step 6: Run the guard + the migrated /api tests + ruff**

Run:
```
uv run pytest tests/test_admin_users_jinja_surface_absent.py tests/test_api_admin_users.py -q
uv run ruff check src/call_manager/web/routes/admin.py src/call_manager/web/api/admin_users.py
```
Expected: PASS, ruff clean.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(teardown): delete Jinja /admin/users routes/templates/tests; route-absence guard; regen schema"
```

---

## Verification (whole-slice gate — run before the final review)

From the worktree root:

```bash
# Backend (targeted — testcontainers is flaky under whole-suite load)
uv run pytest tests/test_api_admin_users.py tests/test_admin_users_jinja_surface_absent.py -q
uv run ruff check src/call_manager/web/api/admin_users.py src/call_manager/web/routes/admin.py

# Routes/schema in sync
uv run python scripts/gen_routes.py --check
grep -c '"/admin/users' frontend/src/api/schema.gen.ts   # expect 0 (Jinja paths gone)

# Frontend
cd frontend
pnpm exec vitest run src/api/admin.test.tsx src/routes/RequireAdmin.test.tsx src/routes/AdminUsers.test.tsx src/routes/AdminPendingUsers.test.tsx src/router.test.tsx
pnpm exec tsc --noEmit
pnpm build
```

All green = ready for the final whole-branch review. **Do NOT merge** — a human merges after independent verification.

## Self-Review notes (author)

- **Spec coverage:** every spec endpoint (2 reads + 6 mutations), the parity invariants (last-admin guard, epoch bumps, no-reset-status-gate, role-before-lookup ordering, role default), the React guard + both-list invalidation, the manifest prefix, and the teardown all map to tasks T1–T9.
- **Ordering locks present:** `test_approve_invalid_role_on_nonexistent_is_400_not_404`, `test_change_role_invalid_role_on_nonexistent_is_400`, `test_reset_password_has_no_status_gate`.
- **No new schema, no new dependency.** The only generated files touched are `schema.gen.ts` (T4 + T9), `routes.gen.ts`/`spa_routes.py` (T8).
- **Known interim:** no SPA ActivityBar admin entry (Slice 2); entry stays via the Jinja `/admin` dashboard links, which now cross-doc-land on the React pages.
