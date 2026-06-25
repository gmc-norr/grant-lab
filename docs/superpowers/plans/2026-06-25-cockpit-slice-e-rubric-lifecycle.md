# Cockpit Slice E — Rubric Lifecycle + Setup→Active Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Bring the funder-rubric review + `setup→active` activation into the React cockpit, make the rubric editable for the workspace's life, and delete the Jinja rubric-review/setup-status routes it replaces — all additive (no DB schema change), repointing BOTH setup entry points (create + list).

**Architecture:** A new owner-gated `/api` module `web/api/rubric.py` (GET rubric, GET setup-status [member], POST activate, PUT rubric) with one shared `_apply_rubric` saver that preserves criterion passthrough fields (`subcriteria`) and writes `rubric_edited` only on real change. The cockpit route branches on `current_phase`: `setup` → a new `WorkspaceSetup` view (polls setup-status; owner edits the rubric and activates); `active` → today's cockpit plus an owner "Edit rubric" modal. Per-slice Jinja teardown follows.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 / Postgres (testcontainers) / `uv` / `ruff` / pytest · React 19 + Vite + react-router-dom 7 + TanStack Query 5 + Mantine v9 + Vitest + Testing Library · `openapi-fetch` against `schema.gen.ts`.

## Global Constraints

- **No DB schema change**; alembic head untouched (`workspaces`, `workspace_setup_jobs`, `workspace_events` all exist).
- **New `/api` module** `src/call_manager/web/api/rubric.py` mounted via `web/api/__init__.py` (after `settings`). Reuse `require_owner`/`require_member` from `proposal_assist/web/_perms.py`; do NOT extract a new shared helper.
- **Permissions:** GET rubric / POST activate / PUT rubric are **owner-gated**; GET setup-status is **member-gated** (`last_error` owner-only).
- **400 envelope code is `bad_request`** (the global `StarletteHTTPException` handler, `web/api/errors.py`), NOT `invalid`. Semantic validation raises `HTTPException(400, ...)` in-handler (mirroring `settings.py`), not via Pydantic `Field` constraints (those 422).
- **Weights are relative** — surface a normalized-% readout; never enforce a sum rule. **Criteria list must be non-empty** (≥1, parity). **`subcriteria` (and any unknown criterion field) round-trips verbatim** — never dropped.
- **Activation event:** emit `WorkspaceEvent(kind="workspace_activated", actor_id=owner, payload={})`. Render under the EXISTING activity category `"rubric"` (no `ActivityRowOut.category` Literal change, no migration).
- **OpenAPI regen requires `EMBEDDING_MODEL=bge-m3`**; additive diff only; **no `routes.gen.ts` change** (`frontend/src/routes.gen.ts` — all in-cockpit, no new SPA route).
- **Backend tests:** targeted files only (the whole suite drops testcontainers Postgres under load). `ruff check` + `ruff format` touched files; ruff fails on unused imports (F401) — split imports across Task 1/2 so each commit is clean.
- **Frontend:** Node 22 + `pnpm`; `pnpm exec tsc --noEmit` is the gate; `pnpm vitest run <file>`; `pnpm build`. No ESLint.
- **Per-slice teardown (this slice):** delete the Jinja rubric-review + setup-status routes/templates; convert the Jinja `workspace_main.html` setup branch to a static notice (do NOT delete the whole two-pane page — broader cutover is later).

## Reuse map (exact signatures — call these; do not reimplement)

- `require_owner` / `require_member(*, workspace_id: uuid.UUID, user, session) -> Workspace` — `proposal_assist/web/_perms.py` (404 if missing, 403 if not owner/member; filter `soft_deleted_at IS NULL`; return the `Workspace`).
- `Workspace` columns: `rubric_criteria` (JSONB `list[dict]`), `rubric_funder_notes` (TEXT nullable), `rubric_updated_at` (DateTime tz, NOT NULL), `current_phase` (`"setup"|"active"`), `owner_id`. `WorkspaceSetupJob(workspace_id, status, last_error, completed_at)` — status ∈ `queued|running|waiting_for_rubric_review|done|failed`, UNIQUE on `workspace_id` (`db/models.py:1352-1392`). `WorkspaceEvent(workspace_id, kind, payload JSONB, actor_id nullable)`.
- The Jinja write path being replaced (`proposal_assist/web/rubric_review.py:88-149`): enforces ≥1 criterion (400), writes `rubric_edited` only when changed (payload carries full old/new), then `job.status="done"` + `job.completed_at` + `ws.current_phase="active"` in one commit.
- `/api` module idioms (see `web/api/settings.py`): `from call_manager.auth.dependencies import get_current_user`; `with request.app.state.session_factory() as session:`; handlers `raise HTTPException`; `response_model=` on each route; mount via `web/api/__init__.py`.
- Frontend: `api`, `unwrap` from `./client` (`unwrap(await api.GET/POST/PUT(...))`); query/mutation patterns + `retry:false` for owner-gated reads (see `api/settings.ts`, `api/workspace.ts`); Mantine `Modal` mount-gated on `isOwner && open` (see `shell/TitleBar.tsx:41`); modal/component tests render via `renderWithProviders` from `frontend/src/test-utils.tsx` (MantineProvider + QueryClient + MemoryRouter).

## Test harness notes

- **Backend:** new `tests/test_api_rubric.py` mirrors `tests/test_api_settings.py`: `from tests.proposal_assist._refs_helpers import add_member, authed_client, build_app`, `from tests.proposal_assist.conftest import make_user, make_workspace`. Seed setup-job rows with `WorkspaceSetupJob(workspace_id=ws.id, status="waiting_for_rubric_review")`; for active-phase tests set `ws.current_phase = "active"`.
- **Frontend:** transport-level tests spy `vi.spyOn(clientMod.api, "GET"/"POST"/"PUT")`; hook-mock tests use `vi.mock("../api/rubric", ...)` with a `vi.hoisted(() => ({...}))` object (a plain top-level `const fn = vi.fn()` trips Vitest hoisting). Mantine components render via `renderWithProviders`.

---

## Task 1: `web/api/rubric.py` — read endpoints (GET rubric, GET setup-status) + mount

**Files:**
- Create: `src/call_manager/web/api/rubric.py`
- Modify: `src/call_manager/web/api/__init__.py`
- Test: `tests/test_api_rubric.py` (create)

**Interfaces:**
- Produces: `GET /api/workspaces/{id}/rubric` (owner) → `RubricOut{criteria:[CriterionOut], funder_notes: str|None, updated_at: datetime}`; `GET /api/workspaces/{id}/setup-status` (member) → `SetupStatusOut{status: str, last_error: str|None}` (`last_error` owner-only, missing job → `"queued"`). `CriterionOut` allows extra fields (subcriteria). Consumed by Tasks 2, 4, 5.

- [ ] **Step 1: Write the failing test** — create `tests/test_api_rubric.py`:

```python
"""JSON /api rubric + setup-status + activation endpoints (Cockpit Slice E)."""

from tests.proposal_assist._refs_helpers import add_member, authed_client, build_app
from tests.proposal_assist.conftest import make_user, make_workspace


def _setup(pg_session_factory, *, phase="setup", job_status="waiting_for_rubric_review",
           criteria=None, last_error=None):
    """Return (ws_id, owner_id, member_id, stranger_id)."""
    from call_manager.db.models import WorkspaceSetupJob

    with pg_session_factory() as s:
        owner = make_user(s, email="owner@ki.se")
        member = make_user(s, email="member@ki.se")
        stranger = make_user(s, email="stranger@ki.se")
        ws = make_workspace(s, owner=owner)
        ws.current_phase = phase
        ws.rubric_criteria = criteria if criteria is not None else [
            {"name": "Impact", "description": "d", "weight": 0.6, "scale_max": 5,
             "subcriteria": [{"name": "reach", "description": "x"}]},
            {"name": "Feasibility", "description": "d", "weight": 0.4, "scale_max": 5},
        ]
        ws.rubric_funder_notes = "notes"
        if job_status is not None:
            s.add(WorkspaceSetupJob(workspace_id=ws.id, status=job_status, last_error=last_error))
        s.commit()
        add_member(s, workspace_id=ws.id, user=member)
        return ws.id, owner.id, member.id, stranger.id


def test_get_rubric_owner_returns_criteria_with_subcriteria(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).get(f"/api/workspaces/{ws_id}/rubric")
    assert r.status_code == 200, r.text
    body = r.json()
    assert {c["name"] for c in body["criteria"]} == {"Impact", "Feasibility"}
    impact = next(c for c in body["criteria"] if c["name"] == "Impact")
    assert impact["subcriteria"] == [{"name": "reach", "description": "x"}]  # passthrough preserved
    assert body["funder_notes"] == "notes"


def test_get_rubric_member_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=member_id).get(f"/api/workspaces/{ws_id}/rubric")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_setup_status_member_sees_status_no_error(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory, job_status="failed", last_error="boom")
    r = authed_client(app, user_id=member_id).get(f"/api/workspaces/{ws_id}/setup-status")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "failed", "last_error": None}  # member never sees the error


def test_setup_status_owner_sees_error(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory, job_status="failed", last_error="boom")
    r = authed_client(app, user_id=owner_id).get(f"/api/workspaces/{ws_id}/setup-status")
    assert r.json() == {"status": "failed", "last_error": "boom"}


def test_setup_status_missing_job_is_queued(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory, job_status=None)
    r = authed_client(app, user_id=owner_id).get(f"/api/workspaces/{ws_id}/setup-status")
    assert r.json()["status"] == "queued"


def test_setup_status_stranger_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, _, stranger_id = _setup(pg_session_factory)
    r = authed_client(app, user_id=stranger_id).get(f"/api/workspaces/{ws_id}/setup-status")
    assert r.status_code == 403
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_api_rubric.py -x -q` → FAIL (404, routes not mounted).

- [ ] **Step 3: Create the module** — `src/call_manager/web/api/rubric.py` (read endpoints only; imports limited to what they use — Task 2 extends them):

```python
"""JSON /api surface for the funder rubric + setup gate (Cockpit Slice E).

GET rubric / activate / PUT rubric are OWNER-gated; GET setup-status is member-
gated (last_error owner-only). Replaces the Jinja rubric-review + setup-status
routes (deleted in this slice). No DB schema change.

# NOTE: Task 2 appends the write endpoints (activate, PUT rubric) + _apply_rubric
# and EXTENDS the imports below (HTTPException, WorkspaceEvent, datetime/UTC).
# They are intentionally absent here so Task 1's `ruff check` stays F401-clean.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import User, WorkspaceSetupJob
from call_manager.proposal_assist.web._perms import require_member, require_owner

router = APIRouter()


class CriterionOut(BaseModel):
    # extra="allow" round-trips passthrough fields (e.g. subcriteria) the editor
    # must not drop. Serialization includes them; OpenAPI emits additionalProperties.
    model_config = ConfigDict(extra="allow")
    name: str
    description: str
    weight: float
    scale_max: int


class RubricOut(BaseModel):
    criteria: list[CriterionOut]
    funder_notes: str | None
    updated_at: object  # datetime; serialized to ISO by FastAPI


class SetupStatusOut(BaseModel):
    status: str
    last_error: str | None


def _rubric_out(ws) -> RubricOut:
    return RubricOut(
        criteria=[CriterionOut(**c) for c in (ws.rubric_criteria or [])],
        funder_notes=ws.rubric_funder_notes,
        updated_at=ws.rubric_updated_at,
    )


@router.get("/workspaces/{workspace_id}/rubric", response_model=RubricOut)
def get_rubric(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> RubricOut:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        return _rubric_out(ws)


@router.get("/workspaces/{workspace_id}/setup-status", response_model=SetupStatusOut)
def get_setup_status(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SetupStatusOut:
    with request.app.state.session_factory() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        job = session.scalar(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws.id)
        )
        is_owner = ws.owner_id == user.id
        return SetupStatusOut(
            status=job.status if job else "queued",
            last_error=(job.last_error if job else None) if is_owner else None,
        )
```

> `RubricOut.updated_at` is typed `object` to avoid importing `datetime` in this task (kept F401-clean); FastAPI serializes the `datetime` value to ISO and OpenAPI types it as a string. If the reviewer prefers an explicit `datetime`, that import is fine — but then keep it used.

- [ ] **Step 4: Mount the router** — in `src/call_manager/web/api/__init__.py`, add `rubric` to the import line and include after `settings`:

```python
from call_manager.web.api import calls, files, me, references, rubric, settings, turns, workspaces
```
```python
router.include_router(settings.router)
router.include_router(rubric.router)
```

- [ ] **Step 5: Run to verify it passes** — `uv run pytest tests/test_api_rubric.py -q` → PASS (6 tests).

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/rubric.py src/call_manager/web/api/__init__.py tests/test_api_rubric.py
uv run ruff format src/call_manager/web/api/rubric.py src/call_manager/web/api/__init__.py tests/test_api_rubric.py
git add src/call_manager/web/api/rubric.py src/call_manager/web/api/__init__.py tests/test_api_rubric.py
git commit -m "feat(api): rubric read + setup-status endpoints (owner/member gated)"
```

---

## Task 2: write endpoints — POST activate + PUT rubric + `_apply_rubric`

**Files:**
- Modify: `src/call_manager/web/api/rubric.py` (append models + saver + 2 routes; extend imports)
- Test: `tests/test_api_rubric.py` (append)

**Interfaces:**
- Produces: `POST /api/workspaces/{id}/activate` (owner, body `RubricUpdateIn`) → saves rubric + `setup→active` (409 unless job `waiting_for_rubric_review`) + `workspace_activated` event → `RubricOut`. `PUT /api/workspaces/{id}/rubric` (owner, body `RubricUpdateIn`) → saves only → `RubricOut`. `_apply_rubric(session, ws, user, criteria, funder_notes)` writes `rubric_edited` only on change (actor_id=user.id, full old/new payload).

- [ ] **Step 1: Write the failing test** — append to `tests/test_api_rubric.py`:

```python
def test_put_rubric_saves_and_preserves_subcriteria_and_bumps(monkeypatch, pg_url, tmp_path, pg_session_factory):
    from call_manager.db.models import Workspace, WorkspaceEvent
    from sqlalchemy import func, select

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory, phase="active")
    with pg_session_factory() as s:
        before = s.get(Workspace, ws_id).rubric_updated_at
    body = {
        "criteria": [
            {"name": "Impact!", "description": "d2", "weight": 0.7, "scale_max": 5,
             "subcriteria": [{"name": "reach", "description": "x"}]},
            {"name": "Feasibility", "description": "d", "weight": 0.4, "scale_max": 5},
        ],
        "funder_notes": "notes",
    }
    r = authed_client(app, user_id=owner_id).put(f"/api/workspaces/{ws_id}/rubric", json=body)
    assert r.status_code == 200, r.text
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        assert ws.rubric_criteria[0]["name"] == "Impact!"
        assert ws.rubric_criteria[0]["subcriteria"] == [{"name": "reach", "description": "x"}]
        assert ws.rubric_updated_at > before  # changed → bumped
        assert s.scalar(select(func.count()).select_from(WorkspaceEvent).where(
            WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "rubric_edited")) == 1


