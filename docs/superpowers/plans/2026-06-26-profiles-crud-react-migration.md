# Profiles CRUD React Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the four Jinja `/profiles*` pages (list / create-with-LLM-bootstrap / detail / edit) to React routes over additive typed `/api/profiles/*` endpoints, then delete the Jinja routes/templates/module.

**Architecture:** Strangler-fig parity port (same as account/settings & calls-detail) — additive `/api/profiles` → React routes under the AppShell → per-slice Jinja teardown. A single consolidated `GET /api/profiles/{id}` flat-typed read-model serves the detail view, the edit form, AND the bootstrap poll (React polls it via TanStack Query `refetchInterval`). Backend reuses `create_profile_with_bootstrap` + `parse_structured`; the route's form helpers + post-save side-effect driver relocate out of the doomed Jinja module first. One atomic slice (the `/profiles` manifest prefix couples all four pages). No DB schema change.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x (no schema change) · React 19 / Vite / TanStack Query / openapi-fetch · pytest (+ testcontainers Postgres) · vitest.

**Source spec:** `docs/superpowers/specs/2026-06-26-profiles-crud-react-migration-design.md` (`6ae9741`, Codex-revised).

## Global Constraints

- **No DB schema change. No new dependency. Strict parity port.** Additive `/api`, then teardown.
- **New backend module is `web/api/profiles.py`**; register it in `web/api/__init__.py` (the `/api`-prefixed router). Response models namespaced `ProfilesListOut` / `ProfileListItemOut` / `ProfileDetailOut` / `ProfileCreateIn` / `ProfileCreateOut` / `ProfileUpdateIn` / `ProfileUpdateOut` (only `ProfileFacet` exists today — no collision; re-verify in `schema.gen.ts` during Task 5's regen).
- **Errors via `raise HTTPException(status_code=…, detail=…)`** — the global handler maps `/api` paths to `{"error":{"code","message"}}` (`code_for(400)=="bad_request"`, `403=="forbidden"`, `404=="not_found"`, `409=="conflict"`, `413=="request_entity_too_large"`, `401=="unauthorized"`). Never hand-build the envelope.
- **Validation ORDER must exactly mirror the Jinja handler** (observable on combined-failure requests). Create: name 400 → grants/mission 413 → level 400 → `parse_structured` 400 → **dup-name 409 → member-bounds 400** → IntegrityError 409. Update: description 413 → budget/TRL 400 → currency 400 → **(in-session) 404 → 403 → 409** → level 400 → `parse_structured` 400.
- **`funding_instruments` flatten asymmetry:** flat `funding_instruments` reads from `structured["funding"]["instruments"]` (NOT `["funding_instruments"]`). All other namespaced fields are 1:1. Read via `.get` (the `org` namespace exists only for org units, `career_stage` only for individuals).
- **`bootstrap_*` fields are nulled when `!can_edit`** (defense-in-depth; non-owners never poll/see them).
- **CSRF:** `OriginCSRFMiddleware` allows an ABSENT Origin (it 403s only a present-but-mismatched / `"null"` Origin). TestClient mutation tests pass WITHOUT an Origin header. Do not add Origin headers.
- **Frontend hook module is `frontend/src/api/profiles.ts`**; styling = CSS Modules + `--app-*` tokens, **never bare element selectors** in a `.module.css` (scope under a hashed class). The frontend `tsc` (via `pnpm build`) is the gate (no TS linter).
- **Manifest is prefix-not-`:param`:** add the prefix `/profiles` to `frontend/routes.json` `spa_routes`; the React routes are `path:"profiles"`, `"profiles/new"`, `"profiles/:id"`, `"profiles/:id/edit"`. `router.test.tsx` is set-equality, so the four routes + the manifest prefix land in **one commit** (Task 12).
- **Two regen scripts:** `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh` → `schema.gen.ts` only. `uv run python scripts/gen_routes.py` → `routes.gen.ts` + `spa_routes.py` only.
- **Ordering:** relocate helpers (T1) → additive `/api` (T2–T5, regen at T5) → React (T6–T11) → wire route+manifest+nav (T12) → Jinja teardown + `backend_prefixes` removal + auth-repoint (T13).
- **`/api` tests live in `tests/` ROOT** (`tests/test_api_*.py`); psycopg3 only (`pg_url`/`pg_session_factory`/`profile_factory` fixtures).
- **Offline-test hazard:** the `PUT` edit path schedules a re-embed + backfill background task when `description.strip()` is non-empty; `TestClient` runs it synchronously after the response and the embed client hits Ollama → hang. Update tests **monkeypatch `call_manager.profiles.post_edit._refresh_and_backfill_in_background`** or use an empty `description`.
- **Subagent git discipline (detached SDD):** `cd <worktree>`, `git -C <worktree>`, verify the `feat/…` branch before any commit; `git rm` for deletions.

---

### Task 1: Relocate form helpers + side-effect driver out of the doomed Jinja module

Move the reusable parsing helpers and the post-save side-effect orchestrator out of `web/routes/profiles.py` into neutral `profiles/` modules, then repoint the still-live Jinja module at them. Nothing kept will import the doomed module after this. No behavior change.

**Files:**
- Create: `src/call_manager/profiles/forms.py`
- Create: `src/call_manager/profiles/post_edit.py`
- Modify: `src/call_manager/web/routes/profiles.py` (delete the moved defs, import from the new homes, rename `_bounded_int` call sites → `_bounded_int_or_400`)
- Test: `tests/test_profiles_forms.py` (new)

**Interfaces:**
- Produces: `profiles/forms.py`: `parse_members_strict(orcids: str, names_affiliations: str) -> list[Member]`, `parse_members(orcids, names_affiliations) -> list[Member]`, `split_lines(raw: str) -> list[str]`, `bounded_int_or_400(raw: str, *, field: str, low: int, high: int) -> int | None`, and constants `MAX_MEMBERS=50`, `MAX_SEED_LINE=500`, `MAX_LIST_ENTRIES=100`, `ALLOWED_CURRENCIES={"", "SEK","EUR","USD","NOK","DKK"}`, `BUDGET_MIN=0`, `BUDGET_MAX=10**12`, `TRL_MIN=1`, `TRL_MAX=9`. (Drop the leading underscores — these are now a public module API. `forms.py` is intentionally FastAPI-coupled: `bounded_int_or_400` raises `HTTPException`, unlike `profiles/fields.py`'s `_bounded_int` which raises `FieldValidationError`.)
- Produces: `profiles/post_edit.py`: `refresh_and_backfill_in_background(*, session_factory, embed_client, profile_id: int, refresh_embedding: bool) -> None` and `refresh_embedding_in_background(*, session_factory, embed_client, profile_id: int) -> None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_profiles_forms.py`:

```python
import pytest
from fastapi import HTTPException

from call_manager.profiles.forms import (
    ALLOWED_CURRENCIES,
    BUDGET_MAX,
    bounded_int_or_400,
    parse_members_strict,
    split_lines,
)


def test_bounded_int_or_400_parses_and_bounds():
    assert bounded_int_or_400("", field="budget_min", low=0, high=BUDGET_MAX) is None
    assert bounded_int_or_400("n/a", field="budget_min", low=0, high=BUDGET_MAX) is None
    assert bounded_int_or_400("5", field="trl_min", low=1, high=9) == 5
    with pytest.raises(HTTPException) as ei:
        bounded_int_or_400("99", field="trl_min", low=1, high=9)
    assert ei.value.status_code == 400


def test_split_lines_strips_and_caps():
    assert split_lines("a\n  b  \n\n c ") == ["a", "b", "c"]


def test_parse_members_strict_rejects_too_many():
    with pytest.raises(HTTPException) as ei:
        parse_members_strict("\n".join(f"0000-0001-0000-{i:04d}" for i in range(60)), "")
    assert ei.value.status_code == 400


def test_allowed_currencies_includes_blank_and_sek():
    assert "" in ALLOWED_CURRENCIES and "SEK" in ALLOWED_CURRENCIES
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_profiles_forms.py -q`
Expected: FAIL — `ModuleNotFoundError: call_manager.profiles.forms`.

- [ ] **Step 3: Create `profiles/forms.py`**

```python
from __future__ import annotations

import contextlib

from fastapi import HTTPException

from call_manager.profiles.seed import Member

MAX_MEMBERS = 50
MAX_SEED_LINE = 500
MAX_LIST_ENTRIES = 100
ALLOWED_CURRENCIES = {"", "SEK", "EUR", "USD", "NOK", "DKK"}
BUDGET_MIN = 0
BUDGET_MAX = 10**12
TRL_MIN = 1
TRL_MAX = 9


def split_lines(raw: str) -> list[str]:
    return [ln.strip() for ln in raw.splitlines() if ln.strip()][:MAX_LIST_ENTRIES]


def bounded_int_or_400(raw: str, *, field: str, low: int, high: int) -> int | None:
    """Parse an optional integer; reject out-of-[low,high] with HTTP 400.

    Empty input -> None. Non-numeric input -> None (a user who types "n/a" in
    an optional numeric box shouldn't see a 400). This module is intentionally
    FastAPI-coupled (raises HTTPException), unlike profiles/fields.py's
    framework-agnostic _bounded_int (which raises FieldValidationError)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        v = int(raw)
    except ValueError:
        return None
    if v < low or v > high:
        raise HTTPException(status_code=400, detail=f"{field} out of range ({low}..{high}): {v}")
    return v


def parse_members_strict(orcids: str, names_affiliations: str) -> list[Member]:
    raw_lines = [
        ln.strip() for ln in (orcids.splitlines() + names_affiliations.splitlines()) if ln.strip()
    ]
    if len(raw_lines) > MAX_MEMBERS:
        raise HTTPException(status_code=400, detail=f"too many members (max {MAX_MEMBERS})")
    if any(len(ln) > MAX_SEED_LINE for ln in raw_lines):
        raise HTTPException(status_code=400, detail=f"member line too long (max {MAX_SEED_LINE} chars)")
    return parse_members(orcids, names_affiliations)


def parse_members(orcids: str, names_affiliations: str) -> list[Member]:
    members: list[Member] = []
    for raw in orcids.splitlines():
        orcid = raw.strip()
        if orcid:
            with contextlib.suppress(ValueError):
                members.append(Member(orcid=orcid))
    for raw in names_affiliations.splitlines():
        line = raw.strip()
        if not line or "|" not in line:
            continue
        name_part, aff_part = line.split("|", 1)
        name_part = name_part.strip()
        aff_part = aff_part.strip()
        if name_part and aff_part:
            with contextlib.suppress(ValueError):
                members.append(Member(name=name_part, affiliation=aff_part))
    return members
```

- [ ] **Step 4: Create `profiles/post_edit.py`**

```python
from __future__ import annotations

import logging

from call_manager.embeddings.refresh import refresh_profile_embedding
from call_manager.ranking.backfill import backfill_profile

_logger = logging.getLogger(__name__)


def refresh_embedding_in_background(*, session_factory, embed_client, profile_id: int) -> None:
    """Run refresh_profile_embedding outside the request lifecycle so a slow
    Ollama doesn't stall the user. Swallow all errors — the daily refresh-missing
    tick will retry, and the embedding gate treats a missing vector as UNKNOWN."""
    try:
        with session_factory() as s:
            refresh_profile_embedding(s, embed_client, profile_id)
            s.commit()
    except Exception as e:  # noqa: BLE001
        _logger.warning(
            "profile.embedding_refresh_failed",
            extra={"event": "profile_embedding_refresh_failed", "profile_id": profile_id, "error": str(e)},
        )


def refresh_and_backfill_in_background(
    *, session_factory, embed_client, profile_id: int, refresh_embedding: bool
) -> None:
    """Refresh first (if the embed text changed), then enqueue backfill. Order
    matters: the ranking jobs must see the post-edit vector, not the pre-edit one."""
    if refresh_embedding:
        refresh_embedding_in_background(
            session_factory=session_factory, embed_client=embed_client, profile_id=profile_id
        )
    try:
        backfill_profile(session_factory=session_factory, profile_id=profile_id)
    except Exception as e:  # noqa: BLE001
        _logger.warning(
            "profile.backfill_failed",
            extra={"event": "profile_backfill_failed", "profile_id": profile_id, "error": str(e)},
        )
```

- [ ] **Step 5: Repoint the still-live Jinja module at the new homes**

In `src/call_manager/web/routes/profiles.py`:
1. DELETE the moved defs: `_refresh_embedding_in_background`, `_refresh_and_backfill_in_background`, `_split_lines`, `_bounded_int`, `_parse_members_strict`, `_parse_members`, and the constants `_MAX_MEMBERS`, `_MAX_SEED_LINE`, `_MAX_LIST_ENTRIES`, `_ALLOWED_CURRENCIES`, `_BUDGET_MIN`, `_BUDGET_MAX`, `_TRL_MIN`, `_TRL_MAX`.
2. Add imports near the top:

```python
from call_manager.profiles.forms import (
    ALLOWED_CURRENCIES as _ALLOWED_CURRENCIES,
    BUDGET_MAX as _BUDGET_MAX,
    BUDGET_MIN as _BUDGET_MIN,
    TRL_MAX as _TRL_MAX,
    TRL_MIN as _TRL_MIN,
    bounded_int_or_400 as _bounded_int,
    parse_members_strict as _parse_members_strict,
    split_lines as _split_lines,
)
from call_manager.profiles.post_edit import refresh_and_backfill_in_background as _refresh_and_backfill_in_background
```

(Aliasing on import means the existing call sites in `profile_new_submit` / `profile_edit_submit` keep working unchanged — `_bounded_int(...)`, `_split_lines(...)`, `_parse_members_strict(...)`, `_ALLOWED_CURRENCIES`, `_refresh_and_backfill_in_background(...)`. This module is deleted in Task 13; the aliases are transitional.)

- [ ] **Step 6: Run the new + existing profiles tests**

Run:
```bash
uv run pytest tests/test_profiles_forms.py -q
uv run pytest tests/ -k "profile" -q
uv run ruff check src/call_manager/profiles/forms.py src/call_manager/profiles/post_edit.py src/call_manager/web/routes/profiles.py
```
Expected: forms tests PASS (4); the existing profile route/test suite still PASS (no behavior change); ruff clean.

- [ ] **Step 7: Severance check + commit**

Run: `grep -rn "from call_manager.web.routes.profiles\|web.routes import.*profiles\|web\.routes\.profiles" src/ tests/ | grep -v "src/call_manager/web/routes/profiles.py"`
Expected: ONLY `main.py`'s `from call_manager.web.routes import (... profiles ...)` + `app.include_router(profiles.router)` (the registration, removed in Task 13). No other module imports the doomed module's internals.

```bash
git add src/call_manager/profiles/forms.py src/call_manager/profiles/post_edit.py \
        src/call_manager/web/routes/profiles.py tests/test_profiles_forms.py
git commit -m "refactor(profiles): relocate form helpers -> forms.py + side-effect driver -> post_edit.py"
```

---

### Task 2: `GET /api/profiles` (list read-model)

**Files:**
- Create: `src/call_manager/web/api/profiles.py`
- Modify: `src/call_manager/web/api/__init__.py` (register the router)
- Test: `tests/test_api_profiles_list.py` (new)

**Interfaces:**
- Produces: route `GET /api/profiles` → `ProfilesListOut {profiles: list[ProfileListItemOut]}`; `ProfileListItemOut {id, name, kind, level: str|None, areas_count: int, budget_currency: str|None, description: str}`. Active profiles ordered by name. Any authenticated user.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_profiles_list.py`:

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
        u = User(status="active", email="viewer@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_list_active_by_name(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    profile_factory(name="Beta", structured={"areas": ["a", "b"]})
    profile_factory(name="Alpha", structured={"areas": ["x"]})
    profile_factory(name="Archived")  # active by default; archive it below
    # archive the third
    from call_manager.db.models import Profile
    with pg_session_factory() as s:
        p = s.execute(Profile.__table__.select().where(Profile.name == "Archived")).first()
        s.execute(Profile.__table__.update().where(Profile.name == "Archived").values(is_active=False))
        s.commit()
    client = _login(app, pg_session_factory)
    with client:
        r = client.get("/api/profiles")
    assert r.status_code == 200
    body = r.json()
    names = [p["name"] for p in body["profiles"]]
    assert names == ["Alpha", "Beta"]  # active only, by name
    alpha = next(p for p in body["profiles"] if p["name"] == "Alpha")
    assert alpha["areas_count"] == 1
    assert alpha["kind"] == "organizational_unit"


def test_list_requires_auth(monkeypatch, pg_url):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/api/profiles")
    assert r.status_code == 401
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_api_profiles_list.py -q`
Expected: FAIL — 404 (no route).

- [ ] **Step 3: Create `web/api/profiles.py` with the list endpoint**

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import Profile, User

router = APIRouter()


class ProfileListItemOut(BaseModel):
    id: int
    name: str
    kind: str
    level: str | None
    areas_count: int
    budget_currency: str | None
    description: str


class ProfilesListOut(BaseModel):
    profiles: list[ProfileListItemOut]


@router.get("/profiles", response_model=ProfilesListOut)
def list_profiles(request: Request, user: User = Depends(get_current_user)):  # noqa: B008
    sf = request.app.state.session_factory
    with sf() as session:
        rows = list(
            session.execute(
                select(Profile).where(Profile.is_active.is_(True)).order_by(Profile.name)
            ).scalars()
        )
        out = [
            ProfileListItemOut(
                id=p.id,
                name=p.name,
                kind=p.kind,
                level=p.level,
                areas_count=len((p.structured or {}).get("areas") or []),
                budget_currency=p.budget_currency,
                description=p.description or "",
            )
            for p in rows
        ]
    return ProfilesListOut(profiles=out)
```

- [ ] **Step 4: Register the router**

In `src/call_manager/web/api/__init__.py`, add `profiles` to the `from call_manager.web.api import (...)` tuple and add `router.include_router(profiles.router)` alongside the others.

- [ ] **Step 5: Run the test + ruff**

Run: `uv run pytest tests/test_api_profiles_list.py -q && uv run ruff check src/call_manager/web/api/profiles.py`
Expected: PASS (2); ruff clean.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/api/profiles.py src/call_manager/web/api/__init__.py tests/test_api_profiles_list.py
git commit -m "feat(api): GET /api/profiles list read-model"
```

---

### Task 3: `GET /api/profiles/{id}` (detail/edit/poll read-model + flatten)

**Files:**
- Modify: `src/call_manager/web/api/profiles.py`
- Test: `tests/test_api_profile_detail.py` (new)

**Interfaces:**
- Produces: route `GET /api/profiles/{profile_id}` → `ProfileDetailOut` (404 if missing/inactive). Helper `_flatten_structured(p: Profile) -> dict` mirroring `parse_structured`'s namespaces. `can_edit = created_by == user.id or user.is_admin`. `bootstrap_status`/`bootstrap_pubmed_status`/`publications_fetched` from `ProfileBootstrapJob`, **nulled when `!can_edit`**.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_profile_detail.py`:

```python
from fastapi.testclient import TestClient

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Profile, ProfileBootstrapJob, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _client_for(app, pg_session_factory, uid):
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def _new_user(pg_session_factory, *, is_admin=False, email="u@ki.se"):
    with pg_session_factory() as s:
        u = User(status="active", email=email, is_admin=is_admin)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def test_detail_flattens_namespaced_structured(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    structured = {
        "areas": ["oncology"],
        "budget_min": 100, "budget_max": 200,
        "trl_min": 3, "trl_max": 7,
        "must_include": ["pediatric"], "hard_exclusions": ["defence"],
        "org": {"org_type": "university_hospital", "capabilities": ["biobank"],
                "patient_populations": ["children"], "scale": "~400 researchers"},
        "funding": {"instruments": ["project_grant"], "funder_exclusions": ["x"]},
        "geo": {"country": "SE", "host_institution": "KI"},
        "eligibility": {"applicant_types": ["solo_pi"], "role": "PI", "years_since_phd": 5},
    }
    pid, owner_id = profile_factory(name="Lab", structured=structured)
    client = _client_for(app, pg_session_factory, owner_id)
    with client:
        r = client.get(f"/api/profiles/{pid}")
    assert r.status_code == 200
    b = r.json()
    assert b["areas"] == ["oncology"]
    assert b["org_type"] == "university_hospital"
    assert b["capabilities"] == ["biobank"]
    # The one asymmetry: form field funding_instruments <- structured["funding"]["instruments"].
    assert b["funding_instruments"] == ["project_grant"]
    assert b["country"] == "SE"
    assert b["can_edit"] is True


def test_detail_can_edit_false_nulls_bootstrap(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Lab")
    with pg_session_factory() as s:
        s.add(ProfileBootstrapJob(profile_id=pid, status="running", seed={"x": 1}))
        s.commit()
    other = _new_user(pg_session_factory, email="other@ki.se")
    client = _client_for(app, pg_session_factory, other)
    with client:
        r = client.get(f"/api/profiles/{pid}")
    assert r.status_code == 200
    b = r.json()
    assert b["can_edit"] is False
    assert b["bootstrap_status"] is None  # nulled for non-owner


def test_detail_owner_sees_bootstrap_status(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Lab")
    with pg_session_factory() as s:
        s.add(ProfileBootstrapJob(profile_id=pid, status="running", seed={"x": 1}))
        s.commit()
    client = _client_for(app, pg_session_factory, owner_id)
    with client:
        r = client.get(f"/api/profiles/{pid}")
    assert r.json()["bootstrap_status"] == "running"


def test_detail_admin_can_edit(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Lab")
    admin = _new_user(pg_session_factory, is_admin=True, email="admin@ki.se")
    client = _client_for(app, pg_session_factory, admin)
    with client:
        r = client.get(f"/api/profiles/{pid}")
    assert r.json()["can_edit"] is True


def test_detail_404_inactive(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Lab")
    with pg_session_factory() as s:
        s.execute(Profile.__table__.update().where(Profile.id == pid).values(is_active=False))
        s.commit()
    client = _client_for(app, pg_session_factory, owner_id)
    with client:
        r = client.get(f"/api/profiles/{pid}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_api_profile_detail.py -q`
Expected: FAIL — 404 route-not-found / KeyError.

- [ ] **Step 3: Add the flatten helper + the read-model endpoint to `web/api/profiles.py`**

Add to the imports: `from call_manager.db.models import Profile, ProfileBootstrapJob, User` and `from fastapi import HTTPException`. Add:

```python
def _flatten_structured(p: Profile) -> dict:
    """Mirror of parse_structured: unwrap structured.{org,eligibility,funding,geo}
    into flat form fields. Read via .get — org exists only for org units,
    career_stage only for individuals. NOTE the one asymmetry: flat
    `funding_instruments` is stored as structured["funding"]["instruments"]."""
    s = p.structured or {}
    elig = s.get("eligibility") or {}
    org = s.get("org") or {}
    funding = s.get("funding") or {}
    geo = s.get("geo") or {}
    return {
        "areas": list(s.get("areas") or []),
        "budget_min": s.get("budget_min"),
        "budget_max": s.get("budget_max"),
        "trl_min": s.get("trl_min"),
        "trl_max": s.get("trl_max"),
        "must_include": list(s.get("must_include") or []),
        "hard_exclusions": list(s.get("hard_exclusions") or []),
        "org_type": org.get("org_type"),
        "capabilities": list(org.get("capabilities") or []),
        "patient_populations": list(org.get("patient_populations") or []),
        "scale": org.get("scale"),
        "career_stage": elig.get("career_stage"),
        "role": elig.get("role"),
        "years_since_phd": elig.get("years_since_phd"),
        "applicant_types": list(elig.get("applicant_types") or []),
        "funding_instruments": list(funding.get("instruments") or []),
        "funder_exclusions": list(funding.get("funder_exclusions") or []),
        "country": geo.get("country"),
        "host_institution": geo.get("host_institution"),
    }


class ProfileDetailOut(BaseModel):
    id: int
    name: str
    kind: str
    level: str | None
    description: str
    budget_currency: str | None
    # rubric
    areas: list[str]
    budget_min: int | None
    budget_max: int | None
    trl_min: int | None
    trl_max: int | None
    must_include: list[str]
    hard_exclusions: list[str]
    # namespaced -> flat
    org_type: str | None
    capabilities: list[str]
    patient_populations: list[str]
    scale: str | None
    career_stage: str | None
    role: str | None
    years_since_phd: int | None
    applicant_types: list[str]
    funding_instruments: list[str]
    funder_exclusions: list[str]
    country: str | None
    host_institution: str | None
    # state
    can_edit: bool
    bootstrap_status: str | None
    bootstrap_pubmed_status: str | None
    publications_fetched: int | None


@router.get("/profiles/{profile_id}", response_model=ProfileDetailOut)
def get_profile(
    request: Request, profile_id: int, user: User = Depends(get_current_user)  # noqa: B008
):
    sf = request.app.state.session_factory
    with sf() as session:
        p = session.get(Profile, profile_id)
        if p is None or p.is_active is False:
            raise HTTPException(status_code=404, detail="profile not found")
        can_edit = p.created_by == user.id or user.is_admin
        job = session.execute(
            select(ProfileBootstrapJob).where(ProfileBootstrapJob.profile_id == profile_id)
        ).scalar_one_or_none()
        flat = _flatten_structured(p)
        core = {
            "id": p.id, "name": p.name, "kind": p.kind, "level": p.level,
            "description": p.description or "", "budget_currency": p.budget_currency,
        }
        # Defense-in-depth: only the owner/admin (who polls on the edit page) sees job state.
        if can_edit and job is not None:
            boot = {
                "bootstrap_status": job.status,
                "bootstrap_pubmed_status": job.pubmed_status,
                "publications_fetched": job.publications_fetched,
            }
        else:
            boot = {"bootstrap_status": None, "bootstrap_pubmed_status": None, "publications_fetched": None}
    return ProfileDetailOut(**core, **flat, can_edit=can_edit, **boot)
```

- [ ] **Step 4: Run the test + ruff**

Run: `uv run pytest tests/test_api_profile_detail.py -q && uv run ruff check src/call_manager/web/api/profiles.py`
Expected: PASS (5); ruff clean.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/api/profiles.py tests/test_api_profile_detail.py
git commit -m "feat(api): GET /api/profiles/{id} flat read-model (detail/edit/poll, can_edit-gated bootstrap)"
```

---

### Task 4: `POST /api/profiles` (create + bootstrap)

**Files:**
- Modify: `src/call_manager/web/api/profiles.py`
- Test: `tests/test_api_profiles_create.py` (new)

**Interfaces:**
- Produces: route `POST /api/profiles` body `ProfileCreateIn` → `ProfileCreateOut {id: int}`. Validation order per Global Constraints. Reuses `create_profile_with_bootstrap` + `parse_members_strict` + `parse_structured`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_profiles_create.py`:

```python
from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Profile, ProfileBootstrapJob, Subscription, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _login(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="creator@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client, uid


_BODY = {
    "name": "Digital Pathology Lab", "kind": "organizational_unit", "level": "group",
    "orcids": "", "names_affiliations": "", "pasted_grants": "", "mission_text": "imaging",
    "areas": "oncology", "career_stage": "", "applicant_types": "", "role": "",
    "years_since_phd": "", "org_type": "university_hospital", "capabilities": "biobank",
    "patient_populations": "", "scale": "", "funding_instruments": "", "funder_exclusions": "",
    "country": "SE", "host_institution": "KI",
}


def test_create_returns_id_and_enqueues_job(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    with client:
        r = client.post("/api/profiles", json=_BODY)
    assert r.status_code == 200
    new_id = r.json()["id"]
    with pg_session_factory() as s:
        assert s.get(Profile, new_id) is not None
        job = s.execute(
            select(ProfileBootstrapJob).where(ProfileBootstrapJob.profile_id == new_id)
        ).scalar_one()
        assert job.status == "queued"
        sub = s.execute(
            select(Subscription).where(Subscription.user_id == uid, Subscription.profile_id == new_id)
        ).scalar_one_or_none()
        assert sub is not None  # creator auto-subscribed
        assert s.get(User, uid).default_profile_id == new_id  # default set


def test_create_duplicate_name_409(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r1 = client.post("/api/profiles", json=_BODY)
        assert r1.status_code == 200
        r2 = client.post("/api/profiles", json={**_BODY})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "conflict"


def test_create_name_too_long_400(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r = client.post("/api/profiles", json={**_BODY, "name": "x" * 201})
    assert r.status_code == 400


def test_create_grants_too_large_413(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r = client.post("/api/profiles", json={**_BODY, "pasted_grants": "y" * 100_001})
    assert r.status_code == 413


def test_create_requires_auth(monkeypatch, pg_url):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post("/api/profiles", json=_BODY)
    assert r.status_code == 401
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_api_profiles_create.py -q`
Expected: FAIL — 404/405.

- [ ] **Step 3: Add the create endpoint**

Add imports to `web/api/profiles.py`:

```python
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import ProfileKind, ProfileLevel, Subscription  # extend existing models import
from call_manager.profiles.create import create_profile_with_bootstrap
from call_manager.profiles.fields import FieldValidationError, parse_structured
from call_manager.profiles.forms import parse_members_strict
```

Add the models + endpoint (the field set mirrors `profile_new_submit`; the validation ORDER mirrors it exactly):

```python
class ProfileCreateIn(BaseModel):
    name: str
    kind: str = "organizational_unit"
    level: str = ""
    orcids: str = ""
    names_affiliations: str = ""
    pasted_grants: str = ""
    mission_text: str = ""
    areas: str = ""
    career_stage: str = ""
    applicant_types: str = ""
    role: str = ""
    years_since_phd: str = ""
    org_type: str = ""
    capabilities: str = ""
    patient_populations: str = ""
    scale: str = ""
    funding_instruments: str = ""
    funder_exclusions: str = ""
    country: str = ""
    host_institution: str = ""


class ProfileCreateOut(BaseModel):
    id: int


@router.post("/profiles", response_model=ProfileCreateOut)
def create_profile(
    request: Request, body: ProfileCreateIn, user: User = Depends(get_current_user)  # noqa: B008
):
    if len(body.name) > 200:
        raise HTTPException(status_code=400, detail="name too long (max 200 chars)")
    if len(body.pasted_grants) > 100_000:
        raise HTTPException(status_code=413, detail="pasted grants too large (max 100 KB)")
    if len(body.mission_text) > 10_240:
        raise HTTPException(status_code=413, detail="mission text too large (max 10 KB)")
    kind = body.kind if body.kind in {k.value for k in ProfileKind} else ProfileKind.ORGANIZATIONAL_UNIT.value
    level_val: str | None = None
    if kind == ProfileKind.ORGANIZATIONAL_UNIT.value:
        level_val = body.level.strip() or None
        if level_val is not None and level_val not in {x.value for x in ProfileLevel}:
            raise HTTPException(status_code=400, detail=f"unknown level: {body.level!r}")
    try:
        user_structured = parse_structured(
            kind=kind,
            raw={
                "areas": body.areas, "career_stage": body.career_stage,
                "applicant_types": body.applicant_types, "role": body.role,
                "years_since_phd": body.years_since_phd, "org_type": body.org_type,
                "capabilities": body.capabilities, "patient_populations": body.patient_populations,
                "scale": body.scale, "funding_instruments": body.funding_instruments,
                "funder_exclusions": body.funder_exclusions, "country": body.country,
                "host_institution": body.host_institution,
            },
        )
    except FieldValidationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e

    sf = request.app.state.session_factory
    with sf() as session:
        existing = session.execute(
            select(Profile.id).where(Profile.name == body.name, Profile.is_active.is_(True))
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail="profile name already exists")
    members = parse_members_strict(body.orcids, body.names_affiliations)
    seed_payload = {
        "name": body.name, "kind": kind, "level": level_val,
        "members": [{"orcid": m.orcid, "name": m.name, "affiliation": m.affiliation} for m in members],
        "pasted_grants": body.pasted_grants, "mission_text": body.mission_text,
    }
    try:
        with sf() as session:
            profile = create_profile_with_bootstrap(
                session=session, owner_id=user.id, name=body.name, kind=kind,
                level=level_val, structured={**user_structured}, seed_payload=seed_payload,
            )
            session.commit()
            session.refresh(profile)
            new_id = profile.id
    except IntegrityError as e:
        raise HTTPException(status_code=409, detail="profile name already exists") from e
    return ProfileCreateOut(id=new_id)
```

- [ ] **Step 4: Run the test + ruff**

Run: `uv run pytest tests/test_api_profiles_create.py -q && uv run ruff check src/call_manager/web/api/profiles.py`
Expected: PASS (5); ruff clean.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/api/profiles.py tests/test_api_profiles_create.py
git commit -m "feat(api): POST /api/profiles (create + bootstrap, parity validation order)"
```

---

### Task 5: `PUT /api/profiles/{id}` (edit) + OpenAPI regen

**Files:**
- Modify: `src/call_manager/web/api/profiles.py`
- Modify (generated): `frontend/src/api/schema.gen.ts`
- Test: `tests/test_api_profile_update.py` (new)

**Interfaces:**
- Produces: route `PUT /api/profiles/{profile_id}` body `ProfileUpdateIn` → `ProfileUpdateOut {id: int}`. Owner-or-admin (403), 409 drafting, validation order per Global Constraints; re-namespaces via `parse_structured`, schedules `post_edit.refresh_and_backfill_in_background`. Schema paths for all four `/api/profiles*` endpoints land in `schema.gen.ts`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_profile_update.py`:

```python
import call_manager.profiles.post_edit as post_edit
from fastapi.testclient import TestClient

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Profile, ProfileBootstrapJob, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    # Keep the re-embed/backfill driver offline (it would hit Ollama).
    monkeypatch.setattr(post_edit, "refresh_and_backfill_in_background", lambda **kw: None)
    return create_app()


def _client_for(app, uid):
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def _other_user(pg_session_factory, *, is_admin=False, email="o@ki.se"):
    with pg_session_factory() as s:
        u = User(status="active", email=email, is_admin=is_admin)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


_EDIT = {
    "description": "Updated description.", "areas": "oncology\nimaging",
    "budget_min": "100", "budget_max": "200", "budget_currency": "SEK",
    "trl_min": "3", "trl_max": "7", "must_include": "pediatric", "hard_exclusions": "",
    "level": "group", "career_stage": "", "applicant_types": "", "role": "",
    "years_since_phd": "", "org_type": "university_hospital", "capabilities": "biobank",
    "patient_populations": "", "scale": "", "funding_instruments": "project_grant",
    "funder_exclusions": "", "country": "SE", "host_institution": "KI",
}


def test_update_owner_renamespaces(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Lab", structured={})
    client = _client_for(app, owner_id)
    with client:
        r = client.put(f"/api/profiles/{pid}", json=_EDIT)
    assert r.status_code == 200
    assert r.json() == {"id": pid}
    with pg_session_factory() as s:
        p = s.get(Profile, pid)
        assert p.structured["areas"] == ["oncology", "imaging"]
        assert p.structured["org"]["capabilities"] == ["biobank"]
        # form funding_instruments -> structured["funding"]["instruments"]
        assert p.structured["funding"]["instruments"] == ["project_grant"]
        assert p.budget_currency == "SEK"


def test_update_403_non_owner(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Lab")
    other = _other_user(pg_session_factory)
    client = _client_for(app, other)
    with client:
        r = client.put(f"/api/profiles/{pid}", json=_EDIT)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_update_409_while_drafting(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="Lab")
    with pg_session_factory() as s:
        s.add(ProfileBootstrapJob(profile_id=pid, status="running", seed={"x": 1}))
        s.commit()
    client = _client_for(app, owner_id)
    with client:
        r = client.put(f"/api/profiles/{pid}", json=_EDIT)
    assert r.status_code == 409


def test_update_bad_currency_400_before_404(monkeypatch, pg_url, pg_session_factory):
    # input-shape validation (currency 400) fires BEFORE the in-session 404 — parity.
    app = _setup(monkeypatch, pg_url)
    owner = _other_user(pg_session_factory, email="owner@ki.se")
    client = _client_for(app, owner)
    with client:
        r = client.put("/api/profiles/999999", json={**_EDIT, "budget_currency": "ZZZ"})
    assert r.status_code == 400


def test_update_404_missing(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    owner = _other_user(pg_session_factory, email="owner@ki.se")
    client = _client_for(app, owner)
    with client:
        r = client.put("/api/profiles/999999", json=_EDIT)
    assert r.status_code == 404
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_api_profile_update.py -q`
Expected: FAIL — 404/405.

- [ ] **Step 3: Add the update endpoint**

Add imports to `web/api/profiles.py`:

```python
from fastapi import BackgroundTasks

from call_manager.embeddings.similarity import build_profile_text
from call_manager.profiles import post_edit
from call_manager.profiles.forms import (
    ALLOWED_CURRENCIES, BUDGET_MAX, BUDGET_MIN, TRL_MAX, TRL_MIN,
    bounded_int_or_400, split_lines,
)
```

Add the models + endpoint (mirrors `profile_edit_submit`; validation ORDER mirrors it — shape checks BEFORE the session). Note: call `post_edit.refresh_and_backfill_in_background` via the module (so the test's monkeypatch on the module attribute takes effect):

```python
class ProfileUpdateIn(BaseModel):
    description: str = ""
    areas: str = ""
    budget_min: str = ""
    budget_max: str = ""
    budget_currency: str = ""
    trl_min: str = ""
    trl_max: str = ""
    must_include: str = ""
    hard_exclusions: str = ""
    level: str = ""
    career_stage: str = ""
    applicant_types: str = ""
    role: str = ""
    years_since_phd: str = ""
    org_type: str = ""
    capabilities: str = ""
    patient_populations: str = ""
    scale: str = ""
    funding_instruments: str = ""
    funder_exclusions: str = ""
    country: str = ""
    host_institution: str = ""


class ProfileUpdateOut(BaseModel):
    id: int


@router.put("/profiles/{profile_id}", response_model=ProfileUpdateOut)
def update_profile(
    request: Request,
    profile_id: int,
    body: ProfileUpdateIn,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),  # noqa: B008
):
    if len(body.description) > 50_000:
        raise HTTPException(status_code=413, detail="description too long (max 50 KB)")
    parsed_budget_min = bounded_int_or_400(body.budget_min, field="budget_min", low=BUDGET_MIN, high=BUDGET_MAX)
    parsed_budget_max = bounded_int_or_400(body.budget_max, field="budget_max", low=BUDGET_MIN, high=BUDGET_MAX)
    parsed_trl_min = bounded_int_or_400(body.trl_min, field="trl_min", low=TRL_MIN, high=TRL_MAX)
    parsed_trl_max = bounded_int_or_400(body.trl_max, field="trl_max", low=TRL_MIN, high=TRL_MAX)
    normalized_currency = (body.budget_currency or "").strip().upper()
    if normalized_currency not in ALLOWED_CURRENCIES:
        raise HTTPException(status_code=400, detail=f"unknown currency: {body.budget_currency!r}")

    sf = request.app.state.session_factory
    embed_text_changed = False
    with sf() as session:
        p = session.get(Profile, profile_id)
        if p is None or p.is_active is False:
            raise HTTPException(status_code=404, detail="profile not found")
        if p.created_by != user.id and not user.is_admin:
            raise HTTPException(status_code=403, detail="not authorized")
        active_job = session.execute(
            select(ProfileBootstrapJob.id).where(
                ProfileBootstrapJob.profile_id == profile_id,
                ProfileBootstrapJob.status.in_(("queued", "running")),
            )
        ).scalar_one_or_none()
        if active_job is not None:
            raise HTTPException(status_code=409, detail="profile is still drafting — try again shortly")
        old_embed_text = build_profile_text(p)
        p.description = body.description
        if p.kind == ProfileKind.ORGANIZATIONAL_UNIT.value:
            lv = (body.level or "").strip() or None
            if lv is not None and lv not in {x.value for x in ProfileLevel}:
                raise HTTPException(status_code=400, detail=f"unknown level: {body.level!r}")
            p.level = lv
        base = {
            "areas": split_lines(body.areas),
            "budget_min": parsed_budget_min, "budget_max": parsed_budget_max,
            "trl_min": parsed_trl_min, "trl_max": parsed_trl_max,
            "must_include": split_lines(body.must_include),
            "hard_exclusions": split_lines(body.hard_exclusions),
        }
        try:
            new_ns = parse_structured(
                kind=p.kind,
                raw={
                    "areas": body.areas, "career_stage": body.career_stage,
                    "applicant_types": body.applicant_types, "role": body.role,
                    "years_since_phd": body.years_since_phd, "org_type": body.org_type,
                    "capabilities": body.capabilities, "patient_populations": body.patient_populations,
                    "scale": body.scale, "funding_instruments": body.funding_instruments,
                    "funder_exclusions": body.funder_exclusions, "country": body.country,
                    "host_institution": body.host_institution,
                },
            )
        except FieldValidationError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message) from e
        new_ns.pop("areas", None)  # base owns areas
        p.structured = {**base, **new_ns}
        p.budget_currency = normalized_currency or None
        new_embed_text = build_profile_text(p)
        embed_text_changed = old_embed_text.strip() != new_embed_text.strip()
        session.commit()

    if body.description.strip():
        background.add_task(
            post_edit.refresh_and_backfill_in_background,
            session_factory=sf,
            embed_client=request.app.state.embed_client,
            profile_id=profile_id,
            refresh_embedding=embed_text_changed,
        )
    return ProfileUpdateOut(id=profile_id)
```

- [ ] **Step 4: Run the test + ruff**

Run: `uv run pytest tests/test_api_profile_update.py -q && uv run ruff check src/call_manager/web/api/profiles.py`
Expected: PASS (5); ruff clean.

- [ ] **Step 5: Regenerate the OpenAPI TS types**

Run: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`
Then verify the four paths + the namespaced models landed and the frontend compiles:

```bash
for p in '"/api/profiles"' '"/api/profiles/{profile_id}"'; do echo -n "$p: "; grep -c "$p" frontend/src/api/schema.gen.ts; done
grep -c 'ProfileDetailOut\|ProfileCreateIn\|ProfileUpdateIn\|ProfilesListOut' frontend/src/api/schema.gen.ts
cd frontend && pnpm build
```
Expected: each path ≥1; the models present; build clean. (The four Jinja `/profiles*` HTML paths are still present — they drop in Task 13.)

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/web/api/profiles.py tests/test_api_profile_update.py frontend/src/api/schema.gen.ts
git commit -m "feat(api): PUT /api/profiles/{id} (edit, re-namespace, 409 drafting) + regen OpenAPI types"
```

---

### Task 6: Frontend hooks — `api/profiles.ts`

**Files:**
- Create: `frontend/src/api/profiles.ts`
- Test: `frontend/src/api/profiles.test.tsx`

**Interfaces:**
- Consumes: `schema.gen.ts` paths `/api/profiles`, `/api/profiles/{profile_id}`; `api`, `unwrap` from `./client`.
- Produces: `useProfilesList()` (`["profiles"]`), `useProfile(id: number)` (`["profile", id]`, polls while drafting), `useCreateProfile()`, `useUpdateProfile(id: number)`; types `ProfileDetail = components["schemas"]["ProfileDetailOut"]`, `ProfileCreateBody = components["schemas"]["ProfileCreateIn"]`, `ProfileUpdateBody = components["schemas"]["ProfileUpdateIn"]`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/api/profiles.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import { useCreateProfile, useProfile, useProfilesList, useUpdateProfile } from "./profiles";

afterEach(() => vi.restoreAllMocks());

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

it("useProfilesList GETs /api/profiles", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: { profiles: [] }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useProfilesList(), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith("/api/profiles", {});
});

it("useProfile GETs the detail by id", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: { id: 5, bootstrap_status: null }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useProfile(5), { wrapper: wrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith("/api/profiles/{profile_id}", { params: { path: { profile_id: 5 } } });
});

it("useCreateProfile POSTs the body and returns the id", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 9 }, error: undefined } as never);
  const qc = new QueryClient();
  const { result } = renderHook(() => useCreateProfile(), { wrapper: wrapper(qc) });
  const out = await result.current.mutateAsync({ name: "X" } as never);
  expect(out).toEqual({ id: 9 });
  expect(post).toHaveBeenCalledWith("/api/profiles", { body: { name: "X" } });
});

