# SPA Redesign — Phase 3b (Cockpit Workspace groups + rich status bar) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the cockpit IA: add three additive read-only backend endpoints (`cockpit-meta`, `versions`, `members`), surface the four Workspace sidebar groups (References · Criteria · Versions · Members) under the relocated Outline, and populate the status bar + title-bar breadcrumb with workspace context (deadline · spec-lock · scorecard band · title).

**Architecture:** Three small member/owner-gated GET handlers in the existing `web/api/workspaces.py` (no new tables/migration), typed into the SPA via the OpenAPI codegen, then consumed by new React Query hooks. The sidebar groups and the always-mounted StatusBar/TitleBar read those hooks the same way Phase 3a crossed the AppShell boundary — global channels (React Query cache + the URL), never props/context. `members` is owner-gated; non-owners see only the non-PII `member_count` from `cockpit-meta`.

**Tech Stack:** Python 3.12 + FastAPI + SQLAlchemy 2.x + Postgres (pytest + testcontainers) · React 19 + Vite + TanStack Query 5 + `openapi-typescript` codegen + Vitest 2 + Testing Library.

**Spec:** `docs/superpowers/specs/2026-06-23-spa-vscode-redesign-phase3-cockpit-design.md` — this plan implements **§5 (backend endpoints) + §8 Plan 3b**. Builds on the merged Phase 3a (`CockpitSidebar`, `SidebarGroup`, `useCockpitNav`, `editorStore`, the populated-on-cockpit `StatusBar`). Read it.

## Global Constraints

- **Backend changes ARE in scope** (the user lifted the no-backend rule). All new endpoints are **additive, read-only, member/owner-gated, NO new tables / NO migration** (alembic head stays where it is). No change to any existing write path.
- **Backend toolchain:** `uv` for Python; run `uv sync` once in the worktree before backend tasks (a worktree has its own `.venv`). Tests use **testcontainers Postgres (Docker required)**. Per project memory, the WHOLE suite is flaky under load (the test Postgres drops connections) — so **gate each backend task with the TARGETED test ids only** (`uv run pytest tests/test_api_workspaces.py::test_x -v`), never the whole suite; re-run a flaky failure in isolation before treating it as real.
- **Frontend toolchain:** pnpm 10.16 (Node ≥ 20); `pnpm install` after any `package.json` change (this plan adds **no** new dependency). Gate each frontend task, all green, from `frontend/`: `pnpm exec tsc --noEmit` AND `pnpm vitest run` AND `pnpm build`. There is no TS linter — `tsc` is the gate.
- **OpenAPI codegen (Task 4):** regenerate `frontend/src/api/schema.gen.ts` by running, from the worktree ROOT, **`EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`**. The `EMBEDDING_MODEL=bge-m3` prefix is MANDATORY — the `.env` dev model (`mxbai-embed-large`) logs a calibration WARNING to stdout that corrupts the JSON `openapi-typescript` parses (project memory). The script needs `uv` + `pnpm` but NO Docker/DB (lazy engine). Commit the regenerated `schema.gen.ts`.
- **Endpoint contracts (verbatim from spec §5):**
  - `GET /api/workspaces/{id}/cockpit-meta` (member-gated) → `{ workspace_title: str, workspace_deadline: datetime|null, current_phase: "setup"|"active", role: "owner"|"member", spec_locked: bool, member_count: int }`. Reads **`Workspace.title` / `Workspace.deadline`** (NOT the nullable `Call`). `member_count` = `1 (owner) + count(WorkspaceViewer)`.
  - `GET /api/workspaces/{id}/versions` (member-gated) → `list[{ commit_sha, message, created_by, created_at }]`, newest-first, capped 50, from `ArtifactVersion`.
  - `GET /api/workspaces/{id}/members` (**owner-gated**, `require_owner`) → `list[{ email, role: "owner"|"member" }]`, owner first. Mirrors the existing settings viewer surface — emails stay owner-only; non-owners see only `member_count`.
- **Criteria needs NO endpoint** — the Criteria sidebar group reuses the existing `useScorecard` (`scorecard.criteria[].{name,band,index}`).
- **Cross-boundary state = global channels only** (Phase-2/3a discipline). The always-mounted `StatusBar`/`TitleBar` derive the workspace id from the URL via `cockpitIdFromPath(pathname)` and pass it to `useCockpitMeta`/`useScorecard` with `enabled: !!id` — so they DON'T fetch off the cockpit. No props/context across the AppShell boundary.
- **Re-skin/extend, not rewrite.** Preserve all Phase-3a behavior + tests. `member`/`owner` auth semantics mirror `require_member`/`require_owner` exactly (403/404 shape: the global handler renders `{"error":{"code":"forbidden",...}}` for a 403).
- **Git — every commit uses THIS exact form.** All work is in the worktree `WT=/home/parlar_ai/dev/call-monitor/.worktrees/spa-vscode-redesign-phase3b` on branch `feat/spa-vscode-redesign-phase3b` (off `master`). Subagents default git/cwd to the MAIN tree (WRONG branch), so **before any commit** run `git -C "$WT" branch --show-current` and confirm it prints exactly `feat/spa-vscode-redesign-phase3b`. Then:
  ```bash
  git -C "$WT" add <paths>
  git -C "$WT" -c commit.gpgsign=false commit -m "<message>

  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
  ```

---

## File Structure