def test_put_rubric_unchanged_is_noop(monkeypatch, pg_url, tmp_path, pg_session_factory):
    """Round-tripping the current rubric (incl. subcriteria) writes no event."""
    from call_manager.db.models import Workspace, WorkspaceEvent
    from sqlalchemy import func, select

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory, phase="active")
    with pg_session_factory() as s:
        current = list(s.get(Workspace, ws_id).rubric_criteria)
    r = authed_client(app, user_id=owner_id).put(
        f"/api/workspaces/{ws_id}/rubric", json={"criteria": current, "funder_notes": "notes"}
    )
    assert r.status_code == 200, r.text
    with pg_session_factory() as s:
        assert s.scalar(select(func.count()).select_from(WorkspaceEvent).where(
            WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "rubric_edited")) == 0


def test_put_rubric_member_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory, phase="active")
    r = authed_client(app, user_id=member_id).put(
        f"/api/workspaces/{ws_id}/rubric",
        json={"criteria": [{"name": "A", "description": "", "weight": 1, "scale_max": 5}], "funder_notes": None},
    )
    assert r.status_code == 403


def test_put_rubric_validation_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory, phase="active")
    c = authed_client(app, user_id=owner_id)
    base = {"name": "A", "description": "", "weight": 1.0, "scale_max": 5}
    assert c.put(f"/api/workspaces/{ws_id}/rubric", json={"criteria": [], "funder_notes": None}).status_code == 400
    assert c.put(f"/api/workspaces/{ws_id}/rubric", json={"criteria": [{**base, "name": "  "}], "funder_notes": None}).status_code == 400
    assert c.put(f"/api/workspaces/{ws_id}/rubric", json={"criteria": [{**base, "weight": -1}], "funder_notes": None}).status_code == 400
    assert c.put(f"/api/workspaces/{ws_id}/rubric", json={"criteria": [{**base, "scale_max": 0}], "funder_notes": None}).status_code == 400
    r = c.put(f"/api/workspaces/{ws_id}/rubric", json={"criteria": [], "funder_notes": None})
    assert r.json()["error"]["code"] == "bad_request"


def test_activate_owner_advances_phase_and_audits(monkeypatch, pg_url, tmp_path, pg_session_factory):
    from call_manager.db.models import Workspace, WorkspaceEvent, WorkspaceSetupJob
    from sqlalchemy import select

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)  # setup phase, waiting job
    with pg_session_factory() as s:
        current = list(s.get(Workspace, ws_id).rubric_criteria)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/activate", json={"criteria": current, "funder_notes": "notes"}
    )
    assert r.status_code == 200, r.text
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        assert ws.current_phase == "active"
        job = s.scalar(select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id))
        assert job.status == "done" and job.completed_at is not None
        assert s.scalar(select(WorkspaceEvent).where(
            WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "workspace_activated")) is not None
        # unedited activate wrote NO rubric_edited (subcriteria round-tripped)
        assert s.scalar(select(WorkspaceEvent).where(
            WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "rubric_edited")) is None


def test_activate_when_not_waiting_409(monkeypatch, pg_url, tmp_path, pg_session_factory):
    from call_manager.db.models import Workspace

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory, phase="active", job_status="done")
    with pg_session_factory() as s:
        current = list(s.get(Workspace, ws_id).rubric_criteria)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/activate", json={"criteria": current, "funder_notes": "notes"}
    )
    assert r.status_code == 409


def test_activate_member_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=member_id).post(
        f"/api/workspaces/{ws_id}/activate",
        json={"criteria": [{"name": "A", "description": "", "weight": 1, "scale_max": 5}], "funder_notes": None},
    )
    assert r.status_code == 403
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_api_rubric.py -k "put_rubric or activate" -x -q` → FAIL (405/404).

- [ ] **Step 3: Implement** — in `src/call_manager/web/api/rubric.py`, **extend the imports** (add `HTTPException` to the fastapi line; add `from datetime import UTC, datetime`; add `WorkspaceEvent` to the db.models import) and append:

```python
class CriterionIn(BaseModel):
    # extra="allow" carries subcriteria (and any unknown field) through unchanged.
    model_config = ConfigDict(extra="allow")
    name: str
    description: str
    weight: float
    scale_max: int


class RubricUpdateIn(BaseModel):
    criteria: list[CriterionIn]
    funder_notes: str | None = None


def _validate_criteria(criteria: list[CriterionIn]) -> list[dict]:
    # Semantic validation -> 400 bad_request (parity with the Jinja form). Returns
    # plain dicts that PRESERVE passthrough fields (model_dump keeps extras).
    if not criteria:
        raise HTTPException(status_code=400, detail="rubric must have at least one criterion")
    out: list[dict] = []
    for i, c in enumerate(criteria):
        name = c.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail=f"criterion {i}: name required")
        if c.weight < 0:
            raise HTTPException(status_code=400, detail=f"criterion {i}: weight must be >= 0")
        if not (1 <= c.scale_max <= 10):
            raise HTTPException(status_code=400, detail=f"criterion {i}: scale_max must be 1..10")
        d = c.model_dump()
        d["name"] = name
        out.append(d)
    return out


def _apply_rubric(session, ws, user, criteria: list[dict], funder_notes: str) -> None:
    # Single saver for both activate and PUT. Writes rubric_edited ONLY on real
    # change. NOTE: we set actor_id=user.id (cleaner than the Jinja path, which
    # left actor_id NULL and stored payload['edited_by_user_id']); activity's
    # actor_user_id() prefers actor_id, so this resolves the editor correctly.
    old_c = ws.rubric_criteria
    old_n = ws.rubric_funder_notes or ""
    content_changed = criteria != old_c or funder_notes != old_n
    ws.rubric_criteria = criteria
    ws.rubric_funder_notes = funder_notes
    if content_changed:
        ws.rubric_updated_at = datetime.now(UTC)
        session.add(
            WorkspaceEvent(
                workspace_id=ws.id,
                kind="rubric_edited",
                actor_id=user.id,
                payload={
                    "old_criteria": old_c,
                    "new_criteria": criteria,
                    "old_funder_notes": old_n,
                    "new_funder_notes": funder_notes,
                },
            )
        )