it("useUpdateProfile PUTs the body + invalidates", async () => {
  const put = vi.spyOn(clientMod.api, "PUT").mockResolvedValue({ data: { id: 5 }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useUpdateProfile(5), { wrapper: wrapper(qc) });
  await result.current.mutateAsync({ description: "d" } as never);
  expect(put).toHaveBeenCalledWith("/api/profiles/{profile_id}", { params: { path: { profile_id: 5 } }, body: { description: "d" } });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["profile", 5] });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/api/profiles.test.tsx`
Expected: FAIL — cannot resolve `./profiles`.

- [ ] **Step 3: Create the hooks**

Create `frontend/src/api/profiles.ts`:

```ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type ProfileDetail = components["schemas"]["ProfileDetailOut"];
export type ProfileListItem = components["schemas"]["ProfileListItemOut"];
export type ProfileCreateBody = components["schemas"]["ProfileCreateIn"];
export type ProfileUpdateBody = components["schemas"]["ProfileUpdateIn"];

export function useProfilesList() {
  return useQuery({
    queryKey: ["profiles"],
    queryFn: async () => unwrap(await api.GET("/api/profiles", {})),
    retry: false,
  });
}

export function useProfile(id: number) {
  return useQuery({
    queryKey: ["profile", id],
    queryFn: async () =>
      unwrap(await api.GET("/api/profiles/{profile_id}", { params: { path: { profile_id: id } } })),
    retry: false,
    // Poll while the bootstrap job is non-terminal; stop once it settles.
    refetchInterval: (q) => {
      const s = q.state.data?.bootstrap_status;
      return s === "queued" || s === "running" ? 3000 : false;
    },
  });
}

export function useCreateProfile() {
  return useMutation({
    mutationFn: async (body: ProfileCreateBody) => unwrap(await api.POST("/api/profiles", { body })),
  });
}

export function useUpdateProfile(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: ProfileUpdateBody) =>
      unwrap(await api.PUT("/api/profiles/{profile_id}", { params: { path: { profile_id: id } }, body })),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["profile", id] });
      qc.invalidateQueries({ queryKey: ["profiles"] });
    },
  });
}
```

- [ ] **Step 4: Run the test + tsc**

Run: `cd frontend && pnpm exec vitest run src/api/profiles.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS (4); tsc clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/profiles.ts frontend/src/api/profiles.test.tsx
git commit -m "feat(spa): profiles hooks (list/detail-with-poll/create/update)"
```

---

### Task 7: `ProfilesList` route

**Files:**
- Create: `frontend/src/routes/ProfilesList.tsx`, `frontend/src/routes/ProfilesList.module.css`
- Test: `frontend/src/routes/ProfilesList.test.tsx`

**Interfaces:**
- Consumes: `useProfilesList`. Produces: `export function ProfilesList()` (registered at `profiles` in Task 12).

- [ ] **Step 1: Write the failing test**

Create `frontend/src/routes/ProfilesList.test.tsx`:

```tsx
import { screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { ProfilesList } from "./ProfilesList";

afterEach(() => vi.restoreAllMocks());

function mock(profiles: unknown[]) {
  return vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: { profiles }, error: undefined } as never);
}