| File | Responsibility |
|---|---|
| `src/call_manager/web/api/workspaces.py` (modify) | Add 3 GET handlers + their Pydantic `*Out` models (`CockpitMetaOut`, `VersionOut`, `MemberOut`) |
| `tests/test_api_workspaces.py` (modify) | pytest for the 3 endpoints (shape, gating, edge cases) |
| `frontend/src/api/schema.gen.ts` (regenerate) | OpenAPI types for the 3 new paths |
| `frontend/src/api/workspace.ts` (new) + `.test.tsx` | `useCockpitMeta` / `useVersions` / `useMembers` hooks (gated) |
| `frontend/src/api/evaluator.ts` (modify) | `useScorecard` gains `enabled: !!id` (so StatusBar can call it safely) |
| `frontend/src/cockpit/cockpitNav.ts` (modify) | Add `cockpitIdFromPath(pathname)` helper |
| `frontend/src/cockpit/CockpitSidebar.tsx` (modify) + `.module.css` + `.test.tsx` | Add References · Criteria · Versions · Members groups |
| `frontend/src/shell/StatusBar.tsx` (modify) + `.module.css` + `.test.tsx` | Add deadline · spec-lock · scorecard band |
| `frontend/src/shell/TitleBar.tsx` (modify) + `.test.tsx` (new) | Breadcrumb = workspace title from `useCockpitMeta` |

---

### Task 1: Backend — `GET /cockpit-meta`

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py`
- Modify: `tests/test_api_workspaces.py`

**Interfaces:**
- Produces: `CockpitMetaOut { workspace_title: str, workspace_deadline: datetime|None, current_phase: str, role: str, spec_locked: bool, member_count: int }`; route `GET /api/workspaces/{workspace_id}/cockpit-meta` (member-gated).
- Consumes: `require_member` (returns the `Workspace`), `WorkspaceViewer`, `func` (sqlalchemy).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_api_workspaces.py`:

```python
def test_cockpit_meta_owner(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/cockpit-meta")
    assert r.status_code == 200
    b = r.json()
    assert b["workspace_title"] == "W"
    assert b["current_phase"] == "active"
    assert b["role"] == "owner"
    assert b["spec_locked"] is False
    assert b["member_count"] == 2  # owner + 1 viewer
    assert b["workspace_deadline"] is None


def test_cockpit_meta_member_sees_member_role(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, member_id).get(f"/api/workspaces/{ws_id}/cockpit-meta")
    assert r.status_code == 200
    assert r.json()["role"] == "member"


def test_cockpit_meta_non_member_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, _, stranger_id, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, stranger_id).get(f"/api/workspaces/{ws_id}/cockpit-meta")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"
```

- [ ] **Step 2: Run — Expected: FAIL** (404 route-not-found / no handler).

Run: `uv run pytest tests/test_api_workspaces.py::test_cockpit_meta_owner tests/test_api_workspaces.py::test_cockpit_meta_member_sees_member_role tests/test_api_workspaces.py::test_cockpit_meta_non_member_403 -v`

- [ ] **Step 3: Implement.** In `src/call_manager/web/api/workspaces.py`:

(a) Extend the sqlalchemy import (currently `from sqlalchemy import select`) to:
```python
from sqlalchemy import func, select
```
(b) Add `WorkspaceViewer` to the `call_manager.db.models` import (it already imports `ArtifactVersion, Reference, ReferenceChunk, SpecLockEvent, User, WorkspaceEvent`):
```python
from call_manager.db.models import (
    ArtifactVersion,
    Reference,
    ReferenceChunk,
    SpecLockEvent,
    User,
    WorkspaceEvent,
    WorkspaceViewer,
)
```
(c) Add the model + handler (place near the other read endpoints, e.g. just before `list_references`):
```python
class CockpitMetaOut(BaseModel):
    workspace_title: str
    workspace_deadline: datetime | None
    current_phase: str  # "setup" | "active"
    role: str  # "owner" | "member"
    spec_locked: bool
    member_count: int


@router.get("/workspaces/{workspace_id}/cockpit-meta", response_model=CockpitMetaOut)
def get_cockpit_meta(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> CockpitMetaOut:
    """Workspace header read-model for the cockpit status bar + title breadcrumb.
    Member-gated. Reads the workspace-level title/deadline (NOT the optional linked
    Call). member_count = owner + viewers (a non-PII aggregate shown to every member)."""
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        viewer_count = session.scalar(
            select(func.count())
            .select_from(WorkspaceViewer)
            .where(WorkspaceViewer.workspace_id == workspace_id)
        )
        return CockpitMetaOut(
            workspace_title=ws.title,
            workspace_deadline=ws.deadline,
            current_phase=ws.current_phase,
            role="owner" if ws.owner_id == user.id else "member",
            spec_locked=ws.spec_locked_at is not None,
            member_count=1 + int(viewer_count or 0),
        )
```

- [ ] **Step 4: Run — Expected: PASS** (3 tests).

Run: `uv run pytest tests/test_api_workspaces.py::test_cockpit_meta_owner tests/test_api_workspaces.py::test_cockpit_meta_member_sees_member_role tests/test_api_workspaces.py::test_cockpit_meta_non_member_403 -v`

- [ ] **Step 5: Commit**

```bash
git -C "$WT" branch --show-current   # MUST print feat/spa-vscode-redesign-phase3b
git -C "$WT" add src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
git -C "$WT" -c commit.gpgsign=false commit -m "feat(api): GET /workspaces/{id}/cockpit-meta (member-gated header read-model)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Backend — `GET /versions`

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py`, `tests/test_api_workspaces.py`

