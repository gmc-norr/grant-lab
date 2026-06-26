# Account / Settings React Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the Jinja `/me` settings page (name / password / digest subscriptions) to a React `/me` route over additive typed `/api/me/*` endpoints, then delete the Jinja route/template/module.

**Architecture:** Strangler-fig parity port (same as calls-detail) — additive `/api/me/{settings,name,password,subscriptions}` → React `/me` under the AppShell → per-slice Jinja teardown. The password endpoint re-mints the session cookie on its JSON response (bumps `session_epoch` to revoke other sessions). The OAuth "Connect Google" stays a native server-side form POST. No DB schema change.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x (no schema change) · React 19 / Vite / TanStack Query / openapi-fetch · pytest (+ testcontainers Postgres) · vitest.

**Source spec:** `docs/superpowers/specs/2026-06-26-account-settings-react-migration-design.md` (`a47c0ad`, Codex-revised).

## Global Constraints

- **No DB schema change. No new dependency.** Additive `/api`, then teardown.
- **Frontend module/hook names are account-scoped:** the new hook file is `frontend/src/api/account.ts` with `useAccountSettings` / `useUpdateName` / `useChangePassword` / `useSaveSubscriptions`. **Do NOT name it `api/settings.ts` or `useSettings`** — those already exist (cockpit workspace settings, `useSettings(id)`) and would collide.
- **Backend response models are namespaced** `Account*` / `UpdateName*` / `ChangePassword*` / `Subscriptions*` (FastAPI auto-suffixes a collision to `…1` and breaks the TS types). Verify against `schema.gen.ts` during Task 4's regen.
- **Errors via `raise HTTPException(status_code=…, detail=…)`** — the global handler (`main.py:333`) maps `/api` paths to the envelope `{"error":{"code","message"}}` (`code_for(400)=="bad_request"`, `401=="unauthorized"`). Never hand-build an envelope.
- **CSRF:** the new mutations are unsafe `/api` methods, so `OriginCSRFMiddleware` applies — but it 403s only a *present-but-mismatched* / `"null"` Origin; an **absent** Origin is allowed (`auth/csrf.py:122-124`). So TestClient mutation tests pass **without** an Origin header (as the calls-detail hide tests do). Do not add Origin headers to the tests.
- **Manifest is prefix-not-`:param`:** add the prefix `/me` to `frontend/routes.json` `spa_routes`; the React route is `path: "me"`. `router.test.tsx` is set-equality, so the route + the manifest prefix land in **one commit** (Task 7).
- **Two regen scripts:** `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh` → `schema.gen.ts` only (the `bge-m3` override avoids the mxbai `.env` stdout WARNING). `uv run python scripts/gen_routes.py` → `routes.gen.ts` + `spa_routes.py` only.
- **Ordering:** additive `/api` + React route + manifest `/me` FIRST (Tasks 1–7); Jinja deletion + `backend_prefixes` `/me` removal LAST (Task 8). While the Jinja `/me` route exists it wins over the catch-all, so the manifest prefix is inert mid-slice.
- **`/api/me/*` tests live in `tests/` ROOT** (`tests/test_api_me*.py` exists there); psycopg3 only (`pg_url` / `postgresql+psycopg://`).
- **Frontend `tsc` (via `pnpm build`) is the gate** (no TS linter). SPA styling = CSS Modules + `--app-*` theme tokens (`--app-border`, `--app-link`, `--app-muted`, `--app-heading`, `--app-text`, `--app-good`, `--app-bad`, `--app-accent`, `--app-sidebar`, `--app-hover`, `--app-border-strong`); never the Jinja globals.
- **Subagent git discipline (detached SDD):** `cd <worktree>`, `git -C <worktree>`, verify `feat/…` branch before any commit; `git rm` for deletions.

---

### Task 1: `GET /api/me/settings` read-model

**Files:**
- Modify: `src/call_manager/web/api/me.py`
- Test: `tests/test_api_me_settings.py` (new)

**Interfaces:**
- Produces: route `GET /api/me/settings` → `AccountSettingsOut` `{name: str|None, email, is_admin, google_oidc_enabled, google_linked, has_password, profiles: [AccountProfileOut{id,name,subscribed}]}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_me_settings.py`:

```python
from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Profile, ProfileKind, Subscription, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _login(app, pg_session_factory, *, name=None, is_admin=False):
    with pg_session_factory() as s:
        u = User(status="active", email="u@ki.se", name=name, is_admin=is_admin)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client, uid


def _profile(pg_session_factory, *, owner_id, name, active=True):
    with pg_session_factory() as s:
        p = Profile(
            name=name,
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level="group",
            description="d",
            structured={},
            created_by=owner_id,
            is_active=active,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def test_settings_read_model(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory, name="Pär", is_admin=True)
    pid_a = _profile(pg_session_factory, owner_id=uid, name="Group A")
    pid_b = _profile(pg_session_factory, owner_id=uid, name="Group B")
    _profile(pg_session_factory, owner_id=uid, name="Archived", active=False)
    with pg_session_factory() as s:
        s.add(Subscription(user_id=uid, profile_id=pid_a))
        s.commit()
    with client:
        r = client.get("/api/me/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Pär"
    assert body["email"] == "u@ki.se"
    assert body["is_admin"] is True
    assert body["has_password"] is False  # OAuth/passwordless test user
    # active profiles only, ordered by name, with subscribed flags
    assert [(p["name"], p["subscribed"]) for p in body["profiles"]] == [
        ("Group A", True),
        ("Group B", False),
    ]
    assert all(p["id"] in (pid_a, pid_b) for p in body["profiles"])


def test_settings_requires_auth(monkeypatch, pg_url):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/api/me/settings")
    assert r.status_code == 401
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_api_me_settings.py -q`
Expected: FAIL — 404 (no `/api/me/settings` route).