it("renders profile cards with a detail link", async () => {
  mock([{ id: 1, name: "Lab A", kind: "organizational_unit", level: "group", areas_count: 2, budget_currency: "SEK", description: "Imaging." }]);
  renderWithProviders(<ProfilesList />);
  const link = await screen.findByRole("link", { name: "Lab A" });
  expect(link).toHaveAttribute("href", "/profiles/1");
  expect(screen.getByText(/2 areas/)).toBeInTheDocument();
});

it("renders an empty state with a create link", async () => {
  mock([]);
  renderWithProviders(<ProfilesList />);
  expect(await screen.findByRole("link", { name: /create your first profile/i })).toHaveAttribute("href", "/profiles/new");
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/routes/ProfilesList.test.tsx`
Expected: FAIL — cannot resolve `./ProfilesList`.

- [ ] **Step 3: Create the CSS Module**

Create `frontend/src/routes/ProfilesList.module.css`:

```css
.page { max-width: 56rem; margin: 0 auto; padding: 1rem 0.5rem 3rem; }
.header { display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; }
.header h1 { margin: 0 0 0.25rem; color: var(--app-heading); }
.sub { margin: 0; color: var(--app-muted); font-size: 0.9rem; }
.newBtn {
  background: var(--app-accent); color: #fff; border-radius: 5px;
  padding: 0.4rem 0.9rem; font-weight: 500; text-decoration: none; white-space: nowrap;
}
.cards { display: grid; gap: 0.75rem; margin-top: 1.25rem; }
.card { border: 1px solid var(--app-border); border-radius: 8px; padding: 1rem; background: var(--app-sidebar); }
.card h3 { margin: 0 0 0.3rem; font-size: 1rem; }
.card h3 a { color: var(--app-link); text-decoration: none; }
.meta { color: var(--app-muted); font-size: 0.8rem; }
.desc { margin: 0.4rem 0 0.6rem; color: var(--app-text); font-size: 0.9rem; }
.links { font-size: 0.85rem; }
.links a { color: var(--app-link); text-decoration: none; }
.empty { text-align: center; padding: 3rem 1rem; color: var(--app-muted); }
.empty a {
  display: inline-block; margin-top: 0.5rem; background: var(--app-accent); color: #fff;
  border-radius: 5px; padding: 0.4rem 0.9rem; text-decoration: none;
}
```

- [ ] **Step 4: Create the component**

Create `frontend/src/routes/ProfilesList.tsx`:

```tsx
import { Link } from "react-router-dom";

import { useProfilesList } from "../api/profiles";
import styles from "./ProfilesList.module.css";

export function ProfilesList() {
  const { data, isLoading, isError } = useProfilesList();
  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <p role="alert">Couldn’t load profiles.</p>;
  const profiles = data.profiles;
  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <hgroup>
          <h1>Ranking profiles</h1>
          <p className={styles.sub}>
            Each profile defines who you are and what you fund-hunt for. The LLM ranks calls against the
            description and rubric.
          </p>
        </hgroup>
        {profiles.length > 0 && (
          <Link className={styles.newBtn} to="/profiles/new">+ New profile</Link>
        )}
      </header>
      {profiles.length === 0 ? (
        <div className={styles.empty}>
          <h3>No profiles yet</h3>
          <p>Create one to see ranked calls on the dashboard.</p>
          <Link to="/profiles/new">Create your first profile</Link>
        </div>
      ) : (
        <div className={styles.cards}>
          {profiles.map((p) => (
            <article key={p.id} className={styles.card}>
              <h3><Link to={`/profiles/${p.id}`}>{p.name}</Link></h3>
              <div className={styles.meta}>
                {p.kind}
                {p.areas_count > 0 && ` · ${p.areas_count} areas`}
                {p.budget_currency && ` · ${p.budget_currency}`}
              </div>
              {p.description && (
                <p className={styles.desc}>
                  {p.description.slice(0, 160)}{p.description.length > 160 ? "…" : ""}
                </p>
              )}
              <div className={styles.links}>
                <Link to={`/?profile=${p.id}`}>Ranked feed →</Link>
                {" · "}
                <Link to={`/profiles/${p.id}/edit`}>Edit</Link>
              </div>
            </article>
          ))}
        </div>
      )}
    </main>
  );
}
```

- [ ] **Step 5: Run the test + tsc**

Run: `cd frontend && pnpm exec vitest run src/routes/ProfilesList.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS (2); tsc clean.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/routes/ProfilesList.tsx frontend/src/routes/ProfilesList.module.css frontend/src/routes/ProfilesList.test.tsx
git commit -m "feat(spa): ProfilesList route + module CSS"
```

---

### Task 8: `ProfileDetail` route

**Files:**
- Create: `frontend/src/routes/ProfileDetail.tsx`, `frontend/src/routes/ProfileDetail.module.css`
- Test: `frontend/src/routes/ProfileDetail.test.tsx`

**Interfaces:**
- Consumes: `useProfile(id)`, `useParams`. Produces: `export function ProfileDetail()` (registered at `profiles/:id`).

- [ ] **Step 1: Write the failing test**

Create `frontend/src/routes/ProfileDetail.test.tsx`:

```tsx
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { ProfileDetail } from "./ProfileDetail";

afterEach(() => vi.restoreAllMocks());

const DETAIL = {
  id: 5, name: "Digital Pathology Lab", kind: "organizational_unit", level: "group",
  description: "Imaging research.", budget_currency: "SEK",
  areas: ["oncology"], budget_min: 100, budget_max: 200, trl_min: 3, trl_max: 7,
  must_include: ["pediatric"], hard_exclusions: [],
  org_type: "university_hospital", capabilities: ["biobank"], patient_populations: [],
  scale: null, career_stage: null, role: null, years_since_phd: null, applicant_types: [],
  funding_instruments: ["project_grant"], funder_exclusions: [], country: "SE", host_institution: "KI",
  can_edit: true, bootstrap_status: null, bootstrap_pubmed_status: null, publications_fetched: null,
};

function renderDetail(payload = DETAIL) {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: payload, error: undefined } as never);
  const qc = new QueryClient();
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/profiles/5"]}>
          <Routes><Route path="/profiles/:id" element={<ProfileDetail />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

it("renders the description, rubric, and namespaced fields", async () => {
  renderDetail();
  expect(await screen.findByRole("heading", { name: /Digital Pathology Lab/ })).toBeInTheDocument();
  expect(screen.getByText("Imaging research.")).toBeInTheDocument();
  expect(screen.getByText("oncology")).toBeInTheDocument();
  expect(screen.getByText("biobank")).toBeInTheDocument();
  expect(screen.getByText("project_grant")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /edit/i })).toHaveAttribute("href", "/profiles/5/edit");
});