**Interfaces:**
- Produces: `VersionOut { commit_sha: str, message: str, created_by: str, created_at: datetime }`; route `GET /api/workspaces/{workspace_id}/versions` (member-gated, newest-first, ≤50).
- Consumes: `ArtifactVersion` (already imported), `require_member`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_api_workspaces.py` (add `ArtifactVersion` to the `call_manager.db.models` import at the top, and `from datetime import UTC, datetime, timedelta` near the top imports):

```python
def test_versions_newest_first(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    now = datetime.now(UTC)
    with pg_session_factory() as s:
        s.add_all([
            ArtifactVersion(workspace_id=ws_id, commit_sha="a" * 40, created_by="user",
                            message="older", created_at=now - timedelta(hours=1)),
            ArtifactVersion(workspace_id=ws_id, commit_sha="b" * 40, created_by="agent",
                            message="newer", created_at=now),
        ])
        s.commit()
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/versions")
    assert r.status_code == 200
    b = r.json()
    assert [v["message"] for v in b] == ["newer", "older"]
    assert b[0]["created_by"] == "agent"
    assert b[0]["commit_sha"] == "b" * 40


def test_versions_empty_ok(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/versions")
    assert r.status_code == 200
    assert r.json() == []


def test_versions_non_member_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    _, _, stranger_id, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, stranger_id).get(f"/api/workspaces/{ws_id}/versions")
    assert r.status_code == 403
```

- [ ] **Step 2: Run — Expected: FAIL** (no handler).

Run: `uv run pytest tests/test_api_workspaces.py -k versions -v`

- [ ] **Step 3: Implement.** Add to `src/call_manager/web/api/workspaces.py` (near `get_cockpit_meta`):
```python
class VersionOut(BaseModel):
    commit_sha: str
    message: str
    created_by: str
    created_at: datetime


@router.get("/workspaces/{workspace_id}/versions", response_model=list[VersionOut])
def list_versions(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> list[VersionOut]:
    """Recent artifact versions (git commits) for the Versions sidebar group.
    Member-gated, newest-first, capped at 50."""
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        rows = session.scalars(
            select(ArtifactVersion)
            .where(ArtifactVersion.workspace_id == workspace_id)
            .order_by(ArtifactVersion.created_at.desc())
            .limit(50)
        ).all()
        return [
            VersionOut(
                commit_sha=v.commit_sha,
                message=v.message,
                created_by=v.created_by,
                created_at=v.created_at,
            )
            for v in rows
        ]
```

- [ ] **Step 4: Run — Expected: PASS** (3 tests).

Run: `uv run pytest tests/test_api_workspaces.py -k versions -v`

- [ ] **Step 5: Commit**

```bash
git -C "$WT" branch --show-current
git -C "$WT" add src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
git -C "$WT" -c commit.gpgsign=false commit -m "feat(api): GET /workspaces/{id}/versions (member-gated commit log)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Backend — `GET /members` (owner-gated)

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py`, `tests/test_api_workspaces.py`

**Interfaces:**
- Produces: `MemberOut { email: str, role: str }`; route `GET /api/workspaces/{workspace_id}/members` (**owner-gated**, owner first).
- Consumes: `require_owner` (returns the `Workspace`), `User`, `WorkspaceViewer`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_api_workspaces.py`:

```python
def test_members_owner_sees_named_list(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, owner_id).get(f"/api/workspaces/{ws_id}/members")
    assert r.status_code == 200
    b = r.json()
    assert b[0] == {"email": "owner@ki.se", "role": "owner"}  # owner first
    assert {"email": "member@ki.se", "role": "member"} in b
    assert len(b) == 2


def test_members_non_owner_member_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    # A workspace MEMBER (not just a stranger) must NOT see the named list — emails
    # stay owner-only (mirrors the settings viewer surface; no PII expansion).
    app = _app(monkeypatch, pg_url, tmp_path)
    _, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n")
    r = _client(app, member_id).get(f"/api/workspaces/{ws_id}/members")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"
```

- [ ] **Step 2: Run — Expected: FAIL** (no handler).

Run: `uv run pytest tests/test_api_workspaces.py -k members -v`

- [ ] **Step 3: Implement.** Add to `src/call_manager/web/api/workspaces.py` (near `list_versions`). Note `require_owner` is already imported (`from call_manager.proposal_assist.web._perms import require_member, require_owner`):
```python
class MemberOut(BaseModel):
    email: str
    role: str  # "owner" | "member"


@router.get("/workspaces/{workspace_id}/members", response_model=list[MemberOut])
def list_members(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> list[MemberOut]:
    """Owner + viewers for the Members sidebar group. OWNER-GATED — mirrors the
    settings viewer list, so member emails stay visible to the owner only (no PII
    expansion; non-owners get only the member_count from cockpit-meta). Owner first."""
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        owner = session.get(User, ws.owner_id)
        viewers = session.scalars(
            select(User)
            .join(WorkspaceViewer, WorkspaceViewer.user_id == User.id)
            .where(WorkspaceViewer.workspace_id == workspace_id)
            .order_by(User.email)
        ).all()
        out: list[MemberOut] = []
        if owner is not None:
            out.append(MemberOut(email=owner.email, role="owner"))
        out.extend(MemberOut(email=v.email, role="member") for v in viewers)
        return out
```

- [ ] **Step 4: Run — Expected: PASS** (2 tests).

Run: `uv run pytest tests/test_api_workspaces.py -k members -v`

- [ ] **Step 5: Commit**

```bash
git -C "$WT" branch --show-current
git -C "$WT" add src/call_manager/web/api/workspaces.py tests/test_api_workspaces.py
git -C "$WT" -c commit.gpgsign=false commit -m "feat(api): GET /workspaces/{id}/members (owner-gated; emails owner-only)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Regenerate OpenAPI types + add frontend hooks

**Files:**
- Regenerate: `frontend/src/api/schema.gen.ts`
- Create: `frontend/src/api/workspace.ts`, `frontend/src/api/workspace.test.tsx`
- Modify: `frontend/src/api/evaluator.ts` (gate `useScorecard`)
- Modify: `frontend/src/cockpit/cockpitNav.ts` (add `cockpitIdFromPath`)

**Interfaces:**
- Produces: `useCockpitMeta(id)`, `useVersions(id)`, `useMembers(id)` (all gated `enabled: !!id`; `useMembers` has `retry: false` so a 403 doesn't retry); `cockpitIdFromPath(pathname): string`. `useScorecard(id)` gains `enabled: !!id`.
- Consumes: the regenerated `schema.gen.ts` (the 3 new typed paths), `api`/`unwrap` (`./client`).

- [ ] **Step 1: Regenerate the schema** (from the worktree ROOT, NOT `frontend/`):

Run: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`
Expected: `wrote frontend/src/api/schema.gen.ts`; `git -C "$WT" diff --stat` shows ONLY `frontend/src/api/schema.gen.ts` changed, adding the `cockpit-meta` / `versions` / `members` paths + the `CockpitMetaOut`/`VersionOut`/`MemberOut` schemas.

- [ ] **Step 2: Add `cockpitIdFromPath`** to `frontend/src/cockpit/cockpitNav.ts` (append, exported):
```ts
/** Extract the workspace id from a `/cockpit/:id` pathname (""" off the cockpit).
 *  Lets the always-mounted StatusBar/TitleBar gate their cockpit queries by route. */
export function cockpitIdFromPath(pathname: string): string {
  const m = pathname.match(/^\/cockpit\/([^/]+)/);
  return m ? m[1] : "";
}
```

- [ ] **Step 3: Gate `useScorecard`** in `frontend/src/api/evaluator.ts` — add `enabled: !!id` to its `useQuery` (so the StatusBar can call it with `""` off the cockpit without firing a request). The hook becomes:
```ts
export function useScorecard(id: string) {
  return useQuery({
    queryKey: ["scorecard", id],
    enabled: !!id,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/scorecard", {
          params: { path: { workspace_id: id } },
        }),
      ) as ScorecardResponse | null,
  });
}
```

- [ ] **Step 4: Write the failing tests** `frontend/src/api/workspace.test.tsx`:
```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import * as clientMod from "./client";
import { useCockpitMeta } from "./workspace";

function Probe({ id }: { id: string }) {
  const q = useCockpitMeta(id);
  return <span data-testid="title">{q.data?.workspace_title ?? "—"}</span>;
}
function renderWith(id: string) {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <Probe id={id} />
    </QueryClientProvider>,
  );
}

it("useCockpitMeta does NOT fetch when id is empty (off-cockpit gating)", () => {
  const get = vi.spyOn(clientMod.api, "GET");
  renderWith("");
  expect(get).not.toHaveBeenCalled();
});

it("useCockpitMeta fetches and returns the header when id is present", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { workspace_title: "My WS", workspace_deadline: null, current_phase: "active", role: "owner", spec_locked: false, member_count: 2 },
    error: undefined,
  } as never);
  const { getByTestId } = renderWith("ws-1");
  await waitFor(() => expect(getByTestId("title").textContent).toBe("My WS"));
});
```

- [ ] **Step 5: Run — Expected: FAIL** (`Cannot find module './workspace'`).

Run: `pnpm vitest run src/api/workspace.test.tsx`

- [ ] **Step 6: Implement** `frontend/src/api/workspace.ts`:
```ts
import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";

/** GET /api/workspaces/{id}/cockpit-meta — header read-model for the status bar +
 *  title breadcrumb. Gated: the always-mounted StatusBar/TitleBar pass "" off-cockpit. */
export function useCockpitMeta(id: string) {
  return useQuery({
    queryKey: ["cockpit-meta", id],
    enabled: !!id,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/cockpit-meta", {
          params: { path: { workspace_id: id } },
        }),
      ),
  });
}

/** GET /api/workspaces/{id}/versions — recent commits for the Versions group. */
export function useVersions(id: string) {
  return useQuery({
    queryKey: ["versions", id],
    enabled: !!id,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/versions", {
          params: { path: { workspace_id: id } },
        }),
      ),
  });
}

/** GET /api/workspaces/{id}/members — OWNER-GATED. A non-owner member gets a 403,
 *  which is expected (they fall back to the member_count) — so do NOT retry it. */
export function useMembers(id: string) {
  return useQuery({
    queryKey: ["members", id],
    enabled: !!id,
    retry: false,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/members", {
          params: { path: { workspace_id: id } },
        }),
      ),
  });
}
```

- [ ] **Step 7: Run — Expected: PASS** (2 tests). Then the FULL frontend gate (the codegen + the `useScorecard` change must not regress anything).

Run: `pnpm vitest run src/api/workspace.test.tsx` then `pnpm exec tsc --noEmit && pnpm vitest run && pnpm build`

- [ ] **Step 8: Commit**

```bash
git -C "$WT" branch --show-current
git -C "$WT" add frontend/src/api/schema.gen.ts frontend/src/api/workspace.ts frontend/src/api/workspace.test.tsx frontend/src/api/evaluator.ts frontend/src/cockpit/cockpitNav.ts
git -C "$WT" -c commit.gpgsign=false commit -m "feat(api): cockpit-meta/versions/members hooks + regen types; gate useScorecard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Sidebar — References + Criteria groups

**Files:**
- Modify: `frontend/src/cockpit/CockpitSidebar.tsx`, `frontend/src/cockpit/CockpitSidebar.module.css`, `frontend/src/cockpit/CockpitSidebar.test.tsx`

**Interfaces:**
- Consumes: `useReferences` (`../api/citations`), `useScorecard` (`../api/evaluator`), `useCockpitNav` (`navigate`), `SidebarGroup`.
- Produces: two new collapsible groups under the Outline. References rows are display-only (`citekey · title`). Criteria rows show a band pill + name and `navigate({ top_tab: "scorecard" })` on click.

- [ ] **Step 1: Add the CSS** — append to `frontend/src/cockpit/CockpitSidebar.module.css`:
```css
.metaRow {
  display: flex;
  align-items: baseline;
  gap: 0.4rem;
  padding: 0.2rem 0.5rem 0.2rem 1.25rem;
  font-size: 0.85rem;
  color: var(--app-text);
}
.metaSub {
  color: var(--app-muted);
  font-size: 0.78rem;
}
.criterion {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  width: 100%;
  border: none;
  background: transparent;
  padding: 0.2rem 0.5rem 0.2rem 1.25rem;
  cursor: pointer;
  color: var(--app-text);
  font-size: 0.85rem;
  text-align: left;
}
.criterion:hover {
  background: var(--app-hover);
}
.criterionName {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
```

- [ ] **Step 2: Implement the groups.** Replace `frontend/src/cockpit/CockpitSidebar.tsx` with (adds References + Criteria below the Outline; keeps the Outline exactly as Phase 3a shipped it):
```tsx
import { useParams } from "react-router-dom";

import { useReferences } from "../api/citations";
import { useDraft, useSpec } from "../api/editor";
import { type ScorecardCriterion, useScorecard } from "../api/evaluator";
import { useCockpitNav } from "./cockpitNav";
import styles from "./CockpitSidebar.module.css";
import { useEditorStore } from "./editorStore";
import { SidebarGroup } from "./SidebarGroup";

const BAND_CLASS: Record<ScorecardCriterion["band"], string> = {
  strong: "band-strong",
  adequate: "band-adequate",
  at_risk: "band-at-risk",
  gap: "band-gap",
};
const BAND_LABEL: Record<ScorecardCriterion["band"], string> = {
  strong: "Strong",
  adequate: "Adequate",
  at_risk: "At-risk",
  gap: "Gap",
};

// The cockpit's left sidebar (the route's handle.Sidebar). Reads the SAME queries +
// ?tab/?sec as the editor/panels — React Query dedupes, the URL is the shared channel —
// so everything stays in sync across the AppShell boundary with no props. All hooks are
// always called (stable order); on scorecard/review tabs the Outline shows Draft sections.
export function CockpitSidebar() {
  const { id = "" } = useParams();
  const { tab, sec, navigate } = useCockpitNav();
  const artifact: "draft" | "spec" = tab === "spec" ? "spec" : "draft";
  const draft = useDraft(id);
  const spec = useSpec(id);
  const q = artifact === "spec" ? spec : draft;
  const { dirty } = useEditorStore();
  const sections = q.data?.sections ?? [];

  const references = useReferences(id);
  // Array.isArray guards keep the never-500 sidebar robust to an unexpected response
  // shape (and keep the Phase-3a tests — which stub every GET with the draft fixture —
  // green); criteria uses deep optional chaining for the same reason.
  const refs = Array.isArray(references.data) ? references.data : [];
  const scorecard = useScorecard(id);
  const criteria = scorecard.data?.scorecard?.criteria ?? [];

  return (
    <div className={styles.sidebar}>
      <SidebarGroup title="Outline" count={sections.length || undefined}>
        {q.isLoading && <p className={styles.note}>Loading…</p>}
        {!q.isLoading && sections.length === 0 && <p className={styles.note}>No sections.</p>}
        <ul className={styles.outline} aria-label="Outline">
          {sections.map((s) => (
            <li key={s.index}>
              <button
                type="button"
                className={styles.row}
                aria-current={sec === s.index}
                onClick={() => navigate({ top_tab: artifact, section_index: s.index })}
              >
                <span className={styles.rowLabel}>{s.heading || "preamble"}</span>
                {dirty.includes(s.index) && (
                  <span className={styles.dirty} aria-label="unsaved">
                    •
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      </SidebarGroup>

      <SidebarGroup title="References" count={refs.length || undefined} defaultOpen={false}>
        {refs.length === 0 && <p className={styles.note}>No references.</p>}
        <ul className={styles.outline} aria-label="References">
          {refs.map((r) => (
            <li key={r.id} className={styles.metaRow}>
              {r.citekey && <span className={styles.metaSub}>{r.citekey}</span>}
              <span className={styles.rowLabel}>{r.title}</span>
            </li>
          ))}
        </ul>
      </SidebarGroup>

      <SidebarGroup title="Criteria" count={criteria.length || undefined} defaultOpen={false}>
        {criteria.length === 0 && <p className={styles.note}>No criteria yet.</p>}
        <ul className={styles.outline} aria-label="Criteria">
          {criteria.map((c) => (
            <li key={c.index}>
              <button
                type="button"
                className={styles.criterion}
                onClick={() => navigate({ top_tab: "scorecard" })}
              >
                <span className={`band-pill ${BAND_CLASS[c.band]}`}>{BAND_LABEL[c.band]}</span>
                <span className={styles.criterionName}>{c.name}</span>
              </button>
            </li>
          ))}
        </ul>
      </SidebarGroup>
    </div>
  );
}
```

- [ ] **Step 3: Add tests** — append to `frontend/src/cockpit/CockpitSidebar.test.tsx` (the existing mock returns the draft fixture for all GETs; these tests add `/references` + `/scorecard` payloads). Add a focused render that mocks both:
```tsx
it("renders References and Criteria groups with a band pill", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/references"))
      return Promise.resolve({ data: [{ id: "r1", citekey: "smith2021", title: "Smith 2021", kind: "paper", text_status: "done" }], error: undefined });
    if (path.endsWith("/scorecard"))
      return Promise.resolve({
        data: { scorecard: { criteria: [{ index: 0, name: "Impact", band: "gap", note: "", deep_link: null, weight: 1, effective_weight: 1 }], overall_band: "gap", summary: "" }, evaluated_draft_sha: null, evaluated_spec_sha: null, completed_at: null, stale: false, stale_reasons: [] },
        error: undefined,
      });
    return Promise.resolve(draft as never); // /draft + /spec
  }) as never);
  renderSidebar();
  // Open the (default-collapsed) groups.
  fireEvent.click(await screen.findByRole("button", { name: /references/i }));
  expect(await screen.findByText("Smith 2021")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /criteria/i }));
  expect(await screen.findByText("Impact")).toBeInTheDocument();
  expect(screen.getByText("Gap")).toBeInTheDocument();
});

it("clicking a criterion navigates to the Scorecard tab", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/scorecard"))
      return Promise.resolve({
        data: { scorecard: { criteria: [{ index: 0, name: "Impact", band: "gap", note: "", deep_link: null, weight: 1, effective_weight: 1 }], overall_band: "gap", summary: "" }, evaluated_draft_sha: null, evaluated_spec_sha: null, completed_at: null, stale: false, stale_reasons: [] },
        error: undefined,
      });
    return Promise.resolve(draft as never);
  }) as never);
  renderSidebar();
  fireEvent.click(await screen.findByRole("button", { name: /criteria/i }));
  fireEvent.click(await screen.findByRole("button", { name: /Impact/ }));
  expect(screen.getByTestId("search").textContent).toContain("tab=scorecard");
});
```
> Note: the existing `CockpitSidebar.test.tsx` `renderSidebar()` helper already mounts `<CockpitSidebar/>` + a `<Probe/>` exposing `data-testid="search"` (the location). Reuse it.

- [ ] **Step 4: Run + gate.**

Run: `pnpm vitest run src/cockpit/CockpitSidebar.test.tsx` then `pnpm exec tsc --noEmit && pnpm vitest run && pnpm build`

- [ ] **Step 5: Commit**

```bash
git -C "$WT" branch --show-current
git -C "$WT" add frontend/src/cockpit/CockpitSidebar.tsx frontend/src/cockpit/CockpitSidebar.module.css frontend/src/cockpit/CockpitSidebar.test.tsx
git -C "$WT" -c commit.gpgsign=false commit -m "feat(cockpit): References + Criteria sidebar groups

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Sidebar — Versions + Members groups