- [ ] **Step 3: Add the read-model to `web/api/me.py`**

Add imports:

```python
from sqlalchemy import select

from call_manager.db.models import Profile, Subscription, User  # User already imported
```

Add the models + endpoint (after the existing `MeOut`/`get_me`):

```python
class AccountProfileOut(BaseModel):
    id: int
    name: str
    subscribed: bool


class AccountSettingsOut(BaseModel):
    name: str | None
    email: str
    is_admin: bool
    google_oidc_enabled: bool
    google_linked: bool
    has_password: bool
    profiles: list[AccountProfileOut]


@router.get("/me/settings", response_model=AccountSettingsOut)
def get_settings(request: Request, user: User = Depends(get_current_user)):  # noqa: B008
    sf = request.app.state.session_factory
    with sf() as session:
        profiles = list(
            session.execute(
                select(Profile).where(Profile.is_active.is_(True)).order_by(Profile.name)
            ).scalars()
        )
        subscribed_ids = set(
            session.execute(
                select(Subscription.profile_id).where(Subscription.user_id == user.id)
            ).scalars()
        )
        profile_out = [
            AccountProfileOut(id=p.id, name=p.name, subscribed=(p.id in subscribed_ids))
            for p in profiles
        ]
    return AccountSettingsOut(
        name=user.name,
        email=user.email,
        is_admin=user.is_admin,
        google_oidc_enabled=request.app.state.settings.google_oidc_enabled,
        google_linked=user.oidc_sub is not None,
        has_password=user.password_hash is not None,
        profiles=profile_out,
    )
```

(`user` from `get_current_user` carries `name`/`email`/`is_admin`/`oidc_sub`/`password_hash` — the Jinja `me_page` reads them the same way, no re-fetch needed.)

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/test_api_me_settings.py -q && uv run ruff check src/call_manager/web/api/me.py`
Expected: PASS (2); ruff clean.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/api/me.py tests/test_api_me_settings.py
git commit -m "feat(api): GET /api/me/settings read-model (name/flags/profiles+subscribed)"
```

---

### Task 2: `POST /api/me/name`

**Files:**
- Modify: `src/call_manager/web/api/me.py`
- Test: `tests/test_api_me_name.py` (new)

**Interfaces:**
- Produces: route `POST /api/me/name` body `{name: str}` → `UpdateNameOut {name: str|None}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_me_name.py`:

```python
from fastapi.testclient import TestClient

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _login(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="u@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client, uid


def test_update_name_strips(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    with client:
        r = client.post("/api/me/name", json={"name": "  Pär  "})
    assert r.status_code == 200
    assert r.json() == {"name": "Pär"}
    with pg_session_factory() as s:
        assert s.get(User, uid).name == "Pär"


def test_update_name_empty_becomes_null(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    with client:
        r = client.post("/api/me/name", json={"name": "   "})
    assert r.status_code == 200
    assert r.json() == {"name": None}


def test_update_name_requires_auth(monkeypatch, pg_url):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post("/api/me/name", json={"name": "x"})
    assert r.status_code == 401
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_api_me_name.py -q`
Expected: FAIL — 404/405.

- [ ] **Step 3: Add the endpoint**

Add `HTTPException` to the fastapi import. Add:

```python
class UpdateNameIn(BaseModel):
    name: str


class UpdateNameOut(BaseModel):
    name: str | None


@router.post("/me/name", response_model=UpdateNameOut)
def update_name(
    request: Request,
    body: UpdateNameIn,
    user: User = Depends(get_current_user),  # noqa: B008
):
    sf = request.app.state.session_factory
    with sf() as session:
        db_user = session.get(User, user.id)
        if db_user is None:
            raise HTTPException(status_code=401, detail="user not found")
        db_user.name = body.name.strip() or None
        new_name = db_user.name
        session.commit()
    return UpdateNameOut(name=new_name)
```

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/test_api_me_name.py -q && uv run ruff check src/call_manager/web/api/me.py`
Expected: PASS (3); ruff clean.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/api/me.py tests/test_api_me_name.py
git commit -m "feat(api): POST /api/me/name (strip->None, 401 on missing user)"
```

---

### Task 3: `POST /api/me/password` (re-mint cookie + bump epoch)

**Files:**
- Modify: `src/call_manager/web/api/me.py`
- Test: `tests/test_api_me_password.py` (new)

**Interfaces:**
- Produces: route `POST /api/me/password` body `{current_password, new_password}` → `ChangePasswordOut {ok: bool}`; sets `Set-Cookie: session=…` (re-mint) on success.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_me_password.py`:

```python
from fastapi.testclient import TestClient

from call_manager.auth.passwords import hash_password
from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _login(app, pg_session_factory, *, password_hash=None, oidc=False):
    with pg_session_factory() as s:
        u = User(
            status="active",
            email="u@ki.se",
            password_hash=password_hash,
            session_epoch=0,
            oidc_iss=("https://accounts.google.com" if oidc else None),
            oidc_sub=("sub-1" if oidc else None),
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid, session_epoch=0))
    return client, uid


def test_password_change_remints_cookie_and_bumps_epoch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory, password_hash=hash_password("oldpassword12"))
    with client:
        r = client.post(
            "/api/me/password",
            json={"current_password": "oldpassword12", "new_password": "newlongpassword"},
        )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    setc = r.headers["set-cookie"].lower()
    assert "httponly" in setc
    assert "samesite=lax" in setc
    assert f"max-age={app.state.settings.session_max_age_seconds}" in setc
    assert ("secure" in setc) == app.state.settings.cookie_secure
    with pg_session_factory() as s:
        assert s.get(User, uid).session_epoch == 1