it("shows not-found on a 404", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: undefined, error: { error: { code: "not_found", message: "profile not found" } },
  } as never);
  const qc = new QueryClient();
  render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/profiles/5"]}>
          <Routes><Route path="/profiles/:id" element={<ProfileDetail />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
  expect(await screen.findByText(/not found/i)).toBeInTheDocument();
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/routes/ProfileDetail.test.tsx`
Expected: FAIL — cannot resolve `./ProfileDetail`.

- [ ] **Step 3: Create the CSS Module**

Create `frontend/src/routes/ProfileDetail.module.css`:

```css
.page { max-width: 48rem; margin: 0 auto; padding: 1rem 0.5rem 3rem; }
.back { color: var(--app-link); text-decoration: none; font-size: 0.85rem; }
.header { display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; margin: 0.5rem 0 1rem; }
.header h1 { margin: 0 0 0.2rem; color: var(--app-heading); }
.kind { margin: 0; color: var(--app-muted); font-size: 0.9rem; }
.actions a {
  display: inline-block; margin-left: 0.5rem; border-radius: 5px; padding: 0.35rem 0.8rem;
  text-decoration: none; font-size: 0.85rem; border: 1px solid var(--app-border-strong); color: var(--app-text);
}
.actions a.primary { background: var(--app-accent); color: #fff; border-color: transparent; }
.card { border: 1px solid var(--app-border); border-radius: 8px; padding: 1rem; background: var(--app-sidebar); margin-bottom: 1rem; }
.card h2 { margin: 0 0 0.6rem; font-size: 1rem; color: var(--app-heading); }
.card dt { color: var(--app-muted); font-size: 0.8rem; margin-top: 0.5rem; }
.card dd { margin: 0.1rem 0 0; }
.chip {
  display: inline-block; background: var(--app-hover); border-radius: 999px;
  padding: 0.1rem 0.55rem; margin: 0.15rem 0.2rem 0 0; font-size: 0.8rem;
}
```

- [ ] **Step 4: Create the component**

Create `frontend/src/routes/ProfileDetail.tsx`:

```tsx
import { Link, useParams } from "react-router-dom";

import type { ProfileDetail as Detail } from "../api/profiles";
import { useProfile } from "../api/profiles";
import styles from "./ProfileDetail.module.css";

function Chips({ items }: { items: string[] }) {
  if (items.length === 0) return <>—</>;
  return <>{items.map((c, i) => <span key={i} className={styles.chip}>{c}</span>)}</>;
}

function range(min: number | null, max: number | null) {
  if (min == null && max == null) return "—";
  return `${min ?? "—"} – ${max ?? "—"}`;
}

export function ProfileDetail() {
  const { id } = useParams();
  const { data, isLoading, isError } = useProfile(Number(id));
  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <main className={styles.page}><p role="alert">Profile not found.</p></main>;
  return <DetailView p={data} />;
}

function DetailView({ p }: { p: Detail }) {
  const hasProfileFields =
    p.org_type || p.capabilities.length || p.patient_populations.length || p.scale ||
    p.career_stage || p.applicant_types.length || p.role || p.years_since_phd != null ||
    p.funding_instruments.length || p.funder_exclusions.length || p.country || p.host_institution;
  return (
    <main className={styles.page}>
      <Link className={styles.back} to="/profiles">← All profiles</Link>
      <header className={styles.header}>
        <hgroup>
          <h1>{p.name}</h1>
          <p className={styles.kind}>{p.kind}{p.level ? ` · ${p.level}` : ""}</p>
        </hgroup>
        <div className={styles.actions}>
          <Link className="primary" to={`/?profile=${p.id}`}>Ranked feed →</Link>
          <Link to={`/profiles/${p.id}/edit`}>Edit</Link>
        </div>
      </header>

      <section className={styles.card}>
        <h2>Description</h2>
        <p>{p.description || "— (no description yet)"}</p>
      </section>

      <section className={styles.card}>
        <h2>Ranking rubric</h2>
        <dl>
          <dt>Areas</dt><dd><Chips items={p.areas} /></dd>
          <dt>Budget</dt>
          <dd>{p.budget_min == null && p.budget_max == null ? "—"
            : `${p.budget_min?.toLocaleString() ?? "—"} – ${p.budget_max?.toLocaleString() ?? "—"}${p.budget_currency ? ` ${p.budget_currency}` : ""}`}</dd>
          <dt>Technology Readiness Level</dt><dd>{range(p.trl_min, p.trl_max)}</dd>
          <dt>Must include</dt><dd><Chips items={p.must_include} /></dd>
          <dt>Hard exclusions</dt><dd><Chips items={p.hard_exclusions} /></dd>
        </dl>
      </section>

      {hasProfileFields && (
        <section className={styles.card}>
          <h2>Profile</h2>
          <dl>
            {p.org_type && (<><dt>Organization type</dt><dd>{p.org_type}</dd></>)}
            {p.capabilities.length > 0 && (<><dt>Capabilities</dt><dd><Chips items={p.capabilities} /></dd></>)}
            {p.patient_populations.length > 0 && (<><dt>Patient populations</dt><dd><Chips items={p.patient_populations} /></dd></>)}
            {p.scale && (<><dt>Scale</dt><dd>{p.scale}</dd></>)}
            {p.career_stage && (<><dt>Career stage</dt><dd>{p.career_stage}</dd></>)}
            {p.role && (<><dt>Role</dt><dd>{p.role}</dd></>)}
            {p.years_since_phd != null && (<><dt>Years since PhD</dt><dd>{p.years_since_phd}</dd></>)}
            {p.applicant_types.length > 0 && (<><dt>Applicant types</dt><dd><Chips items={p.applicant_types} /></dd></>)}
            {p.funding_instruments.length > 0 && (<><dt>Funding instruments</dt><dd><Chips items={p.funding_instruments} /></dd></>)}
            {p.funder_exclusions.length > 0 && (<><dt>Funder exclusions</dt><dd><Chips items={p.funder_exclusions} /></dd></>)}
            {p.host_institution && (<><dt>Host institution</dt><dd>{p.host_institution}</dd></>)}
            {p.country && (<><dt>Country</dt><dd>{p.country}</dd></>)}
          </dl>
        </section>
      )}
    </main>
  );
}
```

- [ ] **Step 5: Run the test + tsc**

Run: `cd frontend && pnpm exec vitest run src/routes/ProfileDetail.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS (2); tsc clean.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/routes/ProfileDetail.tsx frontend/src/routes/ProfileDetail.module.css frontend/src/routes/ProfileDetail.test.tsx
git commit -m "feat(spa): ProfileDetail route + module CSS"
```

---

### Task 9: `StructuredFields` shared form component

The kind-conditional structured inputs shared by New and Edit. Values are flat strings (textareas are newline-joined); the parent owns the form state.

**Files:**
- Create: `frontend/src/routes/StructuredFields.tsx`
- Test: `frontend/src/routes/StructuredFields.test.tsx`

**Interfaces:**
- Produces: `export type StructuredValues` (a record of the 13 flat string fields); `export function StructuredFields({ kind, values, onChange, disabled }: { kind: string; values: StructuredValues; onChange: (k: keyof StructuredValues, v: string) => void; disabled?: boolean })`. Renders the org block when `kind === "organizational_unit"`, the individual block when `kind === "individual"`, plus the common geo/funding/applicant fields.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/routes/StructuredFields.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { StructuredFields, type StructuredValues } from "./StructuredFields";

const EMPTY: StructuredValues = {
  areas: "", career_stage: "", applicant_types: "", role: "", years_since_phd: "",
  org_type: "", capabilities: "", patient_populations: "", scale: "",
  funding_instruments: "", funder_exclusions: "", country: "", host_institution: "",
};

it("shows org fields for an organizational_unit, hides individual fields", () => {
  render(<StructuredFields kind="organizational_unit" values={EMPTY} onChange={vi.fn()} />);
  expect(screen.getByLabelText(/organization type/i)).toBeInTheDocument();
  expect(screen.queryByLabelText(/career stage/i)).toBeNull();
});

it("shows individual fields for an individual, hides org fields", () => {
  render(<StructuredFields kind="individual" values={EMPTY} onChange={vi.fn()} />);
  expect(screen.getByLabelText(/career stage/i)).toBeInTheDocument();
  expect(screen.queryByLabelText(/organization type/i)).toBeNull();
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/routes/StructuredFields.test.tsx`
Expected: FAIL — cannot resolve `./StructuredFields`.

- [ ] **Step 3: Create the component**

Create `frontend/src/routes/StructuredFields.tsx` (reuses the parent form's `.module.css` classes via props is overkill — this component ships unstyled label/inputs; the parent forms wrap it. Use plain semantic elements so the parent CSS Module's descendant selectors style them):

```tsx
export type StructuredValues = {
  areas: string;
  career_stage: string;
  applicant_types: string;
  role: string;
  years_since_phd: string;
  org_type: string;
  capabilities: string;
  patient_populations: string;
  scale: string;
  funding_instruments: string;
  funder_exclusions: string;
  country: string;
  host_institution: string;
};

const ORG_TYPES = [
  ["", "(unspecified)"], ["research_organisation", "Research organisation"],
  ["university_hospital", "University hospital"], ["public_body", "Public body"],
  ["sme", "SME"], ["other", "Other"],
];
const CAREER_STAGES = [
  ["", "(unspecified)"], ["phd_student", "PhD student"], ["postdoc", "Postdoc"],
  ["junior_pi", "Junior PI"], ["senior_pi", "Senior PI"], ["group_leader", "Group leader"],
];

export function StructuredFields({
  kind, values, onChange, disabled,
}: {
  kind: string;
  values: StructuredValues;
  onChange: (k: keyof StructuredValues, v: string) => void;
  disabled?: boolean;
}) {
  const set = (k: keyof StructuredValues) => (e: { target: { value: string } }) => onChange(k, e.target.value);
  return (
    <>
      {kind === "organizational_unit" && (
        <fieldset disabled={disabled}>
          <label>Organization type
            <select value={values.org_type} onChange={set("org_type")}>
              {ORG_TYPES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
          <label>Capabilities <small>(one per line)</small>
            <textarea rows={3} value={values.capabilities} onChange={set("capabilities")} />
          </label>
          <label>Patient populations <small>(one per line)</small>
            <textarea rows={2} value={values.patient_populations} onChange={set("patient_populations")} />
          </label>
          <label>Scale <small>(free text)</small>
            <input type="text" maxLength={200} value={values.scale} onChange={set("scale")} />
          </label>
        </fieldset>
      )}
      {kind === "individual" && (
        <fieldset disabled={disabled}>
          <label>Career stage
            <select value={values.career_stage} onChange={set("career_stage")}>
              {CAREER_STAGES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
          <label>Role <small>(free text)</small>
            <input type="text" maxLength={200} value={values.role} onChange={set("role")} />
          </label>
          <label>Years since PhD <small>(0–80)</small>
            <input type="text" value={values.years_since_phd} onChange={set("years_since_phd")} />
          </label>
        </fieldset>
      )}
      <label>Host institution <small>(free text)</small>
        <input type="text" maxLength={200} value={values.host_institution} onChange={set("host_institution")} />
      </label>
      <label>Country (e.g. SE)
        <input type="text" maxLength={200} value={values.country} onChange={set("country")} />
      </label>
      <label>Applicant types <small>(one per line: solo_pi, consortium_coordinator, consortium_partner, institution)</small>
        <textarea rows={2} value={values.applicant_types} onChange={set("applicant_types")} />
      </label>
      <label>Funding instruments <small>(one per line: project_grant, fellowship, mobility, infrastructure, programme)</small>
        <textarea rows={2} value={values.funding_instruments} onChange={set("funding_instruments")} />
      </label>
      <label>Funder exclusions <small>(one per line)</small>
        <textarea rows={2} value={values.funder_exclusions} onChange={set("funder_exclusions")} />
      </label>
      <label>Research areas <small>(one per line)</small>
        <textarea rows={3} value={values.areas} onChange={set("areas")} />
      </label>
    </>
  );
}
```

- [ ] **Step 4: Run the test + tsc**

Run: `cd frontend && pnpm exec vitest run src/routes/StructuredFields.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS (2); tsc clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/routes/StructuredFields.tsx frontend/src/routes/StructuredFields.test.tsx
git commit -m "feat(spa): shared StructuredFields form component (kind-conditional)"
```

---

### Task 10: `ProfileNew` route (create form)

**Files:**
- Create: `frontend/src/routes/ProfileForm.module.css`, `frontend/src/routes/ProfileNew.tsx`
- Test: `frontend/src/routes/ProfileNew.test.tsx`

**Interfaces:**
- Consumes: `useCreateProfile`, `StructuredFields`, `useNavigate`. Produces: `export function ProfileNew()` (registered at `profiles/new`). Shared `ProfileForm.module.css` is reused by ProfileEdit (Task 11).

- [ ] **Step 1: Write the failing test**

Create `frontend/src/routes/ProfileNew.test.tsx`:

```tsx
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { ProfileNew } from "./ProfileNew";

afterEach(() => vi.restoreAllMocks());

function Probe() { return <span data-testid="loc">{useLocation().pathname}</span>; }

function renderNew() {
  const qc = new QueryClient();
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/profiles/new"]}>
          <Routes>
            <Route path="/profiles/new" element={<ProfileNew />} />
            <Route path="/profiles/:id/edit" element={<Probe />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

it("toggles individual fields when kind changes", () => {
  renderNew();
  expect(screen.getByLabelText(/organization type/i)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText(/^kind/i), { target: { value: "individual" } });
  expect(screen.getByLabelText(/career stage/i)).toBeInTheDocument();
  expect(screen.queryByLabelText(/organization type/i)).toBeNull();
});

it("submits and navigates to the new profile's edit page", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 42 }, error: undefined } as never);
  renderNew();
  fireEvent.change(screen.getByLabelText(/profile name/i), { target: { value: "New Lab" } });
  fireEvent.click(screen.getByRole("button", { name: /bootstrap profile/i }));
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/profiles", expect.objectContaining({ body: expect.objectContaining({ name: "New Lab", kind: "organizational_unit" }) })));
  await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent("/profiles/42/edit"));
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/routes/ProfileNew.test.tsx`
Expected: FAIL — cannot resolve `./ProfileNew`.

- [ ] **Step 3: Create the shared form CSS**

Create `frontend/src/routes/ProfileForm.module.css`:

```css
.page { max-width: 44rem; margin: 0 auto; padding: 1rem 0.5rem 3rem; }
.back { color: var(--app-link); text-decoration: none; font-size: 0.85rem; }
.page h1 { margin: 0.5rem 0 0.25rem; color: var(--app-heading); }
.lede { margin: 0 0 1rem; color: var(--app-muted); font-size: 0.9rem; }
.form label { display: flex; flex-direction: column; gap: 0.25rem; font-size: 0.85rem; margin-bottom: 0.7rem; }
.form label small { color: var(--app-muted); }
.form input, .form select, .form textarea {
  background: var(--app-editor, var(--app-hover)); border: 1px solid var(--app-border-strong);
  border-radius: 5px; padding: 0.4rem 0.55rem; color: var(--app-text); font: inherit;
}
.form input:focus, .form select:focus, .form textarea:focus { outline: 2px solid var(--app-accent); outline-offset: 1px; }
.form fieldset { border: 1px solid var(--app-border); border-radius: 6px; padding: 0.6rem 0.8rem; margin: 0 0 0.7rem; }
.form fieldset:disabled { opacity: 0.6; }
.actions { display: flex; gap: 0.6rem; align-items: center; margin-top: 0.5rem; }
.actions button {
  background: var(--app-accent); color: #fff; border: none; border-radius: 5px;
  padding: 0.45rem 1rem; font: inherit; font-weight: 500; cursor: pointer;
}
.actions button:disabled { opacity: 0.6; cursor: default; }
.actions a { color: var(--app-link); text-decoration: none; font-size: 0.9rem; }
.banner { border: 1px solid var(--app-border); border-radius: 6px; padding: 0.6rem 0.8rem; margin-bottom: 0.8rem; background: var(--app-sidebar); font-size: 0.9rem; }
.bannerWarn { border-color: var(--app-warn, var(--app-bad)); }
.err { color: var(--app-bad); font-size: 0.85rem; }
```

- [ ] **Step 4: Create the component**

Create `frontend/src/routes/ProfileNew.tsx`:

```tsx
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useCreateProfile, type ProfileCreateBody } from "../api/profiles";
import styles from "./ProfileForm.module.css";
import { StructuredFields, type StructuredValues } from "./StructuredFields";

const LEVELS = [
  ["", "(unspecified)"], ["lab", "Lab"], ["group", "Research group"], ["department", "Department"],
  ["clinic", "Clinic"], ["institution", "Institution"], ["other", "Other"],
];

const EMPTY_STRUCT: StructuredValues = {
  areas: "", career_stage: "", applicant_types: "", role: "", years_since_phd: "",
  org_type: "", capabilities: "", patient_populations: "", scale: "",
  funding_instruments: "", funder_exclusions: "", country: "", host_institution: "",
};

export function ProfileNew() {
  const navigate = useNavigate();
  const create = useCreateProfile();
  const [name, setName] = useState("");
  const [kind, setKind] = useState("organizational_unit");
  const [level, setLevel] = useState("");
  const [missionText, setMissionText] = useState("");
  const [orcids, setOrcids] = useState("");
  const [namesAffiliations, setNamesAffiliations] = useState("");
  const [pastedGrants, setPastedGrants] = useState("");
  const [struct, setStruct] = useState<StructuredValues>(EMPTY_STRUCT);
  const setS = (k: keyof StructuredValues, v: string) => setStruct((p) => ({ ...p, [k]: v }));

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const body: ProfileCreateBody = {
      name, kind, level, orcids, names_affiliations: namesAffiliations,
      pasted_grants: pastedGrants, mission_text: missionText, ...struct,
    };
    create.mutate(body, { onSuccess: (out) => navigate(`/profiles/${out.id}/edit`) });
  }

  return (
    <main className={styles.page}>
      <Link className={styles.back} to="/profiles">← All profiles</Link>
      <h1>Create a profile</h1>
      <p className={styles.lede}>
        Pick a kind, add seed data, and the local LLM drafts a description plus rubric. You can edit before it
        goes live.
      </p>
      <form className={styles.form} onSubmit={submit}>
        <label>Profile name
          <input type="text" required maxLength={200} value={name}
                 placeholder="e.g. Digital Pathology Lab" onChange={(e) => setName(e.target.value)} />
        </label>
        <label>Kind
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="organizational_unit">Organizational unit</option>
            <option value="individual">Individual researcher</option>
          </select>
        </label>
        {kind === "organizational_unit" && (
          <>
            <label>Level
              <select value={level} onChange={(e) => setLevel(e.target.value)}>
                {LEVELS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </label>
            <label>Mission / strategy / capabilities <small>(free text)</small>
              <textarea rows={4} value={missionText} onChange={(e) => setMissionText(e.target.value)} />
            </label>
          </>
        )}
        <StructuredFields kind={kind} values={struct} onChange={setS} />
        <label>Member ORCIDs <small>(one per line; for an individual, your own)</small>
          <textarea rows={2} value={orcids} placeholder="0000-0001-2345-6789" onChange={(e) => setOrcids(e.target.value)} />
        </label>
        <label>Members by name <small>(one per line: Last F | Affiliation)</small>
          <textarea rows={2} value={namesAffiliations} onChange={(e) => setNamesAffiliations(e.target.value)} />
        </label>
        <label>Pasted past grants <small>(optional, titles + abstracts)</small>
          <textarea rows={4} value={pastedGrants} onChange={(e) => setPastedGrants(e.target.value)} />
        </label>
        <div className={styles.actions}>
          <button type="submit" disabled={create.isPending}>Bootstrap profile</button>
          {create.isError && (
            <span className={styles.err} role="alert">
              {(create.error as Error | null)?.message ?? "Couldn’t create the profile."}
            </span>
          )}
        </div>
      </form>
    </main>
  );
}
```

- [ ] **Step 5: Run the test + tsc**

Run: `cd frontend && pnpm exec vitest run src/routes/ProfileNew.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS (2); tsc clean.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/routes/ProfileForm.module.css frontend/src/routes/ProfileNew.tsx frontend/src/routes/ProfileNew.test.tsx
git commit -m "feat(spa): ProfileNew create form (kind toggle, seed inputs, StructuredFields)"
```

---

### Task 11: `ProfileEdit` route (edit form + bootstrap polling)

**Files:**
- Create: `frontend/src/routes/ProfileEdit.tsx`
- Test: `frontend/src/routes/ProfileEdit.test.tsx`

**Interfaces:**
- Consumes: `useProfile(id)`, `useUpdateProfile(id)`, `StructuredFields`, the shared `ProfileForm.module.css`, `useParams`/`useNavigate`. Produces: `export function ProfileEdit()` (registered at `profiles/:id/edit`). While `bootstrap_status ∈ {queued,running}` OR `!can_edit`, the fieldset is disabled.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/routes/ProfileEdit.test.tsx`:

```tsx
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { ProfileEdit } from "./ProfileEdit";

afterEach(() => vi.restoreAllMocks());

function Probe() { return <span data-testid="loc">{useLocation().pathname}</span>; }

const BASE = {
  id: 5, name: "Lab", kind: "organizational_unit", level: "group", description: "Old desc.",
  budget_currency: "SEK", areas: ["oncology"], budget_min: 100, budget_max: 200, trl_min: 3, trl_max: 7,
  must_include: [], hard_exclusions: [], org_type: "university_hospital", capabilities: ["biobank"],
  patient_populations: [], scale: null, career_stage: null, role: null, years_since_phd: null,
  applicant_types: [], funding_instruments: ["project_grant"], funder_exclusions: [], country: "SE",
  host_institution: "KI", can_edit: true, bootstrap_status: null, bootstrap_pubmed_status: null,
  publications_fetched: null,
};

function renderEdit(payload = BASE) {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: payload, error: undefined } as never);
  const qc = new QueryClient();
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/profiles/5/edit"]}>
          <Routes>
            <Route path="/profiles/:id/edit" element={<ProfileEdit />} />
            <Route path="/profiles/:id" element={<Probe />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

it("pre-fills from the read-model and saves, navigating to detail", async () => {
  const put = vi.spyOn(clientMod.api, "PUT").mockResolvedValue({ data: { id: 5 }, error: undefined } as never);
  renderEdit();
  const desc = await screen.findByLabelText(/description/i);
  expect(desc).toHaveValue("Old desc.");
  fireEvent.change(desc, { target: { value: "New desc." } });
  fireEvent.click(screen.getByRole("button", { name: /save and re-rank/i }));
  await waitFor(() => expect(put).toHaveBeenCalledWith("/api/profiles/{profile_id}",
    expect.objectContaining({ params: { path: { profile_id: 5 } }, body: expect.objectContaining({ description: "New desc." }) })));
  await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent("/profiles/5"));
});

it("disables the form and shows the banner while drafting", async () => {
  renderEdit({ ...BASE, bootstrap_status: "running" });
  const desc = await screen.findByLabelText(/description/i);
  expect(desc).toBeDisabled();
  expect(screen.getByText(/drafting/i)).toBeInTheDocument();
});

it("disables the form when the viewer cannot edit", async () => {
  renderEdit({ ...BASE, can_edit: false });
  const desc = await screen.findByLabelText(/description/i);
  expect(desc).toBeDisabled();
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/routes/ProfileEdit.test.tsx`
Expected: FAIL — cannot resolve `./ProfileEdit`.

- [ ] **Step 3: Create the component**

Create `frontend/src/routes/ProfileEdit.tsx`:

```tsx
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { useProfile, useUpdateProfile, type ProfileDetail, type ProfileUpdateBody } from "../api/profiles";
import styles from "./ProfileForm.module.css";
import { StructuredFields, type StructuredValues } from "./StructuredFields";

const LEVELS = [
  ["", "(unspecified)"], ["lab", "Lab"], ["group", "Research group"], ["department", "Department"],
  ["clinic", "Clinic"], ["institution", "Institution"], ["other", "Other"],
];
const CURRENCIES = ["", "SEK", "EUR", "USD", "NOK", "DKK"];

function joinLines(xs: string[]) { return xs.join("\n"); }

export function ProfileEdit() {
  const { id } = useParams();
  const pid = Number(id);
  const { data, isLoading, isError } = useProfile(pid);
  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <main className={styles.page}><p role="alert">Profile not found.</p></main>;
  return <EditForm key={data.bootstrap_status ?? "ready"} p={data} pid={pid} />;
}

function EditForm({ p, pid }: { p: ProfileDetail; pid: number }) {
  const navigate = useNavigate();
  const update = useUpdateProfile(pid);
  const drafting = p.bootstrap_status === "queued" || p.bootstrap_status === "running";
  const locked = drafting || !p.can_edit;

  const [description, setDescription] = useState(p.description);
  const [level, setLevel] = useState(p.level ?? "");
  const [budgetMin, setBudgetMin] = useState(p.budget_min?.toString() ?? "");
  const [budgetMax, setBudgetMax] = useState(p.budget_max?.toString() ?? "");
  const [currency, setCurrency] = useState(p.budget_currency ?? "");
  const [trlMin, setTrlMin] = useState(p.trl_min?.toString() ?? "");
  const [trlMax, setTrlMax] = useState(p.trl_max?.toString() ?? "");
  const [mustInclude, setMustInclude] = useState(joinLines(p.must_include));
  const [hardExclusions, setHardExclusions] = useState(joinLines(p.hard_exclusions));
  const [struct, setStruct] = useState<StructuredValues>({
    areas: joinLines(p.areas), career_stage: p.career_stage ?? "", applicant_types: joinLines(p.applicant_types),
    role: p.role ?? "", years_since_phd: p.years_since_phd?.toString() ?? "", org_type: p.org_type ?? "",
    capabilities: joinLines(p.capabilities), patient_populations: joinLines(p.patient_populations),
    scale: p.scale ?? "", funding_instruments: joinLines(p.funding_instruments),
    funder_exclusions: joinLines(p.funder_exclusions), country: p.country ?? "", host_institution: p.host_institution ?? "",
  });
  const setS = (k: keyof StructuredValues, v: string) => setStruct((s) => ({ ...s, [k]: v }));

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const body: ProfileUpdateBody = {
      description, level, budget_min: budgetMin, budget_max: budgetMax, budget_currency: currency,
      trl_min: trlMin, trl_max: trlMax, must_include: mustInclude, hard_exclusions: hardExclusions, ...struct,
    };
    update.mutate(body, { onSuccess: () => navigate(`/profiles/${pid}`) });
  }

  return (
    <main className={styles.page}>
      <Link className={styles.back} to={`/profiles/${pid}`}>← Back to profile</Link>
      <h1>Edit {p.name}</h1>
      <p className={styles.lede}>
        Description feeds the semantic similarity gate. Areas, budgets, TRL, must-include and exclusions feed the
        deterministic rubric gates. Saving kicks off a backfill of the last 90 days against this profile.
      </p>
      {drafting && (
        <div className={styles.banner}>⏳ Drafting your profile from PubMed and past grants — this can take a minute on the local model.</div>
      )}
      {p.bootstrap_status === "done" && (
        <div className={styles.banner}>
          Draft ready.
          {p.bootstrap_pubmed_status === "ok" && ` Found ${p.publications_fetched} publications for your members.`}
          {p.bootstrap_pubmed_status === "empty" && " No publications matched your members on PubMed — the draft rests on your mission text and pasted grants."}
          {p.bootstrap_pubmed_status === "unavailable" && " PubMed was unavailable, so the draft rests on your mission text and pasted grants."}
        </div>
      )}
      {p.bootstrap_status === "failed" && (
        <div className={`${styles.banner} ${styles.bannerWarn}`}>We couldn’t auto-draft a description (the local model was unavailable). Write one below and save.</div>
      )}
      <form className={styles.form} onSubmit={submit}>
        <fieldset disabled={locked}>
          <label>Description
            <textarea rows={6} value={description} onChange={(e) => setDescription(e.target.value)} />
            <small>Changing this triggers a background re-embed.</small>
          </label>
          {p.kind === "organizational_unit" && (
            <label>Level
              <select value={level} onChange={(e) => setLevel(e.target.value)}>
                {LEVELS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </label>
          )}
          <StructuredFields kind={p.kind} values={struct} onChange={setS} disabled={locked} />
          <label>Budget min
            <input type="text" value={budgetMin} onChange={(e) => setBudgetMin(e.target.value)} />
          </label>
          <label>Budget max
            <input type="text" value={budgetMax} onChange={(e) => setBudgetMax(e.target.value)} />
          </label>
          <label>Currency
            <select value={currency} onChange={(e) => setCurrency(e.target.value)}>
              {CURRENCIES.map((c) => <option key={c} value={c}>{c || "(unknown)"}</option>)}
            </select>
          </label>
          <label>Technology Readiness Level — min <small>(1–9)</small>
            <input type="text" value={trlMin} onChange={(e) => setTrlMin(e.target.value)} />
          </label>
          <label>Technology Readiness Level — max <small>(1–9)</small>
            <input type="text" value={trlMax} onChange={(e) => setTrlMax(e.target.value)} />
          </label>
          <label>Must-include keywords <small>(one per line; bypass the timeline gate)</small>
            <textarea rows={3} value={mustInclude} onChange={(e) => setMustInclude(e.target.value)} />
          </label>
          <label>Hard exclusions <small>(one per line)</small>
            <textarea rows={3} value={hardExclusions} onChange={(e) => setHardExclusions(e.target.value)} />
          </label>
          <div className={styles.actions}>
            <button type="submit" disabled={locked || update.isPending}>Save and re-rank</button>
            <Link to={`/profiles/${pid}`}>Cancel</Link>
            {update.isError && (
              <span className={styles.err} role="alert">
                {(update.error as Error | null)?.message ?? "Couldn’t save."}
              </span>
            )}
          </div>
        </fieldset>
      </form>
    </main>
  );
}
```

(`key={data.bootstrap_status ?? "ready"}` remounts the form when the poll flips `running → done`, so the freshly-drafted fields become the form's initial values.)

- [ ] **Step 4: Run the test + tsc**

Run: `cd frontend && pnpm exec vitest run src/routes/ProfileEdit.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS (3); tsc clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/routes/ProfileEdit.tsx frontend/src/routes/ProfileEdit.test.tsx
git commit -m "feat(spa): ProfileEdit route (pre-fill, bootstrap polling/disable, save)"
```

---

### Task 12: Wire routes + manifest prefix + nav flip

Register the four routes, add the `/profiles` SPA-ownership prefix (regenerate), flip the shell nav item to SPA, convert the Settings `/profiles/new` link, and update the drift-guard tests. The four router routes + the manifest prefix land in this one commit (set-equality drift guard).

**Files:**
- Modify: `frontend/src/router.tsx`, `frontend/routes.json`
- Modify (generated): `frontend/src/routes.gen.ts`, `src/call_manager/web/spa_routes.py`
- Modify: `frontend/src/shell/nav-items.ts`, `frontend/src/shell/ActivityBar.test.tsx`
- Modify: `frontend/src/routes/Settings.tsx`
- Modify: `tests/test_gen_routes.py`, `frontend/src/dev-proxy.test.ts`

- [ ] **Step 1: Add `/profiles` to the manifest and regenerate**

In `frontend/routes.json`, change the `spa_routes` line:

```json
  "spa_routes": ["/", "/cockpit", "/calls", "/me", "/profiles"],
```

Leave `backend_prefixes` UNCHANGED (`/profiles` stays — the Jinja routes still exist until Task 13). Then:

Run: `uv run python scripts/gen_routes.py`
Expected: `routes.gen.ts` `SPA_ROUTES` + `spa_routes.py` `SPA_ROUTES` now include `/profiles`.

- [ ] **Step 2: Register the React routes**

In `frontend/src/router.tsx`, add the imports and the four route children (after `calls/:id`, before `"*"`):

```tsx
import { ProfileDetail } from "./routes/ProfileDetail";
import { ProfileEdit } from "./routes/ProfileEdit";
import { ProfileNew } from "./routes/ProfileNew";
import { ProfilesList } from "./routes/ProfilesList";
```

```tsx
      { path: "profiles", element: <ProfilesList /> },
      { path: "profiles/new", element: <ProfileNew /> },
      { path: "profiles/:id", element: <ProfileDetail /> },
      { path: "profiles/:id/edit", element: <ProfileEdit /> },
```

(No `handle` — full-width pages, no sidebar, like `calls/:id`.)

- [ ] **Step 3: Flip the shell nav item to SPA + convert the Settings link**

In `frontend/src/shell/nav-items.ts`, change the `profiles` entry's `kind`:

```tsx
  { id: "profiles", label: "Profiles", icon: "account", kind: "spa", to: "/profiles" },
```

In `frontend/src/routes/Settings.tsx`, the empty-subscriptions "create one" link is a full-page `<a href="/profiles/new">`. Now that `/profiles` is in-app, convert it to a router `Link`. Add `import { Link } from "react-router-dom";` if not present, and change:

```tsx
          No profiles to subscribe to yet — <Link to="/profiles/new">create one</Link>.
```

- [ ] **Step 4: Update the nav test**

In `frontend/src/shell/ActivityBar.test.tsx`, the test `Profiles and Settings still deep-link to their Jinja pages` (after the account slice it asserts Profiles `href="/profiles"` + Settings `href="/me"`). Profiles now routes in-app; the `href` is unchanged (a `NavLink` still renders `<a href="/profiles">`), so only the name/intent updates. Rename it:

```tsx
it("Profiles and Settings route in-app (SPA nav links)", () => {
  renderWithProviders(<ActivityBar />, { route: "/" });
  expect(screen.getByRole("link", { name: "Profiles" })).toHaveAttribute("href", "/profiles");
  expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/me");
});
```

- [ ] **Step 5: Update the route-ownership guard tests (`/profiles` now SPA-owned)**

In `tests/test_gen_routes.py`, in `test_is_spa_route_is_exact_not_prefix`, add:

```python
    assert is_spa_route("/profiles") is True  # /profiles is now an SPA-owned prefix
    assert is_spa_route("/profiles/5/edit") is True
```

In `frontend/src/dev-proxy.test.ts`:
- In the "owns the root and the /calls prefix" test, add `expect(isSpaRoute("/profiles")).toBe(true);` and rename it to include `/profiles`.
- In the `shouldServeViaVite` test, add `expect(shouldServeViaVite("/profiles/5/edit")).toBe(true); // owned profiles route`.
- LEAVE the explicit-manifest branch tests (`isSpaRoute(..., flipped)` / `["/app"]`) UNCHANGED.

- [ ] **Step 6: Run the affected tests + build**

Run:
```bash
cd frontend && pnpm exec vitest run src/router.test.tsx src/dev-proxy.test.ts src/shell/ActivityBar.test.tsx && pnpm exec tsc --noEmit
cd .. && uv run pytest tests/test_gen_routes.py -q
uv run python scripts/gen_routes.py --check
cd frontend && pnpm build
```
Expected: all PASS; gen-routes in sync; build clean. (`router.test.tsx` set-equality holds: the four `profiles*` leaves collapse to `/profiles`, so `{"/", "/cockpit", "/calls", "/me", "/profiles"} == SPA_ROUTES`.)

- [ ] **Step 7: Commit**

```bash
git add frontend/routes.json frontend/src/routes.gen.ts src/call_manager/web/spa_routes.py \
        frontend/src/router.tsx frontend/src/shell/nav-items.ts frontend/src/shell/ActivityBar.test.tsx \
        frontend/src/routes/Settings.tsx tests/test_gen_routes.py frontend/src/dev-proxy.test.ts
git commit -m "feat(spa): own /profiles in the manifest + wire 4 routes + flip Profiles nav to SPA"
```

---

### Task 13: Jinja teardown — delete the `/profiles` routes/templates/module + repoint

Delete the Jinja profiles surface, remove `/profiles` from `backend_prefixes`, regenerate both artifact sets, and re-repoint the `test_web_auth.py` tests onto `/admin`.

**Files:**
- Modify: `src/call_manager/main.py` (drop the Jinja `profiles` import + `include_router`)
- Delete: `src/call_manager/web/routes/profiles.py`, `src/call_manager/web/templates/profiles_index.html`, `profile_new.html`, `profile_detail.html`, `profile_edit.html`, `src/call_manager/web/templates/partials/bootstrap_banner.html`
- Modify: `frontend/routes.json` (remove `/profiles` from `backend_prefixes`)
- Modify (generated): `frontend/src/routes.gen.ts`, `src/call_manager/web/spa_routes.py`, `frontend/src/api/schema.gen.ts`
- Modify: `tests/test_gen_routes.py`, `tests/test_web_auth.py`
- Create: `tests/test_profiles_jinja_surface_absent.py`

- [ ] **Step 1: Re-repoint the three `/profiles` auth tests FIRST (they break the moment the Jinja routes die)**

In `tests/test_web_auth.py`, repoint the three tests the account slice parked on `/profiles` onto still-Jinja `/admin` routes (verified targets — `admin_index` is HTMLResponse + `require_admin`, which 401s before the admin 403 check; `admin_rerank` has no Form/path params):
- `test_html_401_get_threads_next`: `GET /profiles?x=1` → `GET /admin?x=1`; expected location → `"/login?next=%2Fadmin%3Fx%3D1"` (KEEP the query string — this test asserts query-threading).
- `test_json_401_unaffected_by_next`: `GET /profiles` → `GET /admin` (no `Accept: text/html`) → still raw 401 JSON.
- `test_html_401_post_does_not_thread_next`: `POST /profiles/5/edit` → `POST /admin/rerank`; expected location stays `"/login"`.

Run: `uv run pytest tests/test_web_auth.py -q`
Expected: PASS — confirm the exact `next=` encoding for the GET test; adjust to match if `safe_next` normalizes the query.

- [ ] **Step 2: Add a route-absence guard + flip the backend-route assertion**

Append to `tests/test_gen_routes.py` `test_is_backend_route_for_proxy_carveout`:

```python
    assert is_backend_route("/profiles/5/edit") is False  # /profiles retired from backend_prefixes
```

Create `tests/test_profiles_jinja_surface_absent.py`:

```python
def test_no_jinja_profiles_routes_remain(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    from call_manager.main import create_app

    app = create_app()
    paths = {r.path for r in app.routes}
    # Jinja HTML routes gone:
    assert "/profiles" not in paths
    assert "/profiles/new" not in paths
    assert "/profiles/{profile_id}" not in paths
    assert "/profiles/{profile_id}/edit" not in paths
    assert "/profiles/{profile_id}/bootstrap-status" not in paths
    # /api/profiles* remain:
    assert "/api/profiles" in paths
    assert "/api/profiles/{profile_id}" in paths
```

Run: `uv run pytest tests/test_gen_routes.py::test_is_backend_route_for_proxy_carveout tests/test_profiles_jinja_surface_absent.py -q`
Expected: FAIL — the Jinja routes still mounted + `/profiles` still in `backend_prefixes`.

- [ ] **Step 3: Remove `/profiles` from `backend_prefixes` and regenerate routes**

In `frontend/routes.json`, drop `"/profiles"` from `backend_prefixes`:

```json
  "backend_prefixes": ["/api", "/healthz", "/login", "/logout", "/register",
    "/auth", "/admin", "/workspaces", "/static"],
```

Run: `uv run python scripts/gen_routes.py`

- [ ] **Step 4: Unmount + delete the Jinja `profiles` module + templates**

In `src/call_manager/main.py`: remove `profiles` from the `from call_manager.web.routes import (...)` import tuple and remove the `app.include_router(profiles.router)` line. **Only the Jinja router** (`web.routes.profiles`) — the `/api` profiles router (`web.api.profiles`, mounted via `api_router`) stays. Verify: `grep -n "routes import\|include_router(profiles" src/call_manager/main.py`.

```bash
git rm src/call_manager/web/routes/profiles.py \
       src/call_manager/web/templates/profiles_index.html \
       src/call_manager/web/templates/profile_new.html \
       src/call_manager/web/templates/profile_detail.html \
       src/call_manager/web/templates/profile_edit.html \
       src/call_manager/web/templates/partials/bootstrap_banner.html
```

Run: `uv run python -c "import call_manager.main"`
Expected: boots.

- [ ] **Step 5: Regenerate the OpenAPI types (drop the Jinja `/profiles` HTML paths)**

Run: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`
Then:

```bash
grep -c '"/profiles"' frontend/src/api/schema.gen.ts        # expect 0 (Jinja pages gone)
grep -c '"/api/profiles"' frontend/src/api/schema.gen.ts     # expect >=1
cd frontend && pnpm build
```
Expected: 0 / ≥1 / build clean.

- [ ] **Step 6: Hardcoded-link audit + severance recheck**

Run:
```bash
grep -rn '"/profiles\|=/profiles\|href="/profiles' src/call_manager/web/templates/ frontend/src 2>/dev/null | grep -v "test\|spa_routes\|routes.gen\|schema.gen" || echo "no stray Jinja/React /profiles links"
grep -rn "web.routes.profiles\|web\.routes import.*profiles" src/ tests/ || echo "severed: nothing imports the deleted module"
```
Expected: `base.html`'s nav `<a href="/profiles">` may remain (it full-navs to the SPA now — keep it). No imports of the deleted module. React refs are all router `Link`s.

- [ ] **Step 7: Run the slice's guards + the re-repointed auth tests**

Run:
```bash
uv run pytest tests/test_gen_routes.py tests/test_profiles_jinja_surface_absent.py tests/test_web_auth.py \
              tests/test_api_profiles_list.py tests/test_api_profile_detail.py tests/test_api_profiles_create.py \
              tests/test_api_profile_update.py tests/test_profiles_forms.py -q
uv run ruff check src/
cd frontend && pnpm test && pnpm build
```
Expected: all green; ruff clean; build clean.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(teardown): delete Jinja /profiles routes/templates/module; retire /profiles backend prefix; repoint auth tests"
```

---

## Verification (whole-slice gate, before merge)

- `uv run python -c "import call_manager.main"` boots.
- Backend: `uv run pytest tests/test_api_profiles_list.py tests/test_api_profile_detail.py tests/test_api_profiles_create.py tests/test_api_profile_update.py tests/test_profiles_forms.py tests/test_gen_routes.py tests/test_web_auth.py tests/test_profiles_jinja_surface_absent.py -q` green; `uv run ruff check src/` clean. (Whole-suite `uv run pytest --collect-only -q` shows no orphaned imports of the deleted module.)
- Frontend: `cd frontend && pnpm test` green; `pnpm build` clean.
- Manifest: `uv run python scripts/gen_routes.py --check` in sync; `SPA_ROUTES` gained `/profiles`, `BACKEND_PREFIXES` dropped `/profiles`.
- Schema: `schema.gen.ts` has `/api/profiles` + `/api/profiles/{profile_id}` + `ProfileDetailOut`/`ProfileCreateIn`/`ProfileUpdateIn`; the four Jinja `/profiles*` HTML paths are gone.
- Route-table: no non-`/api` route under `/profiles`.

## Self-Review

**Spec coverage:** §3.1 list → T2; detail/edit/poll read-model (flat projection, can_edit, bootstrap nulling, funding_instruments asymmetry) → T3; create+bootstrap (validation order) → T4; update (validation order, 403/409, re-namespace, post_edit driver) → T5. §3.1 helper relocation (forms.py + post_edit.py, `_bounded_int` rename) → T1. §3.2 hooks → T6; ProfilesList → T7; ProfileDetail → T8; StructuredFields → T9; ProfileNew → T10; ProfileEdit (polling/disable) → T11; nav flip + Settings link → T12. §3.3 manifest prefix + four routes → T12; backend_prefixes removal → T13. §6 gates: offline-test monkeypatch (T5), three-auth-test repoint → /admin incl. `?x=1` threading (T13), route-absence guard (T13), hardcoded-link audit (T13). The two flagged consolidations (bootstrap-status fold + `!can_edit` nulling) → T3/T6/T11.

**Placeholder scan:** every code step carries complete code; commands have expected output. The one run-to-confirm value is the auth `next=` encoding (T13 Step 1) — correct TDD posture for an env-derived string.

**Type consistency:** hook names `useProfilesList`/`useProfile`/`useCreateProfile`/`useUpdateProfile` (T6) match their T7–T11 call sites. `StructuredValues` (T9) is consumed identically by ProfileNew (T10) and ProfileEdit (T11). Backend models `ProfilesListOut`/`ProfileListItemOut`/`ProfileDetailOut`/`ProfileCreateIn`/`ProfileCreateOut`/`ProfileUpdateIn`/`ProfileUpdateOut` are consistent across T2–T6; the flat-field names match between `_flatten_structured` (T3), `ProfileDetailOut` (T3), the React forms (T10/T11), and `parse_structured`'s `raw` keys (T4/T5) — including `funding_instruments` (form key) ↔ `funding.instruments` (stored). Relocated helpers `bounded_int_or_400`/`split_lines`/`parse_members_strict`/`ALLOWED_CURRENCIES`/bounds (T1) are imported by name in T4/T5.