**Files:**
- Modify: `frontend/src/cockpit/CockpitSidebar.tsx`, `frontend/src/cockpit/CockpitSidebar.test.tsx`

**Interfaces:**
- Consumes: `useVersions`, `useMembers`, `useCockpitMeta` (`../api/workspace`).
- Produces: Versions group (commit `message` + `created_by` muted); Members group — owners see the named list (`useMembers`); a non-owner member (403 on `/members`) sees only `Members (member_count)` from `cockpit-meta`.

- [ ] **Step 1: Implement.** In `frontend/src/cockpit/CockpitSidebar.tsx` add the imports and the two groups after the Criteria group (before the closing `</div>`):

Add to the import block:
```tsx
import { useCockpitMeta, useMembers, useVersions } from "../api/workspace";
```
Add inside the component body (after the `criteria` line):
```tsx
  const versions = useVersions(id);
  const versionRows = Array.isArray(versions.data) ? versions.data : [];
  const members = useMembers(id);
  const meta = useCockpitMeta(id);
  // Owners get the named list; non-owner members 403 on /members and fall back to the
  // non-PII member_count from cockpit-meta. Array.isArray guard keeps it robust (and the
  // 3a draft-fixture mock → memberRows=null → the count branch, never a .map crash).
  const memberRows = Array.isArray(members.data) ? members.data : null;
  const memberCount = meta.data?.member_count;
```
Add the two `SidebarGroup`s (after the Criteria group's closing `</SidebarGroup>`):
```tsx
      <SidebarGroup title="Versions" count={versionRows.length || undefined} defaultOpen={false}>
        {versionRows.length === 0 && <p className={styles.note}>No versions yet.</p>}
        <ul className={styles.outline} aria-label="Versions">
          {versionRows.map((v) => (
            <li key={v.commit_sha} className={styles.metaRow}>
              <span className={styles.rowLabel}>{v.message}</span>
              <span className={styles.metaSub}>{v.created_by}</span>
            </li>
          ))}
        </ul>
      </SidebarGroup>

      <SidebarGroup title="Members" count={memberCount} defaultOpen={false}>
        {memberRows ? (
          <ul className={styles.outline} aria-label="Members">
            {memberRows.map((m) => (
              <li key={m.email} className={styles.metaRow}>
                <span className={styles.rowLabel}>{m.email}</span>
                <span className={styles.metaSub}>{m.role}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className={styles.note}>
            {memberCount != null ? `${memberCount} member${memberCount === 1 ? "" : "s"}` : "—"}
          </p>
        )}
      </SidebarGroup>
```

- [ ] **Step 2: Add tests** — append to `frontend/src/cockpit/CockpitSidebar.test.tsx`:
```tsx
it("owner sees the named Members list and the Versions commits", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/versions"))
      return Promise.resolve({ data: [{ commit_sha: "a".repeat(40), message: "user spec init", created_by: "user", created_at: "2026-06-24T00:00:00Z" }], error: undefined });
    if (path.endsWith("/members"))
      return Promise.resolve({ data: [{ email: "owner@ki.se", role: "owner" }, { email: "m@ki.se", role: "member" }], error: undefined });
    if (path.endsWith("/cockpit-meta"))
      return Promise.resolve({ data: { workspace_title: "W", workspace_deadline: null, current_phase: "active", role: "owner", spec_locked: false, member_count: 2 }, error: undefined });
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/scorecard")) return Promise.resolve({ data: null, error: undefined });
    return Promise.resolve(draft as never);
  }) as never);
  renderSidebar();
  fireEvent.click(await screen.findByRole("button", { name: /versions/i }));
  expect(await screen.findByText("user spec init")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /members/i }));
  expect(await screen.findByText("owner@ki.se")).toBeInTheDocument();
});

it("a non-owner member sees the member count only (no emails)", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/members"))
      return Promise.resolve({ data: undefined, error: { error: { code: "forbidden", message: "owner only" } } });
    if (path.endsWith("/cockpit-meta"))
      return Promise.resolve({ data: { workspace_title: "W", workspace_deadline: null, current_phase: "active", role: "member", spec_locked: false, member_count: 3 }, error: undefined });
    if (path.endsWith("/versions")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/scorecard")) return Promise.resolve({ data: null, error: undefined });
    return Promise.resolve(draft as never);
  }) as never);
  renderSidebar();
  fireEvent.click(await screen.findByRole("button", { name: /members/i }));
  expect(await screen.findByText(/3 members/)).toBeInTheDocument();
  expect(screen.queryByText("owner@ki.se")).toBeNull();
});
```

- [ ] **Step 3: Run + gate.**

Run: `pnpm vitest run src/cockpit/CockpitSidebar.test.tsx` then `pnpm exec tsc --noEmit && pnpm vitest run && pnpm build`

- [ ] **Step 4: Commit**

```bash
git -C "$WT" branch --show-current
git -C "$WT" add frontend/src/cockpit/CockpitSidebar.tsx frontend/src/cockpit/CockpitSidebar.test.tsx
git -C "$WT" -c commit.gpgsign=false commit -m "feat(cockpit): Versions + Members sidebar groups (members owner-only, count fallback)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Status bar — deadline · spec-lock · scorecard band

**Files:**
- Modify: `frontend/src/shell/StatusBar.tsx`, `frontend/src/shell/StatusBar.module.css`, `frontend/src/shell/StatusBar.test.tsx`

**Interfaces:**
- Consumes: `cockpitIdFromPath`, `useCockpitMeta`, `useScorecard`, `deadlineView` (`../routes/Deadline`).
- Produces: the cockpit status bar gains a deadline chip (colored via the Phase-2 `deadlineView` cutoffs), a spec-lock indicator, and the overall scorecard band — alongside the Phase-3a save/Markdown/Ln-Col items.

- [ ] **Step 1: Add the CSS** — append to `frontend/src/shell/StatusBar.module.css` (colored chips legible on the teal bar):
```css
.deadlineSoon {
  background: var(--app-bad);
  color: #fff;
  border-radius: 3px;
  padding: 0 6px;
}
.deadlineWatch {
  background: var(--app-warn);
  color: #1a1b1e;
  border-radius: 3px;
  padding: 0 6px;
}
```

- [ ] **Step 2a: Wrap `renderAt` in a QueryClientProvider.** `StatusBar` now calls `useQuery` (via `useCockpitMeta`/`useScorecard`), so the existing `renderAt` helper in `frontend/src/shell/StatusBar.test.tsx` MUST provide a QueryClient or every test throws "No QueryClient set". Add the imports `import { QueryClient, QueryClientProvider } from "@tanstack/react-query";`, `import * as clientMod from "../api/client";`, and `vi` (from vitest), and replace `renderAt` with (retry off so a failing/unmocked query doesn't retry-spam):
```tsx
function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <StatusBar />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
```
The existing 3a tests stay green: off-cockpit the gated queries don't fire; the cockpit Ln/Col test leaves `meta`/`scorecard` undefined (deadline/lock/band simply absent) while the asserted save/Markdown/Ln-Col items still render.

ALSO fix `frontend/src/shell/AppShell.test.tsx` the same way — it renders the whole shell (so it now mounts a `useQuery`-calling `StatusBar`/`TitleBar`) but wraps only `MantineProvider`+`RouterProvider`. Add `import { QueryClient, QueryClientProvider } from "@tanstack/react-query";` and wrap BOTH render helpers' `<MantineProvider>…</MantineProvider>` trees in `<QueryClientProvider client={new QueryClient()}>…</QueryClientProvider>`. (It renders at `/`, so the gated queries never fire — but `useQuery` still requires the provider.) This is part of Task 7 so the gate stays green; Task 8's `TitleBar` change is then already covered.

- [ ] **Step 2b: Write the failing tests** — append to `frontend/src/shell/StatusBar.test.tsx`:
```tsx
it("on the cockpit shows the deadline, spec-lock, and scorecard band", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/cockpit-meta"))
      return Promise.resolve({ data: { workspace_title: "W", workspace_deadline: "2099-01-01T00:00:00Z", current_phase: "active", role: "owner", spec_locked: true, member_count: 1 }, error: undefined });
    if (path.endsWith("/scorecard"))
      return Promise.resolve({ data: { scorecard: { criteria: [], overall_band: "adequate", summary: "" }, evaluated_draft_sha: null, evaluated_spec_sha: null, completed_at: null, stale: false, stale_reasons: [] }, error: undefined });
    return Promise.resolve({ data: null, error: undefined });
  }) as never);
  renderAt("/cockpit/ws-1");
  expect(await screen.findByText(/Spec locked/i)).toBeInTheDocument();
  expect(await screen.findByText(/Adequate/)).toBeInTheDocument();
  expect(screen.getByText(/2099-01-01/)).toBeInTheDocument();
});