@router.put("/workspaces/{workspace_id}/rubric", response_model=RubricOut)
def update_rubric(
    workspace_id: uuid.UUID,
    body: RubricUpdateIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> RubricOut:
    criteria = _validate_criteria(body.criteria)
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        _apply_rubric(session, ws, user, criteria, body.funder_notes or "")
        session.commit()
        return _rubric_out(ws)


@router.post("/workspaces/{workspace_id}/activate", response_model=RubricOut)
def activate(
    workspace_id: uuid.UUID,
    body: RubricUpdateIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> RubricOut:
    criteria = _validate_criteria(body.criteria)
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        job = session.scalar(
            select(WorkspaceSetupJob)
            .where(WorkspaceSetupJob.workspace_id == ws.id)
            .with_for_update()
        )
        if job is None or job.status != "waiting_for_rubric_review":
            raise HTTPException(status_code=409, detail="workspace is not awaiting rubric review")
        _apply_rubric(session, ws, user, criteria, body.funder_notes or "")
        job.status = "done"
        job.completed_at = datetime.now(UTC)
        ws.current_phase = "active"
        session.add(
            WorkspaceEvent(
                workspace_id=ws.id, kind="workspace_activated", actor_id=user.id, payload={}
            )
        )
        session.commit()
        return _rubric_out(ws)
```

- [ ] **Step 4: Run to verify it passes** — `uv run pytest tests/test_api_rubric.py -q` → PASS (all).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/web/api/rubric.py tests/test_api_rubric.py
uv run ruff format src/call_manager/web/api/rubric.py tests/test_api_rubric.py
git add src/call_manager/web/api/rubric.py tests/test_api_rubric.py
git commit -m "feat(api): rubric activate + PUT (subcriteria passthrough, workspace_activated audit, 409 guard)"
```

---

## Task 3: Activity feed — `workspace_activated` render

**Files:**
- Modify: `src/call_manager/proposal_assist/activity/feed.py` (`_RENDER` :63-85)
- Test: `tests/proposal_assist/activity/test_feed.py` (append)

**Interfaces:**
- Produces: `workspace_activated` renders as category `"rubric"`, summary `"activated the workspace"`. No `ActivityRowOut.category` Literal change (`"rubric"` already in it).

- [ ] **Step 1: Write the failing test** — append to `tests/proposal_assist/activity/test_feed.py` (use its existing `_ev(...)` builder):

```python
def test_workspace_activated_renders_under_rubric_category():
    from call_manager.proposal_assist.activity.feed import build_activity_feed

    ev = _ev("workspace_activated", actor_id=1, payload={})
    rows = build_activity_feed([ev], {1: "owner@ki.se"})
    assert len(rows) == 1
    assert rows[0].category == "rubric"
    assert "activated the workspace" in rows[0].summary
    assert rows[0].actor == "owner@ki.se"
```

> If `_ev`'s signature differs, match the live file; the only requirements are `kind="workspace_activated"`, an `actor_id` resolvable via `actor_names`, and an empty payload.

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/proposal_assist/activity/test_feed.py -k workspace_activated -x -q` → FAIL (unmapped kind → dropped).

- [ ] **Step 3: Implement** — in `src/call_manager/proposal_assist/activity/feed.py`, add to `_RENDER` (next to the rubric entries):

```python
    "workspace_activated": ("rubric", "activated the workspace", None),
```

- [ ] **Step 4: Run to verify it passes** — `uv run pytest tests/proposal_assist/activity/test_feed.py -k workspace_activated -q` → PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/proposal_assist/activity/feed.py tests/proposal_assist/activity/test_feed.py
uv run ruff format src/call_manager/proposal_assist/activity/feed.py tests/proposal_assist/activity/test_feed.py
git add src/call_manager/proposal_assist/activity/feed.py tests/proposal_assist/activity/test_feed.py
git commit -m "feat(activity): render workspace_activated under the rubric category"
```

---

## Task 4: Regenerate the OpenAPI → TypeScript schema

**Files:** Modify `frontend/src/api/schema.gen.ts` (generated).

**Interfaces:** Produces TS types for `RubricOut`/`CriterionOut`/`RubricUpdateIn`/`CriterionIn`/`SetupStatusOut` and the new `/rubric`, `/setup-status`, `/activate` paths. Consumed by Tasks 5-11.

- [ ] **Step 1: Regenerate (mandatory env prefix)**

```bash
EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh
```

- [ ] **Step 2: Verify additive + no routes.gen.ts churn**

```bash
git diff --stat frontend/src/api/schema.gen.ts
git diff frontend/src/api/schema.gen.ts | grep '^-' | grep -v '^---' | grep -v '^-//' || echo "NO removed content lines"
test -z "$(git diff frontend/src/routes.gen.ts)" && echo "routes.gen.ts EMPTY ✓" || echo "routes.gen.ts CHANGED ✗"
```
Expected: `schema.gen.ts` additions only (new paths + schemas; `CriterionOut`/`CriterionIn` carry `[key: string]: unknown` from `additionalProperties: true`). `routes.gen.ts` empty. Any "removed" line must be an openapi-typescript positional relocation — confirm the symbol still exists before proceeding.

- [ ] **Step 3: Type-check + commit**

```bash
cd frontend && pnpm exec tsc --noEmit
cd .. && git add frontend/src/api/schema.gen.ts
git commit -m "chore(api): regen OpenAPI TS types for rubric lifecycle + setup-status"
```

---

## Task 5: Frontend hooks (`api/rubric.ts`)

**Files:**
- Create: `frontend/src/api/rubric.ts`
- Test: `frontend/src/api/rubric.test.tsx`

**Interfaces:**
- Produces: `useRubric(id)` (owner; `["rubric", id]`, `retry:false`), `useSetupStatus(id, enabled)` (`["setup-status", id]`; polls 2500ms while status ∉ {done, failed}), `useActivate(id)` (POST → invalidate cockpit-meta/turns/setup-status), `useUpdateRubric(id)` (PUT → invalidate rubric/scorecard/problems). `RubricUpdate` body type. Consumed by Tasks 6-7, 10-11.

- [ ] **Step 1: Write the failing test** — create `frontend/src/api/rubric.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import * as clientMod from "./client";
import { useActivate, useRubric } from "./rubric";

afterEach(() => vi.restoreAllMocks());

function wrap() {
  const qc = new QueryClient();
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

it("useRubric fetches the rubric", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { criteria: [{ name: "A", description: "", weight: 1, scale_max: 5 }], funder_notes: null, updated_at: "2026-01-01T00:00:00Z" },
    error: undefined,
  } as never);
  const { result } = renderHook(() => useRubric("ws-1"), { wrapper: wrap() });
  await waitFor(() => expect(result.current.data?.criteria[0].name).toBe("A"));
});

it("useActivate POSTs the rubric", async () => {
  const spy = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { criteria: [], funder_notes: null, updated_at: "x" }, error: undefined } as never);
  const { result } = renderHook(() => useActivate("ws-1"), { wrapper: wrap() });
  result.current.mutate({ criteria: [{ name: "A", description: "", weight: 1, scale_max: 5 }], funder_notes: null });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(spy).toHaveBeenCalled();
});
```

- [ ] **Step 2: Run to verify it fails** — (from `frontend/`) `pnpm vitest run src/api/rubric.test.tsx` → FAIL (no exports).

- [ ] **Step 3: Implement** — create `frontend/src/api/rubric.ts`:

```ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { components } from "./schema.gen";
import { api, unwrap } from "./client";

export type RubricCriterion = components["schemas"]["CriterionOut"];
export type RubricUpdate = { criteria: RubricCriterion[]; funder_notes: string | null };

/** GET /api/workspaces/{id}/rubric — OWNER-GATED (non-owner 403; do not retry). */
export function useRubric(id: string) {
  return useQuery({
    queryKey: ["rubric", id],
    enabled: !!id,
    retry: false,
    queryFn: async () =>
      unwrap(await api.GET("/api/workspaces/{workspace_id}/rubric", { params: { path: { workspace_id: id } } })),
  });
}

/** GET /api/workspaces/{id}/setup-status — member-gated; polls until terminal so
 *  viewers auto-advance when the owner activates. */
export function useSetupStatus(id: string, enabled = true) {
  return useQuery({
    queryKey: ["setup-status", id],
    enabled: !!id && enabled,
    refetchInterval: (q) => {
      const s = (q.state.data as { status?: string } | undefined)?.status;
      return s && s !== "done" && s !== "failed" ? 2500 : false;
    },
    queryFn: async () =>
      unwrap(await api.GET("/api/workspaces/{workspace_id}/setup-status", { params: { path: { workspace_id: id } } })),
  });
}

export function useActivate(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: RubricUpdate) =>
      unwrap(await api.POST("/api/workspaces/{workspace_id}/activate", { params: { path: { workspace_id: id } }, body: vars })),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["cockpit-meta", id] });
      qc.invalidateQueries({ queryKey: ["turns", id] });
      qc.invalidateQueries({ queryKey: ["setup-status", id] });
    },
  });
}

export function useUpdateRubric(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: RubricUpdate) =>
      unwrap(await api.PUT("/api/workspaces/{workspace_id}/rubric", { params: { path: { workspace_id: id } }, body: vars })),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["rubric", id] });
      qc.invalidateQueries({ queryKey: ["scorecard", id] });
      qc.invalidateQueries({ queryKey: ["problems", id] });
    },
  });
}
```

> If `api.PUT` isn't exposed by the `openapi-fetch` client, check `api/client.ts`; `openapi-fetch` provides `PUT`. The `RubricUpdate` body must satisfy the generated `RubricUpdateIn` — if tsc complains about extra/missing keys, align the local type to `components["schemas"]["RubricUpdateIn"]`.

- [ ] **Step 4: Run test + type-check** — `pnpm vitest run src/api/rubric.test.tsx` ; `pnpm exec tsc --noEmit` → PASS + exit 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/rubric.ts frontend/src/api/rubric.test.tsx
git commit -m "feat(spa): rubric hooks (useRubric/useSetupStatus/useActivate/useUpdateRubric)"
```

---

## Task 6: `RubricEditor` — shared editable criteria table

**Files:**
- Create: `frontend/src/cockpit/RubricEditor.tsx`
- Test: `frontend/src/cockpit/RubricEditor.test.tsx`

**Interfaces:**
- Consumes: `RubricCriterion` (Task 5).
- Produces: a controlled editor `RubricEditor({ criteria, funderNotes, onChange })` — edits name/description/weight/scale_max + funder notes, shows a live normalized-weight %, and **preserves each criterion's passthrough fields** (spread). The parent owns the save button.

- [ ] **Step 1: Write the failing test** — create `frontend/src/cockpit/RubricEditor.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { RubricEditor } from "./RubricEditor";

const CRITERIA = [
  { name: "Impact", description: "d", weight: 3, scale_max: 5, subcriteria: [{ name: "reach" }] },
  { name: "Feasibility", description: "d", weight: 1, scale_max: 5 },
];

it("shows normalized weight % and preserves passthrough fields on edit", () => {
  const onChange = vi.fn();
  render(<RubricEditor criteria={CRITERIA as never} funderNotes="" onChange={onChange} />);
  // 3 / (3+1) = 75%
  expect(screen.getByText(/75\s*%/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText(/weight for Impact/i), { target: { value: "5" } });
  const next = onChange.mock.calls[0][0];
  expect(next[0].weight).toBe(5);
  expect(next[0].subcriteria).toEqual([{ name: "reach" }]); // passthrough preserved
});
```

> No Mantine here (plain inputs) — `render` is fine; only Mantine-component tests need `renderWithProviders`.

- [ ] **Step 2: Run to verify it fails** — `pnpm vitest run src/cockpit/RubricEditor.test.tsx` → FAIL (module missing).

- [ ] **Step 3: Implement** — create `frontend/src/cockpit/RubricEditor.tsx`:

```tsx
import type { RubricCriterion } from "../api/rubric";

export function RubricEditor({
  criteria,
  funderNotes,
  onChange,
}: {
  criteria: RubricCriterion[];
  funderNotes: string;
  onChange: (next: { criteria: RubricCriterion[]; funderNotes: string }) => void;
}) {
  const total = criteria.reduce((s, c) => s + (Number(c.weight) || 0), 0);
  const patch = (i: number, field: keyof RubricCriterion, value: unknown) =>
    onChange({
      // spread preserves passthrough fields (subcriteria, etc.)
      criteria: criteria.map((c, idx) => (idx === i ? { ...c, [field]: value } : c)),
      funderNotes,
    });

  return (
    <div className="rubric-editor">
      <table>
        <thead>
          <tr><th>Criterion</th><th>Description</th><th>Weight</th><th>%</th><th>Scale max</th></tr>
        </thead>
        <tbody>
          {criteria.map((c, i) => (
            <tr key={i}>
              <td>
                <input aria-label={`Name for ${c.name}`} value={c.name}
                  onChange={(e) => patch(i, "name", e.target.value)} />
              </td>
              <td>
                <textarea aria-label={`Description for ${c.name}`} value={c.description}
                  onChange={(e) => patch(i, "description", e.target.value)} />
              </td>
              <td>
                <input type="number" min={0} step={0.01} aria-label={`Weight for ${c.name}`}
                  value={c.weight} onChange={(e) => patch(i, "weight", Number(e.target.value))} />
              </td>
              <td>{total > 0 ? Math.round((Number(c.weight) / total) * 100) : 0}%</td>
              <td>
                <input type="number" min={1} max={10} aria-label={`Scale max for ${c.name}`}
                  value={c.scale_max} onChange={(e) => patch(i, "scale_max", Number(e.target.value))} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <label className="rubric-notes">
        Funder notes
        <textarea value={funderNotes} onChange={(e) => onChange({ criteria, funderNotes: e.target.value })} />
      </label>
    </div>
  );
}
```

- [ ] **Step 4: Run test + type-check** — `pnpm vitest run src/cockpit/RubricEditor.test.tsx` ; `pnpm exec tsc --noEmit` → PASS + 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/cockpit/RubricEditor.tsx frontend/src/cockpit/RubricEditor.test.tsx
git commit -m "feat(spa): RubricEditor (normalized-% readout, passthrough-preserving)"
```

---

## Task 7: `WorkspaceSetup` — the setup-phase main view

**Files:**
- Create: `frontend/src/cockpit/WorkspaceSetup.tsx`
- Test: `frontend/src/cockpit/WorkspaceSetup.test.tsx`

**Interfaces:**
- Consumes: `useSetupStatus`, `useRubric`, `useActivate` (Task 5); `useCockpitMeta` (`role`); `RubricEditor` (Task 6).
- Produces: `WorkspaceSetup({ id })` — branches on setup-status: `queued`/`running` → progress; `waiting_for_rubric_review` → owner `RubricEditor` + "Confirm & activate", viewer "Waiting…"; `done` → "Opening cockpit…" + invalidates `cockpit-meta`; `failed` → error + `last_error` (owner).

- [ ] **Step 1: Write the failing test** — create `frontend/src/cockpit/WorkspaceSetup.test.tsx`:

```tsx
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders } from "../test-utils";
import { WorkspaceSetup } from "./WorkspaceSetup";

const h = vi.hoisted(() => ({ setup: vi.fn(), rubric: vi.fn(), activate: vi.fn(), meta: vi.fn() }));
vi.mock("../api/rubric", () => ({
  useSetupStatus: () => h.setup(),
  useRubric: () => h.rubric(),
  useActivate: () => ({ mutate: h.activate, isPending: false }),
}));
vi.mock("../api/workspace", () => ({ useCockpitMeta: () => h.meta() }));

beforeEach(() => {
  h.activate.mockReset();
  h.meta.mockReturnValue({ data: { role: "owner" } });
  h.rubric.mockReturnValue({ data: { criteria: [{ name: "A", description: "", weight: 1, scale_max: 5 }], funder_notes: "n", updated_at: "x" } });
});
afterEach(() => vi.restoreAllMocks());

it("owner sees the editor and activates", async () => {
  h.setup.mockReturnValue({ data: { status: "waiting_for_rubric_review", last_error: null } });
  renderWithProviders(<WorkspaceSetup id="ws1" />);
  await waitFor(() => expect(screen.getByLabelText(/name for a/i)).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: /confirm & activate/i }));
  expect(h.activate).toHaveBeenCalled();
});

it("viewer sees a waiting message, no editor", () => {
  h.setup.mockReturnValue({ data: { status: "waiting_for_rubric_review", last_error: null } });
  h.meta.mockReturnValue({ data: { role: "member" } });
  renderWithProviders(<WorkspaceSetup id="ws1" />);
  expect(screen.getByText(/waiting for the owner/i)).toBeInTheDocument();
  expect(screen.queryByLabelText(/name for a/i)).toBeNull();
});

it("failed shows the error to the owner", () => {
  h.setup.mockReturnValue({ data: { status: "failed", last_error: "boom" } });
  renderWithProviders(<WorkspaceSetup id="ws1" />);
  expect(screen.getByText(/boom/)).toBeInTheDocument();
});
```

- [ ] **Step 2: Run to verify it fails** — `pnpm vitest run src/cockpit/WorkspaceSetup.test.tsx` → FAIL (module missing).

- [ ] **Step 3: Implement** — create `frontend/src/cockpit/WorkspaceSetup.tsx`:

```tsx
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { useActivate, useRubric, useSetupStatus, type RubricCriterion } from "../api/rubric";
import { useCockpitMeta } from "../api/workspace";
import { RubricEditor } from "./RubricEditor";

export function WorkspaceSetup({ id }: { id: string }) {
  const qc = useQueryClient();
  const meta = useCockpitMeta(id);
  const isOwner = meta.data?.role === "owner";
  const setup = useSetupStatus(id);
  const status = setup.data?.status;

  // Viewer (and owner) auto-advance: when setup completes, flip the phase branch.
  useEffect(() => {
    if (status === "done") qc.invalidateQueries({ queryKey: ["cockpit-meta", id] });
  }, [status, id, qc]);

  if (status === "failed")
    return (
      <main className="ws-setup">
        <h1>Setup failed</h1>
        {isOwner && setup.data?.last_error && <p className="ws-setup-error">{setup.data.last_error}</p>}
        <p>Delete this workspace and create it again, or contact an administrator.</p>
      </main>
    );

  if (status === "done")
    return <main className="ws-setup"><p aria-busy="true">Opening cockpit…</p></main>;

  if (status === "waiting_for_rubric_review" && isOwner)
    return <OwnerRubricGate id={id} />;

  if (status === "waiting_for_rubric_review")
    return <main className="ws-setup"><p aria-busy="true">Waiting for the owner to review the rubric…</p></main>;

  return <main className="ws-setup"><p aria-busy="true">Setting up your workspace… (usually under a minute)</p></main>;
}

function OwnerRubricGate({ id }: { id: string }) {
  const rubric = useRubric(id);
  const activate = useActivate(id);
  const [criteria, setCriteria] = useState<RubricCriterion[]>([]);
  const [funderNotes, setFunderNotes] = useState("");

  useEffect(() => {
    if (rubric.data) {
      setCriteria(rubric.data.criteria);
      setFunderNotes(rubric.data.funder_notes ?? "");
    }
  }, [rubric.data]);

  if (rubric.isLoading) return <main className="ws-setup"><p>Loading the rubric…</p></main>;

  return (
    <main className="ws-setup">
      <h1>Review the funder criteria</h1>
      <p className="ws-setup-help">Weights are relative — the % shows each criterion's share.</p>
      <RubricEditor
        criteria={criteria}
        funderNotes={funderNotes}
        onChange={(n) => { setCriteria(n.criteria); setFunderNotes(n.funderNotes); }}
      />
      {activate.isError && <p className="ws-setup-error">Could not activate. Check the criteria and retry.</p>}
      <button type="button" disabled={activate.isPending}
        onClick={() => activate.mutate({ criteria, funder_notes: funderNotes || null })}>
        Confirm &amp; activate
      </button>
    </main>
  );
}
```

- [ ] **Step 4: Run test + type-check** — `pnpm vitest run src/cockpit/WorkspaceSetup.test.tsx` ; `pnpm exec tsc --noEmit` → PASS + 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/cockpit/WorkspaceSetup.tsx frontend/src/cockpit/WorkspaceSetup.test.tsx
git commit -m "feat(spa): WorkspaceSetup view (progress/rubric-gate/waiting/done/failed)"
```

---

## Task 8: `Cockpit.tsx` — phase branch

**Files:**
- Modify: `frontend/src/routes/Cockpit.tsx`
- Test: `frontend/src/routes/Cockpit.test.tsx` (append; read the live file first — it mocks transport via `vi.spyOn(clientMod.api, "GET")`; its existing cockpit-meta stub must gain `current_phase`)

**Interfaces:**
- Consumes: `useCockpitMeta` (phase); `WorkspaceSetup` (Task 7).
- Produces: `/cockpit/:id` renders loading until meta resolves, `<WorkspaceSetup>` when `current_phase==="setup"`, else today's cockpit.

- [ ] **Step 1: Write the failing test** — read `Cockpit.test.tsx`, then append a test that stubs cockpit-meta with `current_phase: "setup"` and asserts the setup view shows. Mirror the file's existing `api.GET` mock (add `current_phase` to its cockpit-meta branch so the EXISTING active-phase tests keep rendering the cockpit):

```tsx
it("renders the setup view when the workspace is in setup phase", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/cockpit-meta"))
      return Promise.resolve({ data: { workspace_title: "W", workspace_deadline: null, current_phase: "setup", role: "owner", spec_locked: false, member_count: 1 }, error: undefined });
    if (path.endsWith("/setup-status"))
      return Promise.resolve({ data: { status: "queued", last_error: null }, error: undefined });
    return Promise.resolve({ data: [], error: undefined });
  }) as never);
  renderCockpit(); // mirror the file's existing render helper + route
  expect(await screen.findByText(/setting up your workspace/i)).toBeInTheDocument();
});
```

> Match the live file's render helper/imports. The EXISTING tests assume active phase — ensure their cockpit-meta stub includes `current_phase: "active"` (add it) so they still hit the cockpit branch.

- [ ] **Step 2: Run to verify it fails** — `pnpm vitest run src/routes/Cockpit.test.tsx` → FAIL (no setup branch).

- [ ] **Step 3: Implement** — in `frontend/src/routes/Cockpit.tsx`, add the phase branch at the top of `Cockpit()` (keep the rest unchanged):

```tsx
import { useCockpitMeta } from "../api/workspace";
import { WorkspaceSetup } from "../cockpit/WorkspaceSetup";
// ...
export function Cockpit() {
  const { id = "" } = useParams();
  const meta = useCockpitMeta(id);
  // Branch the MAIN column on phase. The shell sidebar/right-pane are mounted by
  // AppShell from the route handle (router.tsx) and handle setup themselves (Task 10).
  if (meta.isLoading || !meta.data) return <p className="cockpit-loading">Loading…</p>;
  if (meta.data.current_phase === "setup") return <WorkspaceSetup id={id} />;
  // ...existing tab/nav/panel body unchanged below...
```

> Keep the existing `useCockpitNav`/`useState`/panels code exactly as-is, after the branch. The two early returns precede the existing hook calls — to satisfy the Rules of Hooks, place the existing `const { tab, ... } = useCockpitNav()` etc. ABOVE the branch (call all hooks unconditionally), then branch on `meta` for the returned JSX. Read the live file and order accordingly: hooks first, then `if (loading) … if (setup) … return <existing/>`.

- [ ] **Step 4: Run test + type-check + the routes regression** — `pnpm vitest run src/routes/Cockpit.test.tsx` ; `pnpm vitest run src/router.test.tsx` (drift guard) ; `pnpm exec tsc --noEmit` → PASS + 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/routes/Cockpit.tsx frontend/src/routes/Cockpit.test.tsx
git commit -m "feat(spa): cockpit branches to WorkspaceSetup in setup phase"
```

---

## Task 9: Repoint BOTH setup entry points to the cockpit

**Files:**
- Modify: `frontend/src/routes/WorkspaceCreate.tsx` (navigate to `/cockpit/:id`)
- Modify: `frontend/src/routes/WorkspaceList.tsx` (setup row → React `<Link>`)
- Modify: `frontend/src/api/workspace.ts` (comment at `useCreateWorkspace`)
- Test: `frontend/src/routes/WorkspaceCreate.test.tsx` + `frontend/src/routes/WorkspaceList.test.tsx` (read both first)

**Interfaces:**
- Produces: every newly-created workspace AND every setup-phase list row routes to `/cockpit/:id` (React), not the Jinja `/workspaces/:id`.

- [ ] **Step 1: Write the failing tests** — read both test files. For `WorkspaceCreate.test.tsx`, change the post-create assertion from `window.location.assign('/workspaces/<id>')` to a react-router navigation to `/cockpit/<id>` (the file likely spies `window.location.assign` or checks the URL — switch it to assert `/cockpit/<id>`; use the file's existing router/render harness). For `WorkspaceList.test.tsx`, assert a setup-phase row renders a `<Link>`/anchor to `/cockpit/<id>` (not `/workspaces/<id>`).

```tsx
// WorkspaceCreate.test.tsx — replace the old assign('/workspaces/..') expectation:
expect(navigateSpy).toHaveBeenCalledWith("/cockpit/ws-new");  // however the file captures navigation
```
```tsx
// WorkspaceList.test.tsx — setup row links into the cockpit:
const link = screen.getByRole("link", { name: /setting-up workspace title/i });
expect(link).toHaveAttribute("href", "/cockpit/ws-setup");
```

- [ ] **Step 2: Run to verify they fail** — `pnpm vitest run src/routes/WorkspaceCreate.test.tsx src/routes/WorkspaceList.test.tsx` → FAIL.

- [ ] **Step 3: Implement** —

`WorkspaceCreate.tsx`: import `useNavigate`, replace the `window.location.assign` hand-off:

```tsx
import { Link, useNavigate, useSearchParams } from "react-router-dom";
// ...
  const navigate = useNavigate();
// ...inside onSubmit, replace lines 39-40:
    // Setup + rubric now live in the React cockpit (Slice E).
    navigate(`/cockpit/${out.id}`);
```

`WorkspaceList.tsx`: replace the setup-row branch (lines 19-34) so setup rows use `<Link>` too, and drop the stale comment:

```tsx
  // Both setup- and active-phase workspaces open in the React cockpit (Slice E
  // brought the setup + rubric flow in-cockpit). The "Setting up" badge marks setup.
  const setup = ws.current_phase === "setup";
  return (
    <li className={styles.row}>
      <div className={styles.body}>
        <Link className={styles.title} to={`/cockpit/${ws.id}`}>{ws.title}</Link>
        {/* ...unchanged meta block, including {setup && <span className={styles.setup}>Setting up</span>} ... */}
```

`api/workspace.ts`: update the `useCreateWorkspace` doc comment (lines 64-66) — drop "we leave the SPA for the Jinja setup page"; replace with "after create the caller navigates to the in-cockpit setup view; the list refetches on next mount."

- [ ] **Step 4: Run tests + type-check** — `pnpm vitest run src/routes/WorkspaceCreate.test.tsx src/routes/WorkspaceList.test.tsx` ; `pnpm exec tsc --noEmit` → PASS + 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/routes/WorkspaceCreate.tsx frontend/src/routes/WorkspaceList.tsx frontend/src/api/workspace.ts frontend/src/routes/WorkspaceCreate.test.tsx frontend/src/routes/WorkspaceList.test.tsx
git commit -m "feat(spa): route new + setup-phase workspaces to the React cockpit (drop Jinja hand-off)"
```

---

## Task 10: Setup-aware shell panes + owner "Edit rubric" modal

**Files:**
- Modify: `frontend/src/cockpit/chat/CockpitChat.tsx` (setup copy)
- Modify: `frontend/src/cockpit/CockpitSidebar.tsx` (retry-less repo reads + owner/active "Edit rubric" modal)
- Modify: `frontend/src/api/editor.ts`, `frontend/src/api/evaluator.ts` (add `retry:false` to the repo/scorecard reads — read live first)
- Test: `frontend/src/cockpit/CockpitSidebar.test.tsx` (append; read live first — it spies `clientMod.api.GET`)

**Interfaces:**
- Consumes: `useUpdateRubric`, `useRubric` (Task 5); `RubricEditor` (Task 6); `useCockpitMeta` (already in the sidebar).
- Produces: the chat setup message no longer says "legacy workspace"; the sidebar's repo reads don't retry during setup; an owner on an ACTIVE workspace gets an "Edit rubric" control on the Criteria group that opens a `RubricEditor` modal wired to `useUpdateRubric`.

- [ ] **Step 1: Write the failing test** — append to `frontend/src/cockpit/CockpitSidebar.test.tsx`. Mock `../api/rubric` via a `vi.hoisted` object (the write hook), seed cockpit-meta with `role:"owner", current_phase:"active"`, seed `/rubric` GET, click "Edit rubric", edit, save, assert `useUpdateRubric().mutate` called:

```tsx
const rb = vi.hoisted(() => ({ update: vi.fn() }));
vi.mock("../api/rubric", () => ({
  useUpdateRubric: () => ({ mutate: rb.update, isPending: false, isError: false }),
  useRubric: () => ({ data: { criteria: [{ name: "Impact", description: "d", weight: 1, scale_max: 5 }], funder_notes: "", updated_at: "x" }, isLoading: false }),
}));

it("owner can open Edit rubric and save (active phase)", async () => {
  // extend the file's api.GET spy so /cockpit-meta returns role owner + current_phase active
  // (mirror the existing mockImplementation; add current_phase to the meta branch).
  renderSidebar();
  fireEvent.click(await screen.findByRole("button", { name: /criteria/i })); // open the group
  fireEvent.click(await screen.findByRole("button", { name: /edit rubric/i }));
  fireEvent.click(await screen.findByRole("button", { name: /save/i }));
  expect(rb.update).toHaveBeenCalled();
});
```

> Match the live file's `api.GET` mock + `renderSidebar` helper; the Edit-rubric control and modal must only appear for `role==="owner" && current_phase==="active"`. The modal is Mantine → the file already renders through providers (Phase-3 tests); confirm `renderSidebar` supplies `MantineProvider` (it should, via `renderWithProviders`-style setup).

- [ ] **Step 2: Run to verify it fails** — `pnpm vitest run src/cockpit/CockpitSidebar.test.tsx` → FAIL (no Edit-rubric control).

- [ ] **Step 3: Implement** —

`CockpitChat.tsx`: change the `reason === "setup"` copy (lines 86-90) — drop "legacy workspace":

```tsx
            if (reason === "setup")
              return (
                <p className={styles.note}>
                  Finish setup — review the funder rubric above to start drafting.
                </p>
              );
```

`api/editor.ts` / `api/evaluator.ts`: add `retry: false` to the `useQuery` options of `useDraft`, `useSpec` (editor.ts) and `useScorecard` (evaluator.ts); `useVersions` (workspace.ts) likewise — so a setup-phase render (no scorecard/empty repo) doesn't retry 404s 3×. (One-line option each; read each hook and add the key.)

`CockpitSidebar.tsx`: add the owner/active "Edit rubric" control + modal. Near the top, add state + hooks; in the Criteria `SidebarGroup`, render the control when `meta.data?.role === "owner" && meta.data?.current_phase === "active"`; mount the Mantine `Modal` gated on `isOwnerActive && editOpen`:

```tsx
import { Button, Modal } from "@mantine/core";
import { useRubric, useUpdateRubric } from "../api/rubric";
import { RubricEditor } from "./RubricEditor";
// ...inside CockpitSidebar, with the other hooks:
const isOwnerActive = meta.data?.role === "owner" && meta.data?.current_phase === "active";
const [editOpen, setEditOpen] = useState(false);
const rubric = useRubric(id);
const updateRubric = useUpdateRubric(id);
const [draftCriteria, setDraftCriteria] = useState<RubricCriterion[]>([]);
const [draftNotes, setDraftNotes] = useState("");
useEffect(() => {
  if (editOpen && rubric.data) { setDraftCriteria(rubric.data.criteria); setDraftNotes(rubric.data.funder_notes ?? ""); }
}, [editOpen, rubric.data]);
```

In the Criteria `SidebarGroup` body, before the `<ul>`:

```tsx
{isOwnerActive && (
  <button type="button" className={styles.editRubric} onClick={() => setEditOpen(true)}>
    Edit rubric
  </button>
)}
```

And after the Members group (still inside the sidebar root), mount the modal:

```tsx
{isOwnerActive && editOpen && (
  <Modal opened onClose={() => setEditOpen(false)} title="Edit funder criteria" size="xl">
    <RubricEditor
      criteria={draftCriteria}
      funderNotes={draftNotes}
      onChange={(n) => { setDraftCriteria(n.criteria); setDraftNotes(n.funderNotes); }}
    />
    {updateRubric.isError && <p className={styles.note}>Could not save the rubric.</p>}
    <Button disabled={updateRubric.isPending}
      onClick={() => updateRubric.mutate({ criteria: draftCriteria, funder_notes: draftNotes || null }, { onSuccess: () => setEditOpen(false) })}>
      Save
    </Button>
  </Modal>
)}
```

> Import `RubricCriterion` from `../api/rubric`. Add `useEffect` to the React import. Add a small `.editRubric` class to `CockpitSidebar.module.css` if needed. The mount gate (`isOwnerActive && editOpen`) keeps the closed Modal from mounting under provider-less renders (mirrors `TitleBar.tsx:41`).

- [ ] **Step 4: Run tests + type-check + shell regression** — `pnpm vitest run src/cockpit/CockpitSidebar.test.tsx src/cockpit/chat/CockpitChat.test.tsx` ; `pnpm exec tsc --noEmit` → PASS + 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/cockpit/chat/CockpitChat.tsx frontend/src/cockpit/CockpitSidebar.tsx frontend/src/cockpit/CockpitSidebar.module.css frontend/src/api/editor.ts frontend/src/api/evaluator.ts frontend/src/cockpit/CockpitSidebar.test.tsx
git commit -m "feat(spa): setup-aware chat copy + owner Edit-rubric modal + retry-less setup reads"
```

---

## Task 11: Per-slice Jinja teardown + test migration

**Files:**
- Delete: `src/call_manager/proposal_assist/web/rubric_review.py`, `src/call_manager/web/templates/proposal_assist/rubric_review.html`, `src/call_manager/web/templates/proposal_assist/partials/setup_status.html`
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (remove `get_setup_status` route + the `setup_status`/`setup_last_error` computation in `get_workspace_main`), `src/call_manager/proposal_assist/web/__init__.py` (drop the `rubric_review` router registration), `src/call_manager/web/templates/proposal_assist/workspace_main.html` (setup branch → notice)
- Delete: `tests/proposal_assist/test_rubric_review_endpoint.py`
- Modify: `tests/proposal_assist/test_phase0_e2e.py`, `tests/proposal_assist/test_web_workspace_setup_gate.py` (re-point activation to `/api`)

**Interfaces:** Removes the superseded Jinja surface; the React `/api/.../activate` is the sole activation path.

- [ ] **Step 1: Find every reference** — establish what imports the deleted symbols:

```bash
grep -rn "rubric_review\|rubric-review\|setup_status\|setup-status\|setup_last_error" src/call_manager/proposal_assist/web/ src/call_manager/web/templates/ src/call_manager/web/__init__.py src/call_manager/proposal_assist/web/__init__.py
grep -rln "rubric-review\|/rubric-review\|get_setup_status\|setup-status" tests/
```

- [ ] **Step 2: Re-point the closed-loop tests FIRST** — in `tests/proposal_assist/test_phase0_e2e.py` and `tests/proposal_assist/test_web_workspace_setup_gate.py`, replace the activation call `POST /workspaces/{id}/rubric-review` (form) with `POST /api/workspaces/{id}/activate` (JSON body `{"criteria": [...current...], "funder_notes": ...}`). Keep these files' assertions that the workspace reaches `current_phase=="active"` and the setup job is `done`. **Preserve** the `test_web_workspace_setup_gate.py` assertions that cover the RETAINED Jinja behavior (the `workspace_main` setup notice + that a turn-submit is rejected in setup) — update them to the new notice text (Step 4), do not delete them. Run them RED first (routes still exist but you've switched the call) to confirm the new endpoint drives the loop.

- [ ] **Step 3: Delete the Jinja routes/templates** — remove `rubric_review.py`, both templates, the `get_setup_status` handler in `workspaces.py` (lines 652-680), and in `get_workspace_main` drop the `if ws.current_phase == "setup": …setup_status…` block (615-624) + the `setup_status`/`setup_last_error` context keys (646-647). Remove the `rubric_review` router include from `proposal_assist/web/__init__.py`. Delete `tests/proposal_assist/test_rubric_review_endpoint.py`.

- [ ] **Step 4: Convert the `workspace_main.html` setup branch to a notice** — replace the `{% if workspace.current_phase == 'setup' %}{% include "...setup_status.html" %}` (lines 39-40) with a static link (so the page stays reachable but inert for setup, never falling through to the 409'd composer):

```html
    {% if workspace.current_phase == 'setup' %}
    <div class="callout">
      <p><strong>This workspace is set up in the cockpit.</strong></p>
      <p><a role="button" href="/cockpit/{{ workspace.id }}">Open it in the cockpit →</a></p>
    </div>
    {% elif is_owner %}
```

- [ ] **Step 5: Run the migrated + regression tests**

```bash
uv run pytest tests/proposal_assist/test_phase0_e2e.py tests/proposal_assist/test_web_workspace_setup_gate.py -q
uv run pytest tests/proposal_assist/ -k "setup or workspace_main or activate" -q
uv run ruff check src/call_manager/proposal_assist/web/workspaces.py src/call_manager/proposal_assist/web/__init__.py
```
Expected: green; no import errors from the deletions.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(jinja): delete rubric-review + setup-status routes/templates; setup branch → cockpit notice; migrate tests to /api/activate"
```

---

## Final verification (before the whole-branch review)

- [ ] Backend (targeted): `uv run pytest tests/test_api_rubric.py tests/proposal_assist/activity/test_feed.py -q` — green.
- [ ] Migrated Jinja loop: `uv run pytest tests/proposal_assist/test_phase0_e2e.py tests/proposal_assist/test_web_workspace_setup_gate.py -q` — green via `/api/activate`.
- [ ] Regression — no orphan imports of deleted symbols: `grep -rn "rubric_review\|get_setup_status" src/call_manager/ tests/` returns nothing live; `uv run pytest tests/proposal_assist/ -k "activity or setup" -q`.
- [ ] Lint: `uv run ruff check src/call_manager/web/api/ src/call_manager/proposal_assist/activity/feed.py` — clean.
- [ ] Frontend: `cd frontend && pnpm exec tsc --noEmit && pnpm vitest run && pnpm build` — tsc 0, all vitest green, build 0.
- [ ] Schema additive: `git diff --stat <merge-base> -- frontend/src/api/schema.gen.ts` additions only; `frontend/src/routes.gen.ts` EMPTY diff.
- [ ] Alembic head unchanged (no migration). `git diff --stat <merge-base>` shows `rubric_review.py` + the two templates DELETED, `web/api/rubric.py` added.

## Self-review notes (spec coverage)

- Spec §4.2 (4 endpoints + `_apply_rubric` + subcriteria passthrough + ≥1 + 409 + with_for_update) → Tasks 1-2. §4.3 (activation audit + feed) → Tasks 2-3. §7 regen → Task 4. §4.4 hooks → Task 5, RubricEditor → Task 6, WorkspaceSetup (+done/failed/role) → Task 7, Cockpit branch (loading→setup→active) → Task 8, both entry-point repoints → Task 9, chat copy + sidebar setup-reads + Edit-rubric modal → Task 10. §4.5 teardown + §7 test migration → Task 11.
- Permissions: GET rubric/activate/PUT owner; setup-status member (last_error owner-only) — Tasks 1-2 tests. Weight relative + normalized % — Task 6. subcriteria preserved — Tasks 2, 6 tests.
- Out of scope (spec §6): weight-sum enforcement, subcriteria editing, new activation preconditions, viewer-editable rubric, schema change, broader workspace_main deletion.
