# Cockpit Cutover Slice A — Front Door (workspace list + create) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the cockpit the workspace hub — a React workspace list at `/cockpit`, a from-call create flow at `/cockpit/new`, and the nav flip — backed by two additive `/api/workspaces` endpoints.

**Architecture:** Additive `GET`/`POST /api/workspaces` on the existing cockpit JSON router (no schema change; `POST` reuses `create_workspace`). React list + create routes under the existing AppShell; the always-mounted shell chrome is taught the new non-editor cockpit routes; the dashboard `CallRow` gains a "Start drafting" entry point carrying the dashboard's resolved profile. A newly created workspace is in `setup` phase, so create + setup-phase list rows hand off to the Jinja `/workspaces/{id}` setup flow (interim until Slice E).

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 / Pydantic v2 / pytest (testcontainers Postgres) / uv / ruff. React 19 + Vite + react-router-dom 7 + TanStack Query 5 + Mantine v9 + Vitest + Testing Library; openapi-fetch against `schema.gen.ts`.

Source spec: `docs/superpowers/specs/2026-06-24-cockpit-slice-a-front-door-design.md`.

## Global Constraints

- **No DB schema change.** Alembic head stays put; `create_workspace`, `Workspace`, `WorkspaceSetupJob` are untouched.
- **No `routes.gen.ts` manifest change** — `/cockpit` already owns these paths. Do NOT rewrite the manifest to param form (breaks prod serving). The `router.test.tsx` drift-guard *test* DOES change (Task 8).
- **OpenAPI regen requires `EMBEDDING_MODEL=bge-m3`** (the `.env` mxbai default logs a stdout WARNING that corrupts `schema.gen.ts`). The schema diff must be additive only.
- **The api layer must NOT import `proposal_assist.web.workspaces`** — define the constants + `_parse_deadline` locally in `web/api/workspaces.py`.
- **Backend:** run ONLY the targeted `tests/test_api_workspaces.py` ids the plan names — never the whole suite (testcontainers Postgres drops connections under whole-suite load → false infra failures). `uv run ruff check <files>` + `uv run ruff format <files>` on touched files; unused imports (F401) FAIL the gate — import only what a task uses.
- **Frontend:** Node 22 via nvm + pnpm. Type gate is `pnpm exec tsc --noEmit` (there is NO eslint/TS-lint). Tests: `pnpm vitest run <file>`. Build: `pnpm build`.
- **`window.location.assign("/workspaces/{id}")`** after create is a deliberate full-page hand-off to Jinja (that route is not SPA-served) — not a react-router navigate.
- **`cockpitIdFromPath` must use the reserved-segment guard, NOT a UUID regex** — existing tests mount the cockpit with non-UUID ids (`/cockpit/x`, `/cockpit/ws-1`) and must stay green.

---

### Task 1: Backend `GET /api/workspaces` (list)

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py` (add imports near the top; append the model + constants + route at the end of the file)
- Test: `tests/test_api_workspaces.py` (append)

**Interfaces:**
- Produces: `GET /api/workspaces?status=<opt>` → `list[WorkspaceListItemOut]` where `WorkspaceListItemOut = {id: str, title: str, status: str, current_phase: str, deadline: datetime|None, updated_at: datetime, linked_call_title: str|None, is_owner: bool}`.

- [ ] **Step 1: Write the failing tests**

Add to the END of `tests/test_api_workspaces.py`. First extend the existing model import line:

```python
from call_manager.db.models import (
    ArtifactVersion,
    Call,
    Reference,
    Source,
    User,
    Workspace,
    WorkspaceViewer,
)
```

(That replaces the current `from call_manager.db.models import ArtifactVersion, Reference, User, Workspace, WorkspaceViewer` line — adds `Call` and `Source`.)

Then append the helper + tests:

```python
def _mk_source_call(s, *, slug, title):
    """Seed a Source + Call (Call requires source_id/external_id/url/title)."""
    src = Source(name=slug, slug=slug)
    s.add(src)
    s.flush()
    call = Call(
        source_id=src.id,
        external_id=f"ext-{slug}",
        url=f"https://x.test/{slug}",
        title=title,
    )
    s.add(call)
    s.flush()
    return call