it("off the cockpit it does NOT fetch cockpit-meta / scorecard", () => {
  const get = vi.spyOn(clientMod.api, "GET");
  renderAt("/");
  expect(get).not.toHaveBeenCalled();
});
```

- [ ] **Step 3: Run — Expected: FAIL** (no deadline/lock/band yet; off-cockpit may already pass but the cockpit one fails).

Run: `pnpm vitest run src/shell/StatusBar.test.tsx`

- [ ] **Step 4: Implement** `frontend/src/shell/StatusBar.tsx`:
```tsx
import { useLocation } from "react-router-dom";

import { useScorecard } from "../api/evaluator";
import { useCockpitMeta } from "../api/workspace";
import { cockpitIdFromPath } from "../cockpit/cockpitNav";
import { type SaveState, useEditorStore } from "../cockpit/editorStore";
import { deadlineView } from "../routes/Deadline";
import styles from "./StatusBar.module.css";

const SAVE_LABEL: Record<SaveState, string> = {
  idle: "",
  saving: "Saving…",
  saved: "Saved",
  dirty: "Unsaved",
  error: "Save failed",
};
const BAND_LABEL: Record<string, string> = {
  strong: "Strong",
  adequate: "Adequate",
  at_risk: "At-risk",
  gap: "Gap",
};
const DEADLINE_CLASS: Record<string, string | undefined> = {
  soon: styles.deadlineSoon,
  watch: styles.deadlineWatch,
};