def test_password_wrong_current_400(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory, password_hash=hash_password("rightpassword"))
    with client:
        r = client.post(
            "/api/me/password",
            json={"current_password": "wrongpassword", "new_password": "newlongpassword"},
        )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_password_weak_new_400(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory, password_hash=hash_password("oldpassword12"))
    with client:
        r = client.post(
            "/api/me/password", json={"current_password": "oldpassword12", "new_password": "a"}
        )
    assert r.status_code == 400


def test_password_oauth_only_no_password_400(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory, password_hash=None, oidc=True)
    with client:
        r = client.post(
            "/api/me/password",
            json={"current_password": "anything", "new_password": "newlongpassword"},
        )
    assert r.status_code == 400
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_api_me_password.py -q`
Expected: FAIL — 404/405.

- [ ] **Step 3: Add the endpoint**

Add imports: `Response` to the fastapi import; and:

```python
from call_manager.auth.passwords import WeakPassword, hash_password, verify_password
```

Add:

```python
class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


class ChangePasswordOut(BaseModel):
    ok: bool


@router.post("/me/password", response_model=ChangePasswordOut)
def change_password(
    request: Request,
    body: ChangePasswordIn,
    response: Response,
    user: User = Depends(get_current_user),  # noqa: B008
):
    sf = request.app.state.session_factory
    signer = request.app.state.session_signer
    settings = request.app.state.settings
    with sf() as session:
        db_user = session.get(User, user.id)
        if db_user is None:
            raise HTTPException(status_code=401, detail="user not found")
        # An OAuth-only account (no hash) can't mint a first password from a session.
        if db_user.password_hash is None or not verify_password(
            db_user.password_hash, body.current_password
        ):
            raise HTTPException(status_code=400, detail="current password is incorrect")
        try:
            db_user.password_hash = hash_password(body.new_password)
        except WeakPassword as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        db_user.session_epoch = (db_user.session_epoch or 0) + 1  # revoke other sessions
        new_epoch = db_user.session_epoch
        session.commit()
    # Re-mint THIS browser's session under the new epoch (same cookie attrs as the Jinja route).
    response.set_cookie(
        "session",
        signer.make(user_id=user.id, session_epoch=new_epoch),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    return ChangePasswordOut(ok=True)
```

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/test_api_me_password.py -q && uv run ruff check src/call_manager/web/api/me.py`
Expected: PASS (4); ruff clean.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/api/me.py tests/test_api_me_password.py
git commit -m "feat(api): POST /api/me/password (verify current, weak->400, re-mint cookie + bump epoch)"
```

---

### Task 4: `PUT /api/me/subscriptions` + regenerate OpenAPI types

**Files:**
- Modify: `src/call_manager/web/api/me.py`
- Modify (generated): `frontend/src/api/schema.gen.ts`
- Test: `tests/test_api_me_subscriptions.py` (new)

**Interfaces:**
- Produces: route `PUT /api/me/subscriptions` body `{profile_ids: list[int]}` → `SubscriptionsOut {subscribed: list[int]}`; schema paths for all four new `/api/me/*` endpoints in `schema.gen.ts`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_me_subscriptions.py`:

```python
from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Profile, ProfileKind, Subscription, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _login(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="u@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client, uid


def _profile(pg_session_factory, *, owner_id, name, active=True):
    with pg_session_factory() as s:
        p = Profile(name=name, kind=ProfileKind.ORGANIZATIONAL_UNIT.value, level="group",
                    description="d", structured={}, created_by=owner_id, is_active=active)
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def test_subscriptions_diff_preserves_digest_enabled(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    pid_a = _profile(pg_session_factory, owner_id=uid, name="A")
    pid_b = _profile(pg_session_factory, owner_id=uid, name="B")
    # Pre-existing subscription to A with digest turned OFF — must survive a save that keeps A.
    with pg_session_factory() as s:
        s.add(Subscription(user_id=uid, profile_id=pid_a, digest_enabled=False))
        s.commit()
    with client:
        r = client.put("/api/me/subscriptions", json={"profile_ids": [pid_a, pid_b]})
    assert r.status_code == 200
    assert r.json() == {"subscribed": sorted([pid_a, pid_b])}
    with pg_session_factory() as s:
        row_a = s.execute(
            select(Subscription).where(Subscription.user_id == uid, Subscription.profile_id == pid_a)
        ).scalar_one()
        assert row_a.digest_enabled is False  # NOT reset to default True


def test_subscriptions_empty_clears(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    pid = _profile(pg_session_factory, owner_id=uid, name="A")
    with pg_session_factory() as s:
        s.add(Subscription(user_id=uid, profile_id=pid))
        s.commit()
    with client:
        r = client.put("/api/me/subscriptions", json={"profile_ids": []})
    assert r.status_code == 200
    assert r.json() == {"subscribed": []}
    with pg_session_factory() as s:
        assert s.execute(select(Subscription).where(Subscription.user_id == uid)).first() is None


def test_subscriptions_ignores_inactive(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    archived = _profile(pg_session_factory, owner_id=uid, name="Z", active=False)
    with client:
        r = client.put("/api/me/subscriptions", json={"profile_ids": [archived]})
    assert r.status_code == 200
    assert r.json() == {"subscribed": []}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_api_me_subscriptions.py -q`
Expected: FAIL — 404/405.

- [ ] **Step 3: Add the endpoint**

Add `delete` to the sqlalchemy import (`from sqlalchemy import delete, select`). Add:

```python
class SubscriptionsIn(BaseModel):
    profile_ids: list[int]


class SubscriptionsOut(BaseModel):
    subscribed: list[int]


@router.put("/me/subscriptions", response_model=SubscriptionsOut)
def update_subscriptions(
    request: Request,
    body: SubscriptionsIn,
    user: User = Depends(get_current_user),  # noqa: B008
):
    sf = request.app.state.session_factory
    desired = set(body.profile_ids)
    with sf() as session:
        db_user = session.get(User, user.id)
        if db_user is None:
            raise HTTPException(status_code=401, detail="user not found")
        valid_ids = (
            set(
                session.execute(
                    select(Profile.id).where(
                        Profile.id.in_(desired), Profile.is_active.is_(True)
                    )
                ).scalars()
            )
            if desired
            else set()
        )
        existing_ids = set(
            session.execute(
                select(Subscription.profile_id).where(Subscription.user_id == user.id)
            ).scalars()
        )
        # DIFF, not delete-all-then-reinsert — reinserting would reset digest_enabled.
        to_remove = existing_ids - valid_ids
        to_add = valid_ids - existing_ids
        if to_remove:
            session.execute(
                delete(Subscription).where(
                    Subscription.user_id == user.id,
                    Subscription.profile_id.in_(to_remove),
                )
            )
        for pid in to_add:
            session.add(Subscription(user_id=user.id, profile_id=pid))
        session.commit()
    return SubscriptionsOut(subscribed=sorted(valid_ids))
```

- [ ] **Step 4: Run the test + ruff**

Run: `uv run pytest tests/test_api_me_subscriptions.py -q && uv run ruff check src/call_manager/web/api/me.py`
Expected: PASS (3); ruff clean.

- [ ] **Step 5: Regenerate the OpenAPI TS types**

Run: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`
Then verify the four paths + the namespaced models landed and the frontend compiles:

```bash
for p in settings name password subscriptions; do echo -n "$p: "; grep -c "\"/api/me/$p\"" frontend/src/api/schema.gen.ts; done
grep -c 'AccountSettingsOut' frontend/src/api/schema.gen.ts
cd frontend && pnpm build
```
Expected: each path ≥ 1; `AccountSettingsOut` ≥ 1; build clean. (The Jinja `/me` HTML path is still present — it drops in Task 8.)

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/api/me.py tests/test_api_me_subscriptions.py frontend/src/api/schema.gen.ts
git commit -m "feat(api): PUT /api/me/subscriptions (diff-not-replace) + regen OpenAPI types"
```

---

### Task 5: Frontend hooks — `api/account.ts`

**Files:**
- Create: `frontend/src/api/account.ts`
- Test: `frontend/src/api/account.test.tsx`

**Interfaces:**
- Consumes: `schema.gen.ts` paths `/api/me/{settings,name,password,subscriptions}`; `api`, `unwrap` from `./client`.
- Produces: `useAccountSettings()` (query `["me-settings"]`), `useUpdateName()`, `useChangePassword()`, `useSaveSubscriptions()`; type `AccountSettings = components["schemas"]["AccountSettingsOut"]`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/api/account.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import {
  useAccountSettings,
  useChangePassword,
  useSaveSubscriptions,
  useUpdateName,
} from "./account";

afterEach(() => vi.restoreAllMocks());

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

const SETTINGS = {
  name: "Pär", email: "p@ki.se", is_admin: false,
  google_oidc_enabled: false, google_linked: false, has_password: true, profiles: [],
};

it("useAccountSettings GETs /api/me/settings and unwraps", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: SETTINGS, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useAccountSettings(), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data).toEqual(SETTINGS);
  expect(get).toHaveBeenCalledWith("/api/me/settings", {});
});

it("useUpdateName POSTs the name + invalidates ['me-settings']", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { name: "New" }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useUpdateName(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync("New");
  expect(post).toHaveBeenCalledWith("/api/me/name", { body: { name: "New" } });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["me-settings"] });
});

it("useChangePassword POSTs both passwords", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { ok: true }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useChangePassword(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ current_password: "a", new_password: "blonglongpass" });
  expect(post).toHaveBeenCalledWith("/api/me/password", {
    body: { current_password: "a", new_password: "blonglongpass" },
  });
});

it("useSaveSubscriptions PUTs the id set + invalidates ['me-settings']", async () => {
  const put = vi.spyOn(clientMod.api, "PUT").mockResolvedValue({ data: { subscribed: [1] }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useSaveSubscriptions(), { wrapper: wrapper(qc) });
  await result.current.mutateAsync([1]);
  expect(put).toHaveBeenCalledWith("/api/me/subscriptions", { body: { profile_ids: [1] } });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["me-settings"] });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/api/account.test.tsx`
Expected: FAIL — cannot resolve `./account`.

- [ ] **Step 3: Create the hooks**

Create `frontend/src/api/account.ts`:

```ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type AccountSettings = components["schemas"]["AccountSettingsOut"];

export function useAccountSettings() {
  return useQuery({
    queryKey: ["me-settings"],
    queryFn: async () => unwrap(await api.GET("/api/me/settings", {})),
    // House pattern (session.ts, calls.ts, editor.ts, …): no retry so an
    // unauthenticated load redirects via unwrap immediately, not after 3 retries.
    retry: false,
  });
}

export function useUpdateName() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (name: string) => unwrap(await api.POST("/api/me/name", { body: { name } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["me-settings"] }),
  });
}

export function useChangePassword() {
  return useMutation({
    mutationFn: async (vars: { current_password: string; new_password: string }) =>
      unwrap(await api.POST("/api/me/password", { body: vars })),
  });
}

export function useSaveSubscriptions() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (profile_ids: number[]) =>
      unwrap(await api.PUT("/api/me/subscriptions", { body: { profile_ids } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["me-settings"] }),
  });
}
```

- [ ] **Step 4: Run the test + tsc**

Run: `cd frontend && pnpm exec vitest run src/api/account.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS (4); tsc clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/account.ts frontend/src/api/account.test.tsx
git commit -m "feat(spa): account hooks (useAccountSettings/UpdateName/ChangePassword/SaveSubscriptions)"
```

---

### Task 6: `Settings` route component

**Files:**
- Create: `frontend/src/routes/Settings.tsx`
- Create: `frontend/src/routes/Settings.module.css`
- Test: `frontend/src/routes/Settings.test.tsx`

**Interfaces:**
- Consumes: the Task-5 hooks; `renderWithProviders` (tests).
- Produces: `export function Settings()` — registered at `me` in Task 7.

**Styling:** sibling `Settings.module.css` with `--app-*` tokens (the CallDetail/CallRow pattern). The score-band-style `data-*` hooks aren't needed here; tests assert text/roles.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/routes/Settings.test.tsx`:

```tsx
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { Settings } from "./Settings";

afterEach(() => vi.restoreAllMocks());

const SETTINGS = {
  name: "Pär", email: "p@ki.se", is_admin: false,
  google_oidc_enabled: true, google_linked: false, has_password: true,
  profiles: [
    { id: 1, name: "Group A", subscribed: true },
    { id: 2, name: "Group B", subscribed: false },
  ],
};

function mockGet(payload = SETTINGS) {
  return vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: payload, error: undefined } as never);
}

it("renders the sections from the settings read-model", async () => {
  mockGet();
  renderWithProviders(<Settings />);
  expect(await screen.findByDisplayValue("Pär")).toBeInTheDocument();
  expect(screen.getByText("p@ki.se")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: /change password/i })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: /google sign-in/i })).toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: /Group A/ })).toBeChecked();
  expect(screen.getByRole("checkbox", { name: /Group B/ })).not.toBeChecked();
});

it("hides the password + google sections when the flags are off", async () => {
  mockGet({ ...SETTINGS, has_password: false, google_oidc_enabled: false });
  renderWithProviders(<Settings />);
  await screen.findByDisplayValue("Pär");
  expect(screen.queryByRole("heading", { name: /change password/i })).toBeNull();
  expect(screen.queryByRole("heading", { name: /google sign-in/i })).toBeNull();
});

it("saves the display name", async () => {
  mockGet();
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { name: "New" }, error: undefined } as never);
  renderWithProviders(<Settings />);
  fireEvent.change(await screen.findByDisplayValue("Pär"), { target: { value: "New" } });
  fireEvent.click(screen.getByRole("button", { name: /save name/i }));
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/me/name", { body: { name: "New" } }));
});

it("changes the password and shows success; surfaces an error", async () => {
  mockGet();
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { ok: true }, error: undefined } as never);
  renderWithProviders(<Settings />);
  await screen.findByDisplayValue("Pär");
  fireEvent.change(screen.getByLabelText(/current password/i), { target: { value: "old" } });
  fireEvent.change(screen.getByLabelText(/new password/i), { target: { value: "newlongpassword" } });
  fireEvent.click(screen.getByRole("button", { name: /change password/i }));
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/me/password", {
    body: { current_password: "old", new_password: "newlongpassword" },
  }));
  expect(await screen.findByText(/password changed/i)).toBeInTheDocument();
});

it("shows the password error message from the envelope", async () => {
  mockGet();
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: undefined,
    error: { error: { code: "bad_request", message: "current password is incorrect" } },
  } as never);
  renderWithProviders(<Settings />);
  await screen.findByDisplayValue("Pär");
  fireEvent.change(screen.getByLabelText(/current password/i), { target: { value: "x" } });
  fireEvent.change(screen.getByLabelText(/new password/i), { target: { value: "newlongpassword" } });
  fireEvent.click(screen.getByRole("button", { name: /change password/i }));
  expect(await screen.findByText(/current password is incorrect/i)).toBeInTheDocument();
});

it("saves subscriptions as the checked profile id set", async () => {
  mockGet();
  const put = vi.spyOn(clientMod.api, "PUT").mockResolvedValue({ data: { subscribed: [1, 2] }, error: undefined } as never);
  renderWithProviders(<Settings />);
  fireEvent.click(await screen.findByRole("checkbox", { name: /Group B/ }));
  fireEvent.click(screen.getByRole("button", { name: /save subscriptions/i }));
  await waitFor(() => expect(put).toHaveBeenCalledWith("/api/me/subscriptions", { body: { profile_ids: [1, 2] } }));
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/routes/Settings.test.tsx`
Expected: FAIL — cannot resolve `./Settings`.

- [ ] **Step 3: Create the CSS Module**

Create `frontend/src/routes/Settings.module.css`:

```css
.settings {
  max-width: 40rem;
  margin: 0 auto;
  padding: 1rem 0.5rem 3rem;
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
}
.header h1 { margin: 0 0 0.25rem; color: var(--app-heading); }
.sub { margin: 0; color: var(--app-muted); font-size: 0.9rem; }
.sub code { background: var(--app-hover); padding: 0.05rem 0.3rem; border-radius: 3px; }
.muted { color: var(--app-muted); font-size: 0.85rem; }

.card {
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 1rem;
  background: var(--app-sidebar);
}
.card h2 { margin: 0 0 0.6rem; font-size: 1rem; color: var(--app-heading); }

.label { display: flex; flex-direction: column; gap: 0.25rem; font-size: 0.85rem; margin-bottom: 0.6rem; }
.label input {
  background: var(--app-editor, var(--app-hover));
  border: 1px solid var(--app-border-strong);
  border-radius: 5px;
  padding: 0.4rem 0.55rem;
  color: var(--app-text);
  font: inherit;
}
.label input:focus { outline: 2px solid var(--app-accent); outline-offset: 1px; }

.fieldset { border: none; margin: 0 0 0.6rem; padding: 0; display: flex; flex-direction: column; gap: 0.35rem; }
.check { display: flex; align-items: center; gap: 0.45rem; font-size: 0.9rem; }

button {
  background: var(--app-accent);
  color: #fff;
  border: none;
  border-radius: 5px;
  padding: 0.4rem 0.9rem;
  font: inherit;
  font-weight: 500;
  cursor: pointer;
}
button:disabled { opacity: 0.6; cursor: default; }

.ok { margin-left: 0.6rem; color: var(--app-good); font-size: 0.85rem; }
.err { margin-left: 0.6rem; color: var(--app-bad); font-size: 0.85rem; }
```

- [ ] **Step 4: Create the component**

Create `frontend/src/routes/Settings.tsx`:

```tsx
import { useState } from "react";

import type { AccountSettings } from "../api/account";
import {
  useAccountSettings,
  useChangePassword,
  useSaveSubscriptions,
  useUpdateName,
} from "../api/account";
import styles from "./Settings.module.css";

export function Settings() {
  const { data, isLoading, isError } = useAccountSettings();
  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <p role="alert">Couldn’t load settings.</p>;
  return <SettingsForm data={data} />;
}

function SettingsForm({ data }: { data: AccountSettings }) {
  return (
    <main className={styles.settings}>
      <header className={styles.header}>
        <h1>Settings</h1>
        <p className={styles.sub}>
          Signed in as <code>{data.email}</code>
          {data.is_admin && (
            <>
              {" · "}
              <span className={styles.muted}>admin</span>
            </>
          )}
          .
        </p>
      </header>
      <NameSection initial={data.name ?? ""} />
      {data.google_oidc_enabled && <GoogleSection linked={data.google_linked} />}
      {data.has_password && <PasswordSection />}
      <SubscriptionsSection profiles={data.profiles} />
    </main>
  );
}

function NameSection({ initial }: { initial: string }) {
  const [name, setName] = useState(initial);
  const update = useUpdateName();
  return (
    <section className={styles.card}>
      <h2>Display name</h2>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          update.mutate(name);
        }}
      >
        <label className={styles.label}>
          How should we address you?
          <input
            type="text"
            value={name}
            maxLength={200}
            placeholder="e.g. Pär Larsson"
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <button type="submit" disabled={update.isPending}>Save name</button>
        {update.isSuccess && (
          <span className={styles.ok} role="status">Saved.</span>
        )}
      </form>
    </section>
  );
}

function GoogleSection({ linked }: { linked: boolean }) {
  return (
    <section className={styles.card}>
      <h2>Google sign-in</h2>
      {linked ? (
        <p>
          Google sign-in: <strong>connected</strong>.
        </p>
      ) : (
        // Native server-side OAuth start — full-page POST, not /api. Owned by the auth slice.
        <form method="post" action="/auth/google/start">
          <input type="hidden" name="link" value="1" />
          <button type="submit">Connect Google account</button>
        </form>
      )}
    </section>
  );
}

function PasswordSection() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const change = useChangePassword();
  return (
    <section className={styles.card}>
      <h2>Change password</h2>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          change.mutate(
            { current_password: current, new_password: next },
            { onSuccess: () => { setCurrent(""); setNext(""); } },
          );
        }}
      >
        <label className={styles.label}>
          Current password
          <input
            type="password"
            autoComplete="current-password"
            required
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </label>
        <label className={styles.label}>
          New password
          <input
            type="password"
            autoComplete="new-password"
            required
            value={next}
            onChange={(e) => setNext(e.target.value)}
          />
        </label>
        <button type="submit" disabled={change.isPending}>Change password</button>
        {change.isSuccess && (
          <span className={styles.ok} role="status">Password changed.</span>
        )}
        {change.isError && (
          <span className={styles.err} role="alert">
            {(change.error as Error | null)?.message ?? "Couldn’t change password."}
          </span>
        )}
      </form>
    </section>
  );
}

function SubscriptionsSection({ profiles }: { profiles: AccountSettings["profiles"] }) {
  const [checked, setChecked] = useState<Set<number>>(
    () => new Set(profiles.filter((p) => p.subscribed).map((p) => p.id)),
  );
  const save = useSaveSubscriptions();
  if (profiles.length === 0) {
    return (
      <section className={styles.card}>
        <h2>Weekly digest subscriptions</h2>
        <p className={styles.muted}>
          No profiles to subscribe to yet — <a href="/profiles/new">create one</a>.
        </p>
      </section>
    );
  }
  return (
    <section className={styles.card}>
      <h2>Weekly digest subscriptions</h2>
      <p className={styles.muted}>
        Check the profiles whose Monday-morning digest you want emailed to you.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate([...checked]);
        }}
      >
        <fieldset className={styles.fieldset}>
          {profiles.map((p) => (
            <label key={p.id} className={styles.check}>
              <input
                type="checkbox"
                checked={checked.has(p.id)}
                onChange={(e) =>
                  setChecked((prev) => {
                    const nextSet = new Set(prev);
                    if (e.target.checked) nextSet.add(p.id);
                    else nextSet.delete(p.id);
                    return nextSet;
                  })
                }
              />
              {p.name}
            </label>
          ))}
        </fieldset>
        <button type="submit" disabled={save.isPending}>Save subscriptions</button>
        {save.isSuccess && (
          <span className={styles.ok} role="status">Saved.</span>
        )}
      </form>
    </section>
  );
}
```

- [ ] **Step 5: Run the test + tsc**

Run: `cd frontend && pnpm exec vitest run src/routes/Settings.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS (6); tsc clean.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/routes/Settings.tsx frontend/src/routes/Settings.module.css frontend/src/routes/Settings.test.tsx
git commit -m "feat(spa): Settings route component + module CSS (name/google/password/subscriptions)"
```

---

### Task 7: Wire the route + manifest prefix + nav flip

Register `me` in the router, add the `/me` SPA-ownership prefix (regenerate), flip the shell nav item to SPA, and update the route-ownership guard tests. The router route + manifest prefix land in this one commit (set-equality drift guard).

**Files:**
- Modify: `frontend/src/router.tsx`, `frontend/routes.json`
- Modify (generated): `frontend/src/routes.gen.ts`, `src/call_manager/web/spa_routes.py`
- Modify: `frontend/src/shell/nav-items.ts`, `frontend/src/shell/ActivityBar.test.tsx`
- Modify: `tests/test_gen_routes.py`, `frontend/src/dev-proxy.test.ts`

- [ ] **Step 1: Add `/me` to the manifest and regenerate**

In `frontend/routes.json`, change the `spa_routes` line:

```json
  "spa_routes": ["/", "/cockpit", "/calls", "/me"],
```

Leave `backend_prefixes` UNCHANGED (`/me` stays — the Jinja `/me` route still exists until Task 8). Then:

Run: `uv run python scripts/gen_routes.py`
Expected: `routes.gen.ts` `SPA_ROUTES` + `spa_routes.py` `SPA_ROUTES` now include `/me`.

- [ ] **Step 2: Register the React route**

In `frontend/src/router.tsx`, add the import and the route child (after `calls/:id`, before `"*"`):

```tsx
import { Settings } from "./routes/Settings";
```

```tsx
      { path: "me", element: <Settings /> },
```

(No `handle` — full-width settings page, no sidebar, like `calls/:id`.)

- [ ] **Step 3: Flip the shell nav item to SPA**

In `frontend/src/shell/nav-items.ts`, change the `settings` entry's `kind`:

```tsx
  { id: "settings", label: "Settings", icon: "gear", kind: "spa", to: "/me" },
```

(`ActivityBar` already renders a `kind:"spa"` bottom item as a `<NavLink to="/me">` — no ActivityBar.tsx change. The `profiles` entry STAYS `external`.)

- [ ] **Step 4: Update the nav test**

In `frontend/src/shell/ActivityBar.test.tsx`, the test asserting `Profiles and Settings still deep-link to their Jinja pages` (Settings `href="/me"`) is now half-wrong — Settings routes in-app. Split/rename it:

```tsx
it("Profiles deep-links to its Jinja page; Settings routes in-app to /me", () => {
  // (keep the existing render setup from this test)
  expect(screen.getByRole("link", { name: "Profiles" })).toHaveAttribute("href", "/profiles");
  expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/me");
});
```

(The href is unchanged — a `NavLink` still renders `<a href="/me">` — so the assertion holds; only the name/intent updates.)

- [ ] **Step 5: Update the route-ownership guard tests (`/me` now SPA-owned)**

In `tests/test_gen_routes.py`, in `test_is_spa_route_is_exact_not_prefix`, add the `/me` assertion (it is now an owned prefix; `test_root_owns_only_itself` is unaffected):

```python
    assert is_spa_route("/me") is True  # /me is now an SPA-owned prefix (account/settings)
```

In `frontend/src/dev-proxy.test.ts`:
- In the "owns the root and the /calls prefix" test, add `expect(isSpaRoute("/me")).toBe(true);` and rename it to include `/me`.
- In the `shouldServeViaVite` test, add `expect(shouldServeViaVite("/me")).toBe(true); // owned settings route`.
- LEAVE the explicit-manifest branch tests (`isSpaRoute(..., flipped)` / `["/app"]`) UNCHANGED.

- [ ] **Step 6: Run the affected tests + build**

Run:
```bash
cd frontend && pnpm exec vitest run src/router.test.tsx src/dev-proxy.test.ts src/shell/ActivityBar.test.tsx && pnpm exec tsc --noEmit
cd .. && uv run pytest tests/test_gen_routes.py -q
uv run python scripts/gen_routes.py --check
cd frontend && pnpm build
```
Expected: all PASS; gen-routes in sync; build clean. (`router.test.tsx` set-equality holds: `{"/", "/cockpit", "/calls", "/me"} == SPA_ROUTES`.)

- [ ] **Step 7: Commit**

```bash
git add frontend/routes.json frontend/src/routes.gen.ts src/call_manager/web/spa_routes.py \
        frontend/src/router.tsx frontend/src/shell/nav-items.ts frontend/src/shell/ActivityBar.test.tsx \
        tests/test_gen_routes.py frontend/src/dev-proxy.test.ts
git commit -m "feat(spa): own /me in the route manifest + wire /me route + flip Settings nav to SPA"
```

---

### Task 8: Jinja teardown — delete the `/me` route/template/module + repoint

Delete the Jinja settings surface, remove `/me` from `backend_prefixes`, regenerate both artifact sets, and re-repoint the `test_web_auth.py` tests that calls-detail parked on `/me`.

**Files:**
- Modify: `src/call_manager/main.py` (drop the Jinja `me` router import + `include_router`)
- Delete: `src/call_manager/web/routes/me.py`, `src/call_manager/web/templates/me.html`
- Modify: `frontend/routes.json` (remove `/me` from `backend_prefixes`)
- Modify (generated): `frontend/src/routes.gen.ts`, `src/call_manager/web/spa_routes.py`, `frontend/src/api/schema.gen.ts`
- Modify: `tests/test_gen_routes.py`, `tests/test_web_auth.py`

- [ ] **Step 1: Re-repoint the three `/me` auth tests FIRST (they break the moment `/me` Jinja dies)**

In `tests/test_web_auth.py`, repoint the three tests calls-detail parked on `/me` onto still-Jinja `/profiles` routes:
- `test_html_401_get_threads_next`: `GET /me?x=1` → `GET /profiles?x=1`; expected location → `"/login?next=%2Fprofiles%3Fx%3D1"`.
- `test_json_401_unaffected_by_next`: `GET /me` → `GET /profiles` (no `Accept: text/html`) → still raw 401 JSON.
- `test_html_401_post_does_not_thread_next`: `POST /me/name` → `POST /profiles/5/edit` (its Form fields are all `Form("")` defaults, so an unauthenticated POST 401s on the auth dependency, not a 422); expected location stays `"/login"`.

Run: `uv run pytest tests/test_web_auth.py -q`
Expected: PASS — confirm the exact `next=` encoding for the GET test; adjust the expected string to match `safe_next` + `urlencode` if it normalizes the query.

- [ ] **Step 2: Add a route-absence guard + flip the backend-route assertion**

Append to `tests/test_gen_routes.py` `test_is_backend_route_for_proxy_carveout`:

```python
    assert is_backend_route("/me/name") is False  # /me retired from backend_prefixes (account/settings)
```

Create `tests/test_me_jinja_surface_absent.py`:

```python
def test_no_jinja_me_routes_remain(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    from call_manager.main import create_app

    app = create_app()
    paths = {r.path for r in app.routes}
    assert "/me" not in paths  # Jinja settings page gone
    assert "/me/name" not in paths
    assert "/me/password" not in paths
    assert "/me/subscriptions" not in paths
    # the /api/me/* endpoints remain
    assert "/api/me/settings" in paths
    assert "/api/me/subscriptions" in paths
```

Run: `uv run pytest tests/test_gen_routes.py::test_is_backend_route_for_proxy_carveout tests/test_me_jinja_surface_absent.py -q`
Expected: FAIL — the Jinja `/me` routes still mounted + `/me` still in `backend_prefixes`.

- [ ] **Step 3: Remove `/me` from `backend_prefixes` and regenerate routes**

In `frontend/routes.json`, drop `"/me"` from `backend_prefixes`:

```json
  "backend_prefixes": ["/api", "/healthz", "/login", "/logout", "/register",
    "/auth", "/admin", "/profiles", "/workspaces", "/static"],
```

Run: `uv run python scripts/gen_routes.py`

- [ ] **Step 4: Unmount + delete the Jinja `me` module + template**

In `src/call_manager/main.py`: remove `me` from the `from call_manager.web.routes import (...)` import tuple and remove the `app.include_router(me.router)` line. **Only the Jinja router** (`web.routes.me`) — the `/api` me router (`web.api.me`, mounted via the api_router) stays. Verify: `grep -n "routes import\|include_router(me" src/call_manager/main.py`.

```bash
git rm src/call_manager/web/routes/me.py src/call_manager/web/templates/me.html
```

Run: `uv run python -c "import call_manager.main"`
Expected: boots.

- [ ] **Step 5: Regenerate the OpenAPI types (drop the Jinja `/me` HTML path)**

Run: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`
Then:

```bash
grep -c '"/me"' frontend/src/api/schema.gen.ts        # expect 0 (Jinja page gone)
grep -c '"/api/me/settings"' frontend/src/api/schema.gen.ts  # expect >=1
cd frontend && pnpm build
```
Expected: 0 / ≥1 / build clean.

- [ ] **Step 6: Run the slice's guards + the re-repointed auth tests**

Run:
```bash
uv run pytest tests/test_gen_routes.py tests/test_me_jinja_surface_absent.py tests/test_web_auth.py \
              tests/test_api_me_settings.py tests/test_api_me_name.py tests/test_api_me_password.py \
              tests/test_api_me_subscriptions.py -q
uv run ruff check src/
cd frontend && pnpm test && pnpm build
```
Expected: all green; ruff clean; build clean.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(teardown): delete Jinja /me route/template/module; retire /me backend prefix; repoint auth tests"
```

---

## Verification (whole-slice gate, before merge)

- `uv run python -c "import call_manager.main"` boots.
- Backend: `uv run pytest tests/test_api_me_settings.py tests/test_api_me_name.py tests/test_api_me_password.py tests/test_api_me_subscriptions.py tests/test_gen_routes.py tests/test_web_auth.py tests/test_me_jinja_surface_absent.py -q` green; `uv run ruff check src/` clean. (Whole-suite `uv run pytest --collect-only -q` shows no orphaned imports.)
- Frontend: `cd frontend && pnpm test` green; `pnpm build` clean.
- Manifest: `uv run python scripts/gen_routes.py --check` in sync; `SPA_ROUTES` gained `/me`, `BACKEND_PREFIXES` dropped `/me`.
- Schema: `schema.gen.ts` has `/api/me/{settings,name,password,subscriptions}` + `AccountSettingsOut`; the Jinja `/me` HTML path is gone.
- Route-table: no non-`/api` route under `/me`.

## Self-Review

**Spec coverage:** §3.1 read-model → Task 1; name → Task 2; password (re-mint + epoch + 400s) → Task 3; subscriptions (diff) → Task 4. §3.2 hooks → Task 5; `Settings` + CSS → Task 6; nav flip → Task 7. §3.3 manifest prefix + React route → Task 7; backend_prefixes removal → Task 8. §6 gates (incl. cookie-attr assertions Task 3; three-auth-test repoint Task 8; CSRF posture = no Origin needed) all mapped. OAuth native form → Task 6 `GoogleSection`.

**Placeholder scan:** every code step carries complete code; commands have expected output. The one run-to-confirm value is the auth `next=` encoding (Task 8 Step 1) — correct TDD posture for an env-derived string, not a placeholder.

**Type consistency:** hook names `useAccountSettings`/`useUpdateName`/`useChangePassword`/`useSaveSubscriptions` (Task 5) match their Task-6 call sites; `AccountSettings` type drives `SettingsForm`/sections. Backend models `Account*`/`UpdateName*`/`ChangePassword*`/`Subscriptions*` are collision-namespaced; paths `/api/me/{settings,name,password,subscriptions}` identical across schema, hooks, tests. The module is `api/account.ts` (NOT `api/settings.ts`) throughout.