def test_list_workspaces_owned_and_viewer_only(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        other = User(status="active", email="other@ki.se")
        s.add_all([owner, other])
        s.flush()
        call = _mk_source_call(s, slug="vinnova", title="Linked Call")
        w_own = Workspace(
            title="Mine", owner_id=owner.id, current_phase="active", call_id=call.id
        )
        w_shared = Workspace(title="Shared", owner_id=other.id, current_phase="setup")
        w_hidden = Workspace(title="Hidden", owner_id=other.id)
        w_deleted = Workspace(
            title="Gone", owner_id=owner.id, soft_deleted_at=datetime.now(UTC)
        )
        s.add_all([w_own, w_shared, w_hidden, w_deleted])
        s.flush()
        s.add(WorkspaceViewer(workspace_id=w_shared.id, user_id=owner.id))
        s.commit()
        owner_id = owner.id
        own_id, shared_id = str(w_own.id), str(w_shared.id)
    c = _client(app, owner_id)
    r = c.get("/api/workspaces")
    assert r.status_code == 200
    items = r.json()
    ids = [it["id"] for it in items]
    assert own_id in ids and shared_id in ids
    assert all(it["title"] != "Hidden" for it in items)  # not visible
    assert all(it["title"] != "Gone" for it in items)  # soft-deleted excluded
    mine = next(it for it in items if it["id"] == own_id)
    assert mine["is_owner"] is True
    assert mine["linked_call_title"] == "Linked Call"
    assert mine["current_phase"] == "active"
    shared = next(it for it in items if it["id"] == shared_id)
    assert shared["is_owner"] is False
    assert shared["linked_call_title"] is None


def test_list_workspaces_status_filter_and_validation(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        s.add_all(
            [
                Workspace(title="Draft one", owner_id=owner.id, status="drafting"),
                Workspace(title="Submitted one", owner_id=owner.id, status="submitted"),
            ]
        )
        s.commit()
        owner_id = owner.id
    c = _client(app, owner_id)
    r = c.get("/api/workspaces?status=submitted")
    assert r.status_code == 200
    assert [it["title"] for it in r.json()] == ["Submitted one"]
    assert c.get("/api/workspaces?status=bogus").status_code == 400
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_api_workspaces.py::test_list_workspaces_owned_and_viewer_only tests/test_api_workspaces.py::test_list_workspaces_status_filter_and_validation -v`
Expected: FAIL — 404 (route not defined yet) / assertion errors.

- [ ] **Step 3: Add imports**

In `src/call_manager/web/api/workspaces.py`, change the sqlalchemy import:

```python
from sqlalchemy import func, or_, select
```

and add `Call` and `Workspace` to the `call_manager.db.models` import block (keep it alphabetized):

```python
from call_manager.db.models import (
    ArtifactVersion,
    Call,
    Reference,
    ReferenceChunk,
    SpecLockEvent,
    User,
    Workspace,
    WorkspaceEvent,
    WorkspaceViewer,
)
```

- [ ] **Step 4: Append the model, constants, and route**

Append at the END of `src/call_manager/web/api/workspaces.py`:

```python
# --- Slice A: workspace collection (list + create) -------------------------
# Mirrors the ck_workspaces_status CHECK constraint (db/models.py) — the single
# source of truth for the ?status allowlist.
_WORKSPACE_STATUSES = frozenset(
    {"drafting", "submitted", "awarded", "rejected", "abandoned"}
)
_WORKSPACE_LIST_LIMIT = 200


class WorkspaceListItemOut(BaseModel):
    id: str
    title: str
    status: str  # drafting|submitted|awarded|rejected|abandoned
    current_phase: str  # "setup" | "active"
    deadline: datetime | None
    updated_at: datetime
    linked_call_title: str | None  # None when call_id is null (call deleted)
    is_owner: bool


@router.get("/workspaces", response_model=list[WorkspaceListItemOut])
def list_workspaces_api(
    request: Request,
    status: str | None = None,
    user: User = Depends(get_current_user),  # noqa: B008
) -> list[WorkspaceListItemOut]:
    """Workspaces visible to the user (owned OR viewer-of), not soft-deleted,
    most-recently-updated first, capped. Optional ?status filter (allowlisted).
    Mirrors the Jinja list_workspaces query; powers the React /cockpit list."""
    if status is not None and status not in _WORKSPACE_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status filter")
    sf = request.app.state.session_factory
    with sf() as session:
        viewer_ids = session.scalars(
            select(WorkspaceViewer.workspace_id).where(
                WorkspaceViewer.user_id == user.id
            )
        ).all()
        q = (
            select(Workspace)
            .where(
                Workspace.soft_deleted_at.is_(None),
                or_(Workspace.owner_id == user.id, Workspace.id.in_(viewer_ids)),
            )
            .order_by(Workspace.updated_at.desc())
            .limit(_WORKSPACE_LIST_LIMIT)
        )
        if status:
            q = q.where(Workspace.status == status)
        rows = list(session.scalars(q))
        # Batch the linked-call titles in ONE query (no N+1); a row whose call was
        # deleted (call_id SET NULL) simply has no title.
        call_ids = {w.call_id for w in rows if w.call_id is not None}
        titles = (
            dict(
                session.execute(
                    select(Call.id, Call.title).where(Call.id.in_(call_ids))
                ).all()
            )
            if call_ids
            else {}
        )
        return [
            WorkspaceListItemOut(
                id=str(w.id),
                title=w.title,
                status=w.status,
                current_phase=w.current_phase,
                deadline=w.deadline,
                updated_at=w.updated_at,
                linked_call_title=titles.get(w.call_id),
                is_owner=(w.owner_id == user.id),
            )
            for w in rows
        ]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_api_workspaces.py::test_list_workspaces_owned_and_viewer_only tests/test_api_workspaces.py::test_list_workspaces_status_filter_and_validation -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Lint + format**

Run: `uv run ruff check src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py && uv run ruff format src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
git commit -m "feat(api): GET /api/workspaces list for the React front door"
```

---

### Task 2: Backend `POST /api/workspaces` (from-call create)

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py` (add `Profile` + `create_workspace` imports; append models + parser + route)
- Test: `tests/test_api_workspaces.py` (append)

**Interfaces:**
- Consumes: `_mk_source_call` (Task 1, test helper); `create_workspace(*, session, owner_id, title, call_id=None, profile_id=None) -> Workspace` from `call_manager.workspaces`.
- Produces: `POST /api/workspaces` body `WorkspaceCreateIn = {call_id: int, title: str, deadline: str|None, profile_id: int|None}` → **201** `WorkspaceCreatedOut = {id: str}`.

- [ ] **Step 1: Write the failing tests**

Extend the test imports — add `Profile`, `ProfileKind`, `WorkspaceSetupJob` to the `db.models` import, and add a `select` import:

```python
from sqlalchemy import select

from call_manager.db.models import (
    ArtifactVersion,
    Call,
    Profile,
    ProfileKind,
    Reference,
    Source,
    User,
    Workspace,
    WorkspaceSetupJob,
    WorkspaceViewer,
)
```

Append the tests:

```python
def test_create_workspace_from_call(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        call = _mk_source_call(s, slug="vinnova", title="Linked Call")
        s.commit()
        owner_id, call_id = owner.id, call.id
    c = _client(app, owner_id)
    r = c.post("/api/workspaces", json={"call_id": call_id, "title": "  My draft  "})
    assert r.status_code == 201
    new_id = r.json()["id"]
    with pg_session_factory() as s:
        ws = s.get(Workspace, uuid.UUID(new_id))
        assert ws.title == "My draft"  # trimmed
        assert ws.current_phase == "setup"
        assert ws.call_id == call_id
        jobs = s.scalars(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws.id)
        ).all()
        assert len(jobs) == 1  # Phase-0 enqueued


def test_create_workspace_validation(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        call = _mk_source_call(s, slug="v", title="C")
        s.commit()
        owner_id, call_id = owner.id, call.id
    c = _client(app, owner_id)
    assert (
        c.post("/api/workspaces", json={"call_id": call_id, "title": "   "}).status_code
        == 400
    )
    assert (
        c.post(
            "/api/workspaces", json={"call_id": call_id, "title": "x" * 501}
        ).status_code
        == 400
    )
    assert (
        c.post("/api/workspaces", json={"call_id": 999999, "title": "ok"}).status_code
        == 400
    )
    assert (
        c.post(
            "/api/workspaces",
            json={"call_id": call_id, "title": "ok", "deadline": "not-a-date"},
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/api/workspaces",
            json={"call_id": call_id, "title": "ok", "profile_id": 999999},
        ).status_code
        == 400
    )


def test_create_workspace_stores_profile_and_deadline(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        call = _mk_source_call(s, slug="v", title="C")
        prof = Profile(
            name="Grp",
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            description="d",
            structured={},
            created_by=owner.id,
        )
        s.add(prof)
        s.commit()
        owner_id, call_id, prof_id = owner.id, call.id, prof.id
    c = _client(app, owner_id)
    r = c.post(
        "/api/workspaces",
        json={
            "call_id": call_id,
            "title": "ok",
            "profile_id": prof_id,
            "deadline": "2099-07-12T00:00",
        },
    )
    assert r.status_code == 201
    with pg_session_factory() as s:
        ws = s.get(Workspace, uuid.UUID(r.json()["id"]))
        assert ws.profile_id == prof_id
        assert ws.deadline is not None and ws.deadline.year == 2099
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_api_workspaces.py::test_create_workspace_from_call tests/test_api_workspaces.py::test_create_workspace_validation tests/test_api_workspaces.py::test_create_workspace_stores_profile_and_deadline -v`
Expected: FAIL — 405/404 (POST route not defined).

- [ ] **Step 3: Add imports**

In `src/call_manager/web/api/workspaces.py`, add `Profile` to the `db.models` import (keep alphabetized: `..., Call, Profile, Reference, ...`), and add below the existing `from call_manager.workspaces ...`-style imports (near the other top-level imports):

```python
from call_manager.workspaces import create_workspace
```

(`datetime` is already imported via `from datetime import UTC, datetime`.)

- [ ] **Step 4: Append the models, parser, and route**

Append at the END of `src/call_manager/web/api/workspaces.py` (after Task 1's list route):

```python
_MAX_WORKSPACE_TITLE_LEN = 500


class WorkspaceCreateIn(BaseModel):
    call_id: int
    title: str
    deadline: str | None = None  # ISO-8601; parsed + validated server-side
    profile_id: int | None = None  # the dashboard's resolved effective profile


class WorkspaceCreatedOut(BaseModel):
    id: str


def _parse_deadline(raw: str | None) -> datetime | None:
    """Parse an optional ISO-8601 deadline; 400 on a malformed string (mirrors the
    Jinja _parse_deadline_form, without the Form coupling)."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError as e:
        raise HTTPException(
            status_code=400, detail="invalid deadline (expected ISO-8601)"
        ) from e


@router.post("/workspaces", response_model=WorkspaceCreatedOut, status_code=201)
def create_workspace_api(
    body: WorkspaceCreateIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> WorkspaceCreatedOut:
    """From-call workspace creation. Validates first (clean 400, not a FK 500),
    then reuses create_workspace (sets current_phase='setup' + enqueues Phase-0).
    Returns the new id; the SPA hands off to the Jinja setup flow."""
    title = body.title.strip()
    if not title or len(title) > _MAX_WORKSPACE_TITLE_LEN:
        raise HTTPException(status_code=400, detail="invalid title")
    deadline = _parse_deadline(body.deadline)
    sf = request.app.state.session_factory
    with sf() as session:
        if session.get(Call, body.call_id) is None:
            raise HTTPException(status_code=400, detail=f"No such call: {body.call_id}")
        if body.profile_id is not None and session.get(Profile, body.profile_id) is None:
            raise HTTPException(
                status_code=400, detail=f"No such profile: {body.profile_id}"
            )
        ws = create_workspace(
            session=session,
            owner_id=user.id,
            title=title,
            call_id=body.call_id,
            profile_id=body.profile_id,
        )
        if deadline:
            ws.deadline = deadline
        session.commit()
        new_id = str(ws.id)
    return WorkspaceCreatedOut(id=new_id)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_api_workspaces.py::test_create_workspace_from_call tests/test_api_workspaces.py::test_create_workspace_validation tests/test_api_workspaces.py::test_create_workspace_stores_profile_and_deadline -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Lint + format**

Run: `uv run ruff check src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py && uv run ruff format src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
git commit -m "feat(api): POST /api/workspaces from-call create (reuses create_workspace)"
```

---

### Task 3: Regenerate the OpenAPI TypeScript types

**Files:**
- Modify: `frontend/src/api/schema.gen.ts` (generated — do not hand-edit)

**Interfaces:**
- Produces: `paths["/api/workspaces"]` (GET + POST) and `components["schemas"]["WorkspaceListItemOut"|"WorkspaceCreateIn"|"WorkspaceCreatedOut"]`, consumed by Tasks 4–6.

- [ ] **Step 1: Regenerate the schema**

From the worktree ROOT (NOT `frontend/`):

```bash
EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh
```

The `EMBEDDING_MODEL=bge-m3` prefix is MANDATORY — the `.env` mxbai default logs a stdout WARNING that corrupts the generated file.

- [ ] **Step 2: Verify the diff is additive only**

Run: `git diff --stat frontend/src/api/schema.gen.ts && git diff frontend/src/api/schema.gen.ts | grep -E '^\-' | grep -v '^---'`
Expected: only additions (the second command prints nothing but the `---` file header line is filtered). The added paths include `"/api/workspaces"` with `get` + `post`, and the three new schemas. No removed/changed existing lines.

- [ ] **Step 3: Verify the frontend still type-checks**

```bash
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; nvm use 22
cd frontend && pnpm exec tsc --noEmit
```
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/schema.gen.ts
git commit -m "chore(api): regenerate OpenAPI types for /api/workspaces"
```

---

### Task 4: Frontend API hooks (`useWorkspaces`, `useCreateWorkspace`)

**Files:**
- Modify: `frontend/src/api/workspace.ts` (append two hooks)
- Test: `frontend/src/api/workspace.test.tsx` (append)

**Interfaces:**
- Consumes: generated `/api/workspaces` paths (Task 3); `api`, `unwrap` from `./client`.
- Produces: `useWorkspaces(status: string | null)` (query key `["workspaces", status ?? ""]`); `useCreateWorkspace()` (mutation; `mutateAsync({call_id, title, deadline?, profile_id?}) -> {id: string}`).

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/api/workspace.test.tsx`. First extend the imports at the top of the file:

```tsx
import { renderHook, render, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";

import { useCockpitMeta, useWorkspaces, useCreateWorkspace } from "./workspace";
```

(The existing file already imports `QueryClient, QueryClientProvider`, `clientMod`, `vi`, `expect`, `it`. Keep `render` if it was already imported — merge, do not duplicate.)

Append the tests:

```tsx
function wrap() {
  const qc = new QueryClient();
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

it("useWorkspaces fetches the list with an empty query when status is null", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: [
      {
        id: "w1",
        title: "A",
        status: "drafting",
        current_phase: "active",
        deadline: null,
        updated_at: "2026-06-24T00:00:00Z",
        linked_call_title: "C",
        is_owner: true,
      },
    ],
    error: undefined,
  } as never);
  const { result } = renderHook(() => useWorkspaces(null), { wrapper: wrap() });
  await waitFor(() => expect(result.current.data?.[0]?.title).toBe("A"));
  expect(get).toHaveBeenCalledWith("/api/workspaces", { params: { query: {} } });
});

it("useWorkspaces passes the status filter as a query param", async () => {
  const get = vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: [], error: undefined } as never);
  const { result } = renderHook(() => useWorkspaces("submitted"), { wrapper: wrap() });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(get).toHaveBeenCalledWith("/api/workspaces", {
    params: { query: { status: "submitted" } },
  });
});

it("useCreateWorkspace posts the body and returns the new id", async () => {
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { id: "new-1" }, error: undefined } as never);
  const { result } = renderHook(() => useCreateWorkspace(), { wrapper: wrap() });
  const out = await result.current.mutateAsync({ call_id: 3, title: "T", profile_id: 7 });
  expect(out.id).toBe("new-1");
  expect(post).toHaveBeenCalledWith("/api/workspaces", {
    body: { call_id: 3, title: "T", profile_id: 7 },
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; nvm use 22; cd frontend
pnpm vitest run src/api/workspace.test.tsx
```
Expected: FAIL — `useWorkspaces`/`useCreateWorkspace` are not exported.

- [ ] **Step 3: Implement the hooks**

In `frontend/src/api/workspace.ts`, change the react-query import to include `useMutation`:

```ts
import { useMutation, useQuery } from "@tanstack/react-query";
```

and append:

```ts
/** GET /api/workspaces — the workspace list for the React front door (/cockpit).
 *  status: null = all; otherwise the lifecycle filter. */
export function useWorkspaces(status: string | null) {
  return useQuery({
    queryKey: ["workspaces", status ?? ""],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces", {
          params: { query: status ? { status } : {} },
        }),
      ),
  });
}

/** POST /api/workspaces — from-call create. Returns { id }. No invalidation:
 *  after create we leave the SPA for the Jinja setup page; the list refetches on
 *  next mount. */
export function useCreateWorkspace() {
  return useMutation({
    mutationFn: async (vars: {
      call_id: number;
      title: string;
      deadline?: string | null;
      profile_id?: number | null;
    }) => unwrap(await api.POST("/api/workspaces", { body: vars })),
  });
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pnpm vitest run src/api/workspace.test.tsx`
Expected: PASS.

- [ ] **Step 5: Type-check + commit**

```bash
pnpm exec tsc --noEmit
git add frontend/src/api/workspace.ts frontend/src/api/workspace.test.tsx
git commit -m "feat(spa): useWorkspaces + useCreateWorkspace hooks"
```

---

### Task 5: `WorkspaceList` + `WorkspacesFilters` (the list route components)

**Files:**
- Create: `frontend/src/routes/WorkspaceList.tsx`, `frontend/src/routes/WorkspaceList.module.css`
- Create: `frontend/src/routes/WorkspacesFilters.tsx`, `frontend/src/routes/WorkspacesFilters.module.css`
- Test: `frontend/src/routes/WorkspaceList.test.tsx`, `frontend/src/routes/WorkspacesFilters.test.tsx`

**Interfaces:**
- Consumes: `useWorkspaces` (Task 4); `Deadline` from `./Deadline`; `components["schemas"]["WorkspaceListItemOut"]`.
- Produces: `WorkspaceList`, `WorkspacesFilters` (named exports), wired into the router in Task 8.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/routes/WorkspaceList.test.tsx`:

```tsx
import { screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { WorkspaceList } from "./WorkspaceList";

function wsItem(over: Record<string, unknown> = {}) {
  return {
    id: "ws-1",
    title: "AMR grant",
    status: "drafting",
    current_phase: "active",
    deadline: null,
    updated_at: "2026-06-24T00:00:00Z",
    linked_call_title: "Vinnova call",
    is_owner: true,
    ...over,
  };
}

it("an active workspace links to the React cockpit", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: [wsItem()], error: undefined } as never);
  renderWithProviders(<WorkspaceList />, { route: "/cockpit" });
  const link = await screen.findByRole("link", { name: "AMR grant" });
  expect(link).toHaveAttribute("href", "/cockpit/ws-1");
});

it("a setup workspace links to the Jinja setup flow and is marked 'Setting up'", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: [wsItem({ id: "ws-2", title: "New one", current_phase: "setup", is_owner: false, linked_call_title: null })],
    error: undefined,
  } as never);
  renderWithProviders(<WorkspaceList />, { route: "/cockpit" });
  const link = await screen.findByRole("link", { name: "New one" });
  expect(link).toHaveAttribute("href", "/workspaces/ws-2");
  expect(screen.getByText("Setting up")).toBeInTheDocument();
  expect(screen.getByText("Shared")).toBeInTheDocument(); // viewer-of badge
});

it("shows an empty state that points back to the dashboard", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: [], error: undefined } as never);
  renderWithProviders(<WorkspaceList />, { route: "/cockpit" });
  expect(await screen.findByText(/pick a call to start drafting/i)).toBeInTheDocument();
});

it("reads the ?status filter from the URL into the query", async () => {
  const get = vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: [], error: undefined } as never);
  renderWithProviders(<WorkspaceList />, { route: "/cockpit?status=submitted" });
  await waitFor(() =>
    expect(get).toHaveBeenCalledWith("/api/workspaces", { params: { query: { status: "submitted" } } }),
  );
});
```

Create `frontend/src/routes/WorkspacesFilters.test.tsx`:

```tsx
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation } from "react-router-dom";
import { expect, it } from "vitest";

import { renderWithProviders } from "../test-utils";
import { WorkspacesFilters } from "./WorkspacesFilters";

function LocationProbe() {
  const loc = useLocation();
  return <output data-testid="search">{loc.search}</output>;
}

it("selecting a status writes ?status to the URL", async () => {
  const user = userEvent.setup({ pointerEventsCheck: 0 });
  renderWithProviders(
    <>
      <WorkspacesFilters />
      <LocationProbe />
    </>,
    { route: "/cockpit" },
  );
  await user.click(screen.getByLabelText("Status", { selector: "input" }));
  await user.click(await screen.findByRole("option", { name: "Submitted", hidden: true }));
  await waitFor(() =>
    expect(screen.getByTestId("search").textContent).toContain("status=submitted"),
  );
});
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; nvm use 22; cd frontend
pnpm vitest run src/routes/WorkspaceList.test.tsx src/routes/WorkspacesFilters.test.tsx
```
Expected: FAIL — modules don't exist.

- [ ] **Step 3: Implement `WorkspaceList`**

Create `frontend/src/routes/WorkspaceList.tsx`:

```tsx
import { Link, useSearchParams } from "react-router-dom";

import type { components } from "../api/schema.gen";
import { useWorkspaces } from "../api/workspace";
import { Deadline } from "./Deadline";
import styles from "./WorkspaceList.module.css";

type WsItem = components["schemas"]["WorkspaceListItemOut"];

function WorkspaceRow({ ws }: { ws: WsItem }) {
  // Setup-phase workspaces aren't cockpit-ready (no repo yet); the setup + rubric
  // flow is still Jinja (Slice E) — full-page hand-off via <a>. Active workspaces
  // open the React cockpit.
  const setup = ws.current_phase === "setup";
  return (
    <li className={styles.row}>
      <div className={styles.body}>
        {setup ? (
          <a className={styles.title} href={`/workspaces/${ws.id}`}>
            {ws.title}
          </a>
        ) : (
          <Link className={styles.title} to={`/cockpit/${ws.id}`}>
            {ws.title}
          </Link>
        )}
        <div className={styles.meta}>
          {ws.linked_call_title && <span>{ws.linked_call_title}</span>}
          {ws.linked_call_title && ws.deadline && <span aria-hidden> · </span>}
          {ws.deadline && <Deadline iso={ws.deadline} />}
          {!ws.is_owner && <span className={styles.badge}>Shared</span>}
          {setup && <span className={styles.setup}>Setting up</span>}
        </div>
      </div>
    </li>
  );
}

export function WorkspaceList() {
  const [sp] = useSearchParams();
  const status = sp.get("status");
  const { data, isLoading, isError } = useWorkspaces(status);

  if (isLoading) return <p>Loading…</p>;
  if (isError) return <p role="alert">Couldn&apos;t load workspaces.</p>;

  const items = data ?? [];
  return (
    <main>
      <div className={styles.header}>
        <h1 className={styles.h1}>Workspaces</h1>
        <Link className={styles.newBtn} to="/">
          New workspace
        </Link>
      </div>
      {items.length === 0 ? (
        <p className={styles.empty}>
          No workspaces yet — <Link to="/">pick a call to start drafting</Link>.
        </p>
      ) : (
        <ul className={styles.list}>
          {items.map((ws) => (
            <WorkspaceRow key={ws.id} ws={ws} />
          ))}
        </ul>
      )}
    </main>
  );
}
```

Create `frontend/src/routes/WorkspaceList.module.css`:

```css
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}
.h1 {
  margin: 0;
  font-size: 1.1rem;
}
.newBtn {
  color: var(--app-link);
  text-decoration: none;
  font-weight: 500;
  font-size: 0.9rem;
}
.newBtn:hover {
  text-decoration: underline;
}
.list {
  margin: 0.5rem 0;
  padding: 0;
}
.row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.5rem 0.25rem;
  border-bottom: 1px solid var(--app-border);
  list-style: none;
}
.body {
  min-width: 0;
}
.title {
  color: var(--app-link);
  text-decoration: none;
  font-weight: 500;
}
.title:hover {
  text-decoration: underline;
}
.meta {
  margin-top: 0.15rem;
  color: var(--app-muted);
  font-size: 0.85rem;
  display: flex;
  align-items: center;
  gap: 0.4rem;
}
.badge,
.setup {
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--app-muted);
  border: 1px solid var(--app-border);
  border-radius: 4px;
  padding: 0 0.3rem;
}
.empty {
  color: var(--app-muted);
  margin: 1rem 0.25rem;
}
```

- [ ] **Step 4: Implement `WorkspacesFilters`**

Create `frontend/src/routes/WorkspacesFilters.tsx`:

```tsx
import { Select } from "@mantine/core";
import { useSearchParams } from "react-router-dom";

import styles from "./WorkspacesFilters.module.css";

const STATUS_OPTIONS = [
  { value: "", label: "All statuses" },
  { value: "drafting", label: "Drafting" },
  { value: "submitted", label: "Submitted" },
  { value: "awarded", label: "Awarded" },
  { value: "rejected", label: "Rejected" },
  { value: "abandoned", label: "Abandoned" },
];

// Sidebar filter declared on the /cockpit route via `handle.Sidebar`. Binds the
// status filter to the URL (?status); the list reads the same URL, so the two
// stay in sync with no props across the shell boundary.
export function WorkspacesFilters() {
  const [sp, setSp] = useSearchParams();
  const status = sp.get("status") ?? "";
  return (
    <div className={styles.filters}>
      <h2 className={styles.heading}>Filters</h2>
      <Select
        label="Status"
        data={STATUS_OPTIONS}
        value={status}
        allowDeselect={false}
        onChange={(v) =>
          setSp((prev) => {
            const next = new URLSearchParams(prev);
            if (v) next.set("status", v);
            else next.delete("status");
            return next;
          })
        }
      />
    </div>
  );
}
```

Create `frontend/src/routes/WorkspacesFilters.module.css`:

```css
.filters {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  padding: 0.75rem;
}
.heading {
  margin: 0;
  font-size: 0.7rem;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--app-muted);
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pnpm vitest run src/routes/WorkspaceList.test.tsx src/routes/WorkspacesFilters.test.tsx`
Expected: PASS.

- [ ] **Step 6: Type-check + commit**

```bash
pnpm exec tsc --noEmit
git add frontend/src/routes/WorkspaceList.tsx frontend/src/routes/WorkspaceList.module.css frontend/src/routes/WorkspacesFilters.tsx frontend/src/routes/WorkspacesFilters.module.css frontend/src/routes/WorkspaceList.test.tsx frontend/src/routes/WorkspacesFilters.test.tsx
git commit -m "feat(spa): WorkspaceList + WorkspacesFilters for the /cockpit front door"
```

---

### Task 6: `WorkspaceCreate` (the `/cockpit/new` from-call form)

**Files:**
- Create: `frontend/src/routes/WorkspaceCreate.tsx`, `frontend/src/routes/WorkspaceCreate.module.css`
- Test: `frontend/src/routes/WorkspaceCreate.test.tsx`

**Interfaces:**
- Consumes: `useCreateWorkspace` (Task 4). Reads `call_id`, `call_title`, `profile_id` from the query string.
- Produces: `WorkspaceCreate` (named export), wired into the router in Task 8.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/routes/WorkspaceCreate.test.tsx`:

```tsx
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { WorkspaceCreate } from "./WorkspaceCreate";

afterEach(() => vi.restoreAllMocks());

function stubLocation() {
  const orig = window.location;
  const assign = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...orig, assign },
  });
  return { assign, restore: () => Object.defineProperty(window, "location", { configurable: true, value: orig }) };
}

it("prefills the title from ?call_title and creates, then hands off to Jinja setup", async () => {
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { id: "new-9" }, error: undefined } as never);
  const loc = stubLocation();
  try {
    const user = userEvent.setup();
    renderWithProviders(<WorkspaceCreate />, {
      route: "/cockpit/new?call_id=3&call_title=Grant%20X&profile_id=7",
    });
    const title = screen.getByLabelText("Title") as HTMLInputElement;
    expect(title.value).toBe("Grant X");
    await user.click(screen.getByRole("button", { name: "Start drafting" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/api/workspaces",
        expect.objectContaining({ body: expect.objectContaining({ call_id: 3, title: "Grant X", profile_id: 7 }) }),
      ),
    );
    await waitFor(() => expect(loc.assign).toHaveBeenCalledWith("/workspaces/new-9"));
  } finally {
    loc.restore();
  }
});

it("shows a guard (not a form) when call_id is missing", () => {
  renderWithProviders(<WorkspaceCreate />, { route: "/cockpit/new" });
  expect(screen.getByText(/pick a call from the dashboard/i)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Start drafting" })).toBeNull();
});
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; nvm use 22; cd frontend
pnpm vitest run src/routes/WorkspaceCreate.test.tsx
```
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `WorkspaceCreate`**

Create `frontend/src/routes/WorkspaceCreate.tsx`:

```tsx
import { type FormEvent, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useCreateWorkspace } from "../api/workspace";
import styles from "./WorkspaceCreate.module.css";

export function WorkspaceCreate() {
  const [sp] = useSearchParams();
  const callIdRaw = sp.get("call_id");
  const callId = callIdRaw ? Number(callIdRaw) : null;
  const callTitle = sp.get("call_title") ?? "";
  const profileIdRaw = sp.get("profile_id");
  const profileId = profileIdRaw ? Number(profileIdRaw) : null;

  const [title, setTitle] = useState(callTitle);
  const [deadline, setDeadline] = useState("");
  const create = useCreateWorkspace();

  // From-call only: without a call there is nothing to draft against (Phase-0
  // requires a call). Guard a direct hit / refresh-without-params.
  if (callId === null || Number.isNaN(callId)) {
    return (
      <main className={styles.guard} role="alert">
        <h1>Start drafting</h1>
        <p>Pick a call from the dashboard to start drafting.</p>
        <Link to="/">← Back to calls</Link>
      </main>
    );
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const out = await create.mutateAsync({
      call_id: callId as number,
      title,
      deadline: deadline || undefined,
      profile_id: profileId,
    });
    // Hand off to the Jinja setup/rubric flow (not SPA-served) — full page nav.
    window.location.assign(`/workspaces/${out.id}`);
  }

  return (
    <main className={styles.wrap}>
      <h1>New workspace</h1>
      <p className={styles.context}>Drafting for: {callTitle || `call #${callId}`}</p>
      <form onSubmit={onSubmit} className={styles.form}>
        <label className={styles.label}>
          Title
          <input
            className={styles.input}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            required
            maxLength={500}
          />
        </label>
        <label className={styles.label}>
          Deadline (optional)
          <input
            className={styles.input}
            type="datetime-local"
            value={deadline}
            onChange={(e) => setDeadline(e.target.value)}
          />
        </label>
        <button
          className={styles.submit}
          type="submit"
          disabled={create.isPending || !title.trim()}
        >
          {create.isPending ? "Creating…" : "Start drafting"}
        </button>
        {create.isError && (
          <p role="alert" className={styles.error}>
            {create.error instanceof Error
              ? create.error.message
              : "Couldn’t create the workspace."}
          </p>
        )}
      </form>
    </main>
  );
}
```

Create `frontend/src/routes/WorkspaceCreate.module.css`:

```css
.wrap,
.guard {
  max-width: 32rem;
}
.context {
  color: var(--app-muted);
  font-size: 0.9rem;
  margin: 0.25rem 0 1rem;
}
.form {
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
}
.label {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  font-size: 0.85rem;
  color: var(--app-muted);
}
.input {
  padding: 0.4rem 0.5rem;
  border: 1px solid var(--app-border);
  border-radius: 4px;
  background: var(--app-bg, transparent);
  color: inherit;
  font-size: 0.95rem;
}
.submit {
  align-self: flex-start;
  padding: 0.4rem 0.9rem;
  border: 1px solid var(--app-link);
  border-radius: 4px;
  background: transparent;
  color: var(--app-link);
  cursor: pointer;
  font-weight: 500;
}
.submit:disabled {
  opacity: 0.5;
  cursor: default;
}
.error {
  color: var(--app-danger, #e5484d);
  font-size: 0.85rem;
  margin: 0;
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pnpm vitest run src/routes/WorkspaceCreate.test.tsx`
Expected: PASS.

- [ ] **Step 5: Type-check + commit**

```bash
pnpm exec tsc --noEmit
git add frontend/src/routes/WorkspaceCreate.tsx frontend/src/routes/WorkspaceCreate.module.css frontend/src/routes/WorkspaceCreate.test.tsx
git commit -m "feat(spa): WorkspaceCreate from-call form (/cockpit/new)"
```

---

### Task 7: Shell chrome for non-editor cockpit routes

**Files:**
- Modify: `frontend/src/cockpit/cockpitNav.ts` (the `cockpitIdFromPath` helper)
- Modify: `frontend/src/shell/TitleBar.tsx`
- Modify: `frontend/src/shell/ActivityBar.tsx`
- Test: `frontend/src/cockpit/cockpitNav.test.tsx`, `frontend/src/shell/TitleBar.test.tsx`, `frontend/src/shell/ActivityBar.test.tsx`, `frontend/src/shell/StatusBar.test.tsx`

**Interfaces:**
- Modifies: `cockpitIdFromPath(pathname: string): string` — now returns `""` for `/cockpit/new` and the bare `/cockpit` (in addition to off-cockpit), and the workspace id for `/cockpit/:id`.
- Consumes: `cockpitIdFromPath` in `ActivityBar` (new) to gate the "Current workspace" chip.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/cockpit/cockpitNav.test.tsx` (add the import + cases):

```tsx
import { cockpitIdFromPath } from "./cockpitNav";

it("cockpitIdFromPath returns the id only for the editor route", () => {
  expect(cockpitIdFromPath("/cockpit/abc")).toBe("abc");
  expect(cockpitIdFromPath("/cockpit/abc/sub")).toBe("abc");
  expect(cockpitIdFromPath("/cockpit/new")).toBe(""); // create route, NOT an id
  expect(cockpitIdFromPath("/cockpit")).toBe(""); // list route
  expect(cockpitIdFromPath("/")).toBe(""); // off cockpit
});
```

Append to `frontend/src/shell/TitleBar.test.tsx`:

```tsx
it("shows 'Workspaces' on the cockpit list and does not fetch", () => {
  const get = vi.spyOn(clientMod.api, "GET");
  renderAt("/cockpit");
  expect(screen.getByText("Workspaces")).toBeInTheDocument();
  expect(get).not.toHaveBeenCalled();
});

it("shows 'New workspace' on the create route and does not fetch", () => {
  const get = vi.spyOn(clientMod.api, "GET");
  renderAt("/cockpit/new");
  expect(screen.getByText("New workspace")).toBeInTheDocument();
  expect(get).not.toHaveBeenCalled();
});
```

Append to `frontend/src/shell/ActivityBar.test.tsx`:

```tsx
it("hides the current-workspace item on the cockpit list route", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit" });
  expect(screen.queryByRole("link", { name: "Current workspace" })).toBeNull();
});

it("hides the current-workspace item on the create route", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit/new" });
  expect(screen.queryByRole("link", { name: "Current workspace" })).toBeNull();
});
```

Append to `frontend/src/shell/StatusBar.test.tsx`:

```tsx
it("does NOT fetch on the cockpit create route (no editor chrome)", () => {
  const get = vi.spyOn(clientMod.api, "GET");
  renderAt("/cockpit/new");
  expect(get).not.toHaveBeenCalled();
});
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; nvm use 22; cd frontend
pnpm vitest run src/cockpit/cockpitNav.test.tsx src/shell/TitleBar.test.tsx src/shell/ActivityBar.test.tsx src/shell/StatusBar.test.tsx
```
Expected: FAIL — `/cockpit/new` currently yields id `"new"` (TitleBar shows the fallback title and fetches; ActivityBar shows the chip; StatusBar fetches); `cockpitIdFromPath` is not yet exported-tested for these cases.

- [ ] **Step 3: Fix `cockpitIdFromPath`**

In `frontend/src/cockpit/cockpitNav.ts`, replace the `cockpitIdFromPath` function:

```ts
// static /cockpit/* subpaths that are NOT workspace ids
const COCKPIT_RESERVED = new Set(["new"]);

/** Extract the workspace id from a `/cockpit/:id` editor pathname ("" off the
 *  editor: off-cockpit, the `/cockpit` list, or a reserved subpath like
 *  `/cockpit/new`). Lets the always-mounted shell chrome gate its cockpit
 *  queries by route. */
export function cockpitIdFromPath(pathname: string): string {
  const m = pathname.match(/^\/cockpit\/([^/]+)/);
  if (!m) return "";
  return COCKPIT_RESERVED.has(m[1]) ? "" : m[1];
}
```

- [ ] **Step 4: Title the three cockpit routes in `TitleBar`**

In `frontend/src/shell/TitleBar.tsx`, replace the `title` derivation (the single ternary on line ~15):

```tsx
export function TitleBar() {
  const { pathname } = useLocation();
  const id = cockpitIdFromPath(pathname);
  const meta = useCockpitMeta(id).data;
  // Editor → workspace title (falls back until it loads). Non-editor cockpit
  // routes get their own labels; everything else is the dashboard. useCockpitMeta
  // is gated (id===""), so the non-editor branches never fetch.
  let title: string;
  if (id) title = meta?.workspace_title ?? "Proposal workspace";
  else if (pathname === "/cockpit/new") title = "New workspace";
  else if (pathname.startsWith("/cockpit")) title = "Workspaces";
  else title = "Funding calls";
  return (
    <header className={styles.titlebar}>
      <span className={styles.brand}>
        <BrandMark size={18} />
        <span className={styles.app}>Call Manager</span>
      </span>
      <span className={styles.title}>{title}</span>
    </header>
  );
}
```

(StatusBar needs NO change — it already gates its editor chrome on `onCockpit = !!id`, which is now correctly `""` for the list/create routes.)

- [ ] **Step 5: Gate the ActivityBar chip on a real workspace id**

In `frontend/src/shell/ActivityBar.tsx`, add the import and replace the chip block:

```tsx
import { cockpitIdFromPath } from "../cockpit/cockpitNav";
```

```tsx
export function ActivityBar() {
  const { pathname } = useLocation();
  const wsId = cockpitIdFromPath(pathname);
  return (
    <nav className={styles.activitybar} aria-label="Primary">
      <div className={styles.group}>
        {ACTIVITY_TOP.map((it) => (
          <Item key={it.id} item={it} />
        ))}
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
        {ACTIVITY_BOTTOM.map((it) => (
          <Item key={it.id} item={it} />
        ))}
      </div>
    </nav>
  );
}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pnpm vitest run src/cockpit/cockpitNav.test.tsx src/shell/TitleBar.test.tsx src/shell/ActivityBar.test.tsx src/shell/StatusBar.test.tsx`
Expected: PASS (all, including the pre-existing cases — `/cockpit/abc` still yields the chip, `/cockpit/ws-1` still fetches the title).

- [ ] **Step 7: Type-check + commit**

```bash
pnpm exec tsc --noEmit
git add frontend/src/cockpit/cockpitNav.ts frontend/src/cockpit/cockpitNav.test.tsx frontend/src/shell/TitleBar.tsx frontend/src/shell/TitleBar.test.tsx frontend/src/shell/ActivityBar.tsx frontend/src/shell/ActivityBar.test.tsx frontend/src/shell/StatusBar.test.tsx
git commit -m "fix(spa): shell chrome handles the non-editor cockpit routes"
```

---

### Task 8: Wire the routes + generalize the drift guard + nav flip

**Files:**
- Modify: `frontend/src/router.tsx` (add the two routes)
- Modify: `frontend/src/router.test.tsx` (generalize the drift-guard reduction)
- Modify: `frontend/src/shell/nav-items.ts` (flip "workspaces" to SPA `/cockpit`)
- Modify: `frontend/src/shell/ActivityBar.test.tsx` (update the now-stale "deep-link to Jinja" assertion)

**Interfaces:**
- Consumes: `WorkspaceList`, `WorkspacesFilters` (Task 5), `WorkspaceCreate` (Task 6).
- Produces: live `/cockpit` (list) and `/cockpit/new` (create) routes; "Workspaces" nav → `/cockpit`.

- [ ] **Step 1: Update the router and generalize the drift guard (failing first)**

In `frontend/src/router.tsx`, add the imports:

```tsx
import { WorkspaceCreate } from "./routes/WorkspaceCreate";
import { WorkspaceList } from "./routes/WorkspaceList";
import { WorkspacesFilters } from "./routes/WorkspacesFilters";
```

and add the two child routes (place them right before the existing `cockpit/:id` route):

```tsx
{ path: "cockpit", element: <WorkspaceList />, handle: { Sidebar: WorkspacesFilters } },
{ path: "cockpit/new", element: <WorkspaceCreate /> },
{ path: "cockpit/:id", element: <Cockpit />, handle: { Sidebar: CockpitSidebar, RightPane: CockpitChat } },
```

- [ ] **Step 2: Run the drift-guard test to verify it now FAILS**

Run: `pnpm vitest run src/router.test.tsx`
Expected: FAIL — `/cockpit/new` is a concrete leaf the old reduction (only collapsing `/:param`) does not map to `/cockpit`, so the set no longer equals `SPA_ROUTES`.

- [ ] **Step 3: Generalize the drift-guard reduction**

In `frontend/src/router.test.tsx`, replace the test body (the `it(...)` block) with a reduction that collapses each concrete leaf to its owning manifest prefix (mirroring `is_spa_route`'s subtree match):

```tsx
// router.tsx is the only HAND-maintained consumer of the route-ownership
// manifest. Pin its concrete page paths (excluding the in-app "*" soft-404) to
// SPA_ROUTES by collapsing each concrete leaf to the manifest entry that OWNS it
// (the way is_spa_route matches a subtree: "/cockpit" owns "/cockpit/new" and
// "/cockpit/:id"; "/" owns only itself).
function toOwningPrefix(p: string): string {
  const owner = SPA_ROUTES.filter((r) =>
    r === "/" ? p === "/" : p === r || p.startsWith(r + "/"),
  ).sort((a, b) => b.length - a.length)[0];
  return owner ?? p; // unowned -> stays itself -> set mismatch -> guard fails
}

it("router concrete paths are all owned by the SPA_ROUTES manifest (drift guard)", () => {
  const concrete = leafPaths(router.routes as RouteObject[])
    .filter((p) => !p.includes("*"))
    .map(toOwningPrefix);
  expect(new Set(concrete)).toEqual(new Set(SPA_ROUTES));
});
```

- [ ] **Step 4: Run the drift-guard test to verify it passes**

Run: `pnpm vitest run src/router.test.tsx`
Expected: PASS — `/cockpit`, `/cockpit/new`, `/cockpit/:id` all collapse to `/cockpit`; `/` maps to `/`; the set equals `SPA_ROUTES`.

- [ ] **Step 5: Flip the nav item (failing first)**

In `frontend/src/shell/nav-items.ts`, change the "workspaces" item:

```ts
{
  id: "workspaces",
  label: "Workspaces",
  icon: "folder",
  kind: "spa",
  to: "/cockpit",
},
```

- [ ] **Step 6: Run the ActivityBar test to verify the stale assertion FAILS**

Run: `pnpm vitest run src/shell/ActivityBar.test.tsx`
Expected: FAIL — the "Workspaces, Profiles and Settings deep-link to their Jinja pages" test still asserts `Workspaces` → `/workspaces`.

- [ ] **Step 7: Update the stale ActivityBar assertion**

In `frontend/src/shell/ActivityBar.test.tsx`, replace the "deep-link to their Jinja pages" test with a split that reflects the flip (Workspaces is now an in-app SPA link; Profiles/Settings stay external):

```tsx
it("Workspaces is an SPA link to the cockpit front door", () => {
  renderWithProviders(<ActivityBar />, { route: "/" });
  expect(screen.getByRole("link", { name: "Workspaces" })).toHaveAttribute("href", "/cockpit");
});

it("Profiles and Settings still deep-link to their Jinja pages", () => {
  renderWithProviders(<ActivityBar />, { route: "/" });
  expect(screen.getByRole("link", { name: "Profiles" })).toHaveAttribute("href", "/profiles");
  expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/me");
});
```

- [ ] **Step 8: Run the affected tests to verify they pass**

Run: `pnpm vitest run src/router.test.tsx src/shell/ActivityBar.test.tsx src/shell/AppShell.test.tsx`
Expected: PASS (AppShell.test uses its own synthetic router and is unaffected; included as a guard).

- [ ] **Step 9: Type-check + commit**

```bash
pnpm exec tsc --noEmit
git add frontend/src/router.tsx frontend/src/router.test.tsx frontend/src/shell/nav-items.ts frontend/src/shell/ActivityBar.test.tsx
git commit -m "feat(spa): wire /cockpit list + /cockpit/new, flip Workspaces nav, generalize drift guard"
```

---

### Task 9: Dashboard "Start drafting" entry point (`CallRow` + `Dashboard`)

**Files:**
- Modify: `frontend/src/routes/CallRow.tsx` (add the Start-drafting Link + `profileId` prop)
- Modify: `frontend/src/routes/CallRow.module.css` (style the action)
- Modify: `frontend/src/routes/Dashboard.tsx` (pass `effective.profile_id`)
- Test: `frontend/src/routes/CallRow.test.tsx` (wrap in a Router + assert the link), `frontend/src/routes/Dashboard.test.tsx` (integration)

**Interfaces:**
- Consumes: `data.effective.profile_id` from the dashboard query.
- Produces: each call row links to `/cockpit/new?call_id=…&call_title=…[&profile_id=…]`.

- [ ] **Step 1: Update the failing tests**

Replace `frontend/src/routes/CallRow.test.tsx` (CallRow now contains a react-router `<Link>`, so renders must be wrapped in a Router):

```tsx
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it } from "vitest";

import type { CallItem } from "../api/dashboard";
import { CallRow } from "./CallRow";

function call(over: Partial<CallItem> = {}): CallItem {
  return {
    id: 3,
    title: "Grant X",
    source_name: "Vinnova",
    source_slug: "vinnova",
    deadline: null,
    published_at: null,
    aggregate_score: 82,
    ...over,
  } as CallItem;
}

function renderRow(ui: React.ReactElement) {
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}

it("links the title to the Jinja call-detail page, shows source + match badge", () => {
  renderRow(<CallRow call={call()} />);
  expect(screen.getByRole("link", { name: "Grant X" })).toHaveAttribute("href", "/calls/3");
  expect(screen.getByText("Vinnova")).toBeInTheDocument();
  expect(screen.getByText("82")).toHaveAttribute("data-band", "strong");
});

it("omits the match badge when the score is null", () => {
  renderRow(<CallRow call={call({ aggregate_score: null })} />);
  expect(screen.queryByText("82")).toBeNull();
});

it("Start drafting links to /cockpit/new with the call prefilled + profile", () => {
  renderRow(<CallRow call={call()} profileId={7} />);
  const href = screen.getByRole("link", { name: "Start drafting" }).getAttribute("href") ?? "";
  expect(href).toContain("/cockpit/new?");
  expect(href).toContain("call_id=3");
  expect(href).toContain("call_title=Grant+X"); // URLSearchParams encodes space as +
  expect(href).toContain("profile_id=7");
});

it("omits profile_id from the Start-drafting link when there is no resolved profile", () => {
  renderRow(<CallRow call={call()} />);
  const href = screen.getByRole("link", { name: "Start drafting" }).getAttribute("href") ?? "";
  expect(href).not.toContain("profile_id");
});
```

Append to `frontend/src/routes/Dashboard.test.tsx`:

```tsx
it("threads the dashboard's effective profile into the Start-drafting links", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(
    itemsBody([row(1, "Call one", null)], {
      effective: { profile_id: 5, source: null, sort: "newest", show_closed: false },
    }) as never,
  );
  renderAt("/");
  const start = await screen.findByRole("link", { name: "Start drafting" });
  expect(start.getAttribute("href")).toContain("profile_id=5");
});
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; nvm use 22; cd frontend
pnpm vitest run src/routes/CallRow.test.tsx src/routes/Dashboard.test.tsx
```
Expected: FAIL — no "Start drafting" link yet.

- [ ] **Step 3: Add the Start-drafting link to `CallRow`**

Replace `frontend/src/routes/CallRow.tsx`:

```tsx
import { Link } from "react-router-dom";

import type { CallItem } from "../api/dashboard";
import { MatchBadge } from "../components/MatchBadge";
import { Deadline } from "./Deadline";
import styles from "./CallRow.module.css";

// One call in the list: title link (to the un-ported Jinja detail page), a muted
// meta line (source · deadline), the banded match pill, and a "Start drafting"
// action that opens the React from-call create form. profileId is the dashboard's
// resolved effective profile (carried so Phase-0 can snapshot it).
export function CallRow({ call, profileId }: { call: CallItem; profileId?: number | null }) {
  const params = new URLSearchParams({ call_id: String(call.id), call_title: call.title });
  if (profileId != null) params.set("profile_id", String(profileId));
  const startHref = `/cockpit/new?${params.toString()}`;
  return (
    <li className={styles.row}>
      <div className={styles.body}>
        <a className={styles.title} href={`/calls/${call.id}`}>
          {call.title}
        </a>
        <div className={styles.meta}>
          {call.source_name && <span>{call.source_name}</span>}
          {call.source_name && call.deadline && <span aria-hidden> · </span>}
          {call.deadline && <Deadline iso={call.deadline} />}
        </div>
      </div>
      <div className={styles.actions}>
        <MatchBadge score={call.aggregate_score} />
        <Link className={styles.start} to={startHref}>
          Start drafting
        </Link>
      </div>
    </li>
  );
}
```

Append to `frontend/src/routes/CallRow.module.css`:

```css
.actions {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 0.3rem;
  flex-shrink: 0;
}
.start {
  color: var(--app-link);
  text-decoration: none;
  font-size: 0.8rem;
  font-weight: 500;
  white-space: nowrap;
}
.start:hover {
  text-decoration: underline;
}
```

- [ ] **Step 4: Pass the effective profile in `Dashboard`**

In `frontend/src/routes/Dashboard.tsx`, change the map:

```tsx
{data.items.map((c) => (
  <CallRow key={c.id} call={c} profileId={data.effective.profile_id} />
))}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pnpm vitest run src/routes/CallRow.test.tsx src/routes/Dashboard.test.tsx`
Expected: PASS.

- [ ] **Step 6: Type-check + commit**

```bash
pnpm exec tsc --noEmit
git add frontend/src/routes/CallRow.tsx frontend/src/routes/CallRow.module.css frontend/src/routes/Dashboard.tsx frontend/src/routes/CallRow.test.tsx frontend/src/routes/Dashboard.test.tsx
git commit -m "feat(spa): Start-drafting entry point on the dashboard call rows"
```

---

## Final verification (after all tasks)

- [ ] **Backend (targeted):**
  `uv run pytest tests/test_api_workspaces.py -v` (this file only — NOT the whole suite).
  Expected: all green, including the pre-existing cockpit JSON tests.
- [ ] **Frontend type gate + full unit suite + build:**
  ```bash
  export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; nvm use 22; cd frontend
  pnpm exec tsc --noEmit && pnpm vitest run && pnpm build
  ```
  Expected: tsc clean, all vitest green, build succeeds.
- [ ] **Schema diff is additive:** `git diff "$(git merge-base HEAD master)"..HEAD -- frontend/src/api/schema.gen.ts` shows only additions.
- [ ] **Manifest untouched:** `git diff "$(git merge-base HEAD master)"..HEAD -- frontend/src/routes.gen.ts` is empty.