// The global status bar gains cockpit context on /cockpit: workspace deadline, spec-lock,
// scorecard band (this plan) + save-state, language, Ln/Col (Phase 3a). It derives the
// workspace id from the URL and gates its queries (enabled:!!id), so it never fetches off
// the cockpit — the same global-channel discipline the rest of the cockpit uses.
export function StatusBar() {
  const { pathname } = useLocation();
  const id = cockpitIdFromPath(pathname);
  const onCockpit = !!id;
  const { cursor, save } = useEditorStore();
  const meta = useCockpitMeta(id).data;
  const scorecard = useScorecard(id).data;
  const dl = meta?.workspace_deadline ? deadlineView(meta.workspace_deadline) : null;
  const band = scorecard?.scorecard.overall_band;

  return (
    <footer className={styles.statusbar} aria-label="Status bar">
      <span className={styles.item}>Call Manager</span>
      {onCockpit && dl && (
        <span className={`${styles.item} ${DEADLINE_CLASS[dl.state] ?? ""}`}>
          {dl.state === "closed" ? `Closed · ${dl.date}` : `Due ${dl.date}`}
        </span>
      )}
      {onCockpit && meta?.spec_locked && <span className={styles.item}>🔒 Spec locked</span>}
      {onCockpit && band && <span className={styles.item}>Band: {BAND_LABEL[band] ?? band}</span>}
      <span className={styles.spacer} />
      {onCockpit && (
        <>
          {SAVE_LABEL[save] && <span className={styles.item}>{SAVE_LABEL[save]}</span>}
          <span className={styles.item}>Markdown</span>
          {cursor && (
            <span className={styles.item}>
              Ln {cursor.line}, Col {cursor.col}
            </span>
          )}
        </>
      )}
    </footer>
  );
}
```

- [ ] **Step 5: Run — Expected: PASS.** Then full gate.

Run: `pnpm vitest run src/shell/StatusBar.test.tsx` then `pnpm exec tsc --noEmit && pnpm vitest run && pnpm build`

- [ ] **Step 6: Commit**

```bash
git -C "$WT" branch --show-current
git -C "$WT" add frontend/src/shell/StatusBar.tsx frontend/src/shell/StatusBar.module.css frontend/src/shell/StatusBar.test.tsx frontend/src/shell/AppShell.test.tsx
git -C "$WT" -c commit.gpgsign=false commit -m "feat(shell): status bar deadline + spec-lock + scorecard band on the cockpit

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Title bar — workspace-title breadcrumb

**Files:**
- Modify: `frontend/src/shell/TitleBar.tsx`
- Create: `frontend/src/shell/TitleBar.test.tsx`

**Interfaces:**
- Consumes: `cockpitIdFromPath`, `useCockpitMeta`.
- Produces: on `/cockpit`, the title shows the workspace title (falling back to "Proposal workspace" until it loads); elsewhere "Funding calls".

- [ ] **Step 1: Write the failing test** `frontend/src/shell/TitleBar.test.tsx`:
```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { TitleBar } from "./TitleBar";

function renderAt(path: string) {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <TitleBar />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

it("shows the workspace title on the cockpit", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { workspace_title: "AMR surveillance grant", workspace_deadline: null, current_phase: "active", role: "owner", spec_locked: false, member_count: 1 },
    error: undefined,
  } as never);
  renderAt("/cockpit/ws-1");
  expect(await screen.findByText("AMR surveillance grant")).toBeInTheDocument();
});

it("shows 'Funding calls' off the cockpit and does not fetch", () => {
  const get = vi.spyOn(clientMod.api, "GET");
  renderAt("/");
  expect(screen.getByText("Funding calls")).toBeInTheDocument();
  expect(get).not.toHaveBeenCalled();
});
```

- [ ] **Step 2: Run — Expected: FAIL** (title is the static "Proposal workspace").

Run: `pnpm vitest run src/shell/TitleBar.test.tsx`

- [ ] **Step 3: Implement** `frontend/src/shell/TitleBar.tsx`:
```tsx
import { useLocation } from "react-router-dom";

import { useCockpitMeta } from "../api/workspace";
import { cockpitIdFromPath } from "../cockpit/cockpitNav";
import { BrandMark } from "./BrandMark";
import styles from "./TitleBar.module.css";

export function TitleBar() {
  const { pathname } = useLocation();
  const id = cockpitIdFromPath(pathname);
  const meta = useCockpitMeta(id).data;
  // On the cockpit, the breadcrumb is the workspace title (falls back until it loads);
  // off the cockpit it's the dashboard label. useCockpitMeta is gated (id===""), so the
  // off-cockpit branch never fetches.
  const title = id ? (meta?.workspace_title ?? "Proposal workspace") : "Funding calls";
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

- [ ] **Step 4: Run — Expected: PASS.** Then full gate.

Run: `pnpm vitest run src/shell/TitleBar.test.tsx` then `pnpm exec tsc --noEmit && pnpm vitest run && pnpm build`

- [ ] **Step 5: Commit**

```bash
git -C "$WT" branch --show-current
git -C "$WT" add frontend/src/shell/TitleBar.tsx frontend/src/shell/TitleBar.test.tsx
git -C "$WT" -c commit.gpgsign=false commit -m "feat(shell): title-bar breadcrumb shows the workspace title on the cockpit

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (after Task 8)

- [ ] **Backend (targeted, NOT whole-suite):** `uv run pytest tests/test_api_workspaces.py -k "cockpit_meta or versions or members" -v` — all green. (Re-run any flaky failure in isolation per the project-memory testcontainers caveat.)
- [ ] **Frontend whole gate** from `frontend/`: `pnpm exec tsc --noEmit` + `pnpm vitest run` + `pnpm build`, all green.
- [ ] **Codegen drift:** `git -C "$WT" diff --stat` shows `schema.gen.ts` was regenerated (committed in Task 4) and nothing else stray.
- [ ] **Visual check** (per `reference_spa_visual_check`): backendless `pnpm build` + static-serve; mock `/api/workspaces/*/{draft,spec,problems,proposed-changes,references,scorecard,versions,members,cockpit-meta}` via Playwright `page.route`; screenshot `/cockpit/ws-1`. Confirm: the five collapsible sidebar groups (Outline · References · Criteria w/ band pills · Versions · Members), and the teal status bar showing the deadline chip + 🔒 Spec locked + Band + Markdown + Ln/Col, and the title-bar workspace breadcrumb.

## STOP-BEFORE-MERGE (for a detached run)

Implement, review, fix, and COMMIT all 8 tasks on `feat/spa-vscode-redesign-phase3b`; run the final whole-branch review; record findings. Then STOP — do NOT merge, rebase, push, or touch the main working tree. The human verifies independently (backend pytest + frontend gate + visual) and merges on reconnect.
