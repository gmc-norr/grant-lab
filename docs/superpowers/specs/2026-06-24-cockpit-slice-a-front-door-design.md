# Cockpit Cutover Slice A — Front Door (React workspace list + creation)

**Date:** 2026-06-24
**Status:** Design — ready for plan
**Sub-project:** Cockpit cutover (sub-project 1 of the full-React migration)
**Builds on:** Slice B chat (merged `ddc6e3a`)

## 1. Context & goal

The web app is mid-migration from Jinja2/HTMX to a single React SPA via a
strangler-fig, route-by-route cutover (the migration decision and slice
breakdown are recorded in the `project_full_react_migration` memory; the
preceding slice is `docs/superpowers/specs/2026-06-24-cockpit-chat-slice-design.md`).
The React cockpit (`/cockpit/:id`) is built and ahead of the Jinja workspace
on most surfaces, but it is **dark-launched**: the "Workspaces" nav item still
points at the Jinja `/workspaces` two-pane UI, and there is no React way to
**list** or **create** a workspace. Every path into the cockpit currently runs
through Jinja.

**Slice A makes the cockpit the workspace hub.** It adds a React workspace
**list** at `/cockpit`, a React **create** flow that starts from a ranked call,
and flips the nav so "Workspaces" lands in React. After Slice A, browsing and
creating workspaces is React; only the **setup → rubric-review** activation
phase (Slice E) stays in Jinja for now.

Goal in one sentence: *a user can see their workspaces and start a new one from
a call without touching a Jinja page, except the setup/rubric step that Slice E
will migrate.*

## 2. Scope

**In scope**
- `GET /api/workspaces` — list workspaces visible to the user (additive, no schema change).
- `POST /api/workspaces` — create a workspace from a call (additive, reuses `create_workspace`).
- React route `/cockpit` (index) → `WorkspaceList` + a sidebar status filter.
- React route `/cockpit/new` → `WorkspaceCreate` (from-call form: title + optional deadline).
- A "Start drafting" affordance on the React Dashboard `CallRow`.
- Nav flip: "Workspaces" ActivityBar item → SPA `/cockpit`.

**Out of scope (deferred to later slices / sub-projects)**
- The setup-status / rubric-review activation UI → **Slice E** (stays Jinja).
- Standalone (no-call) workspace creation. A call is required for Phase-0, and
  every entry point we control (the dashboard) has a call in hand. Not built.
- Workspace settings, viewer/collaborator management → **Slice D**.
- A lightweight `/api/calls` options endpoint or `/api/profiles` — **not needed**
  (from-call creation gets `call_id` + title from the dashboard row, and the
  dashboard's already-resolved `effective.profile_id` for the optional profile).
- Soft-delete / rename / status-change actions from the list.
- Migrating the Jinja `/workspaces*` routes — they keep working (coexistence
  until Slice G teardown).

## 3. Architecture overview

```
Dashboard CallRow ──"Start drafting"──▶ /cockpit/new?call_id=&call_title=
                                              │
                                       WorkspaceCreate (title + deadline)
                                              │ POST /api/workspaces {call_id,title,deadline?,profile_id?}
                                              ▼
                                    create_workspace() → status=drafting,
                                    current_phase=setup, Phase-0 job enqueued
                                              │ returns {id}
                                              ▼
                              window.location → Jinja /workspaces/{id}
                                    (setup status → rubric review → activate)   [Slice E territory]

/cockpit (WorkspaceList)  ◀── nav "Workspaces"
   ├─ active row   → /cockpit/{id}        (React cockpit, router Link)
   └─ setup  row   → /workspaces/{id}     (Jinja setup flow, <a href>, full load)
```

Three global channels keep state coherent across the AppShell boundary, exactly
as the Dashboard/cockpit already do: the **URL** holds the list's `?status`
filter; **React Query** caches the list (`["workspaces", status]`) so the list
body and the sidebar filter share one fetch; the create form is plain local
state (no cross-boundary coupling).

`/cockpit`, `/cockpit/new`, and `/cockpit/{uuid}` are all already SPA-served:
`SPA_ROUTES = ("/cockpit", "/")` and `is_spa_route` matches any sub-path beneath
`/cockpit`. **No server routing/serving change, and no `routes.gen.ts` manifest
change** (the manifest carries the `/cockpit` *prefix*, not param paths — do not
touch it).

## 4. Routing & navigation

### 4.1 Router (`frontend/src/router.tsx`)

Add two child routes under the existing AppShell layout, beside the current
`cockpit/:id`:

```tsx
{ index: true, element: <Dashboard />, handle: { Sidebar: CallsFilters } },
{ path: "cockpit", element: <WorkspaceList />, handle: { Sidebar: WorkspacesFilters } },
{ path: "cockpit/new", element: <WorkspaceCreate /> },
{ path: "cockpit/:id", element: <Cockpit />, handle: { Sidebar: CockpitSidebar, RightPane: CockpitChat } },
{ path: "*", element: <NotFound/> },
```

**Route ranking:** the static segment `cockpit/new` must out-rank the dynamic
`cockpit/:id` so `/cockpit/new` never resolves to the editor with `id="new"`.
react-router v7 ranks static segments above dynamic ones automatically, so this
holds regardless of source order — but a router test pins it (`/cockpit/new` →
WorkspaceCreate, not Cockpit).

`/cockpit/new` deliberately has **no** `handle.Sidebar`/`handle.RightPane` — the
create form owns the full main area; the shell renders its empty sidebar region.

### 4.2 Nav flip (`frontend/src/shell/nav-items.ts`)

Flip the one "workspaces" item from a Jinja deep-link to an in-app SPA link:

```ts
{ id: "workspaces", label: "Workspaces", icon: "folder", kind: "spa", to: "/cockpit" },
```

ActivityBar already renders `kind:"spa"` as a react-router `NavLink` and
`kind:"external"` as an `<a href>`; no nav-items change beyond this flip. (The
dynamic active-cockpit chip is corrected in §4.3.)

### 4.3 Shell chrome for non-editor cockpit routes

The always-mounted shell chrome (TitleBar, StatusBar, ActivityBar) derives "are
we in a workspace editor?" from the URL. Today that derivation assumes every
`/cockpit/*` path is an editor, which the new list/create routes break:

- `cockpitIdFromPath` (`frontend/src/cockpit/cockpitNav.ts`) matches
  `/^\/cockpit\/([^/]+)/`, so `/cockpit/new` returns `"new"` — TitleBar's
  `useCockpitMeta("new")` and StatusBar's `useScorecard("new")` would fire queries
  against a non-existent workspace. Add a reserved-segment guard so it returns
  `""` for non-id subpaths (the bare `/cockpit` list already returns `""` — no
  match):

  ```ts
  // static /cockpit/* subpaths that are NOT workspace ids
  const COCKPIT_RESERVED = new Set(["new"]);
  export function cockpitIdFromPath(pathname: string): string {
    const m = pathname.match(/^\/cockpit\/([^/]+)/);
    if (!m) return "";
    return COCKPIT_RESERVED.has(m[1]) ? "" : m[1];
  }
  ```

  (Reserved-set, not a UUID regex, on purpose: it stays green for existing tests
  that mount the cockpit with non-UUID ids, and adding a future static subpath is
  a one-line change.)

- **TitleBar** must title the three cockpit routes distinctly (it currently has
  only editor-vs-dashboard): editor (`id` set) → workspace title; `/cockpit/new`
  → "New workspace"; `/cockpit` → "Workspaces"; `/` → "Funding calls".

- **StatusBar** already gates its editor chrome (deadline / spec-lock / band /
  save-state / Ln-Col) on `onCockpit = !!id`. Once `cockpitIdFromPath` returns
  `""` for the list/create routes, that chrome correctly disappears there — no
  StatusBar change beyond the helper fix.

- **ActivityBar** currently renders the "Current workspace" chip whenever
  `pathname.startsWith("/cockpit")` — i.e. also on the list and create routes.
  Gate it on the real workspace id instead, and point it at the canonical editor
  path:

  ```tsx
  const id = cockpitIdFromPath(pathname);
  ...
  {id && <Item item={{ id: "active-ws", label: "Current workspace",
                        icon: "edit", kind: "spa", to: `/cockpit/${id}` }} />}
  ```

  After the nav flip (§4.2), the "Workspaces" item must NOT rely on plain NavLink
  prefix-matching: that marks it active across the whole `/cockpit` subtree, so on
  `/cockpit/:id` BOTH it and the Current-workspace chip read active (two
  `aria-current="page"` items). Drive its active state explicitly instead — active
  on the non-editor cockpit routes (`pathname.startsWith("/cockpit") &&
  cockpitIdFromPath(pathname) === ""`), inactive on the editor (where the chip owns
  it).

- **Drift guard** (`frontend/src/router.test.tsx`): the manifest (`routes.gen.ts`)
  is **unchanged**, but the guard's reduction only collapses `/:param` paths, so
  the new static `/cockpit/new` leaf would not reduce to `/cockpit` and the test
  would fail. Generalize the reduction to map each concrete leaf to its owning
  manifest prefix (mirroring `is_spa_route`'s subtree match), then assert
  set-equality with `SPA_ROUTES`:

  ```ts
  function toOwningPrefix(p: string): string {
    const owner = SPA_ROUTES
      .filter((r) => (r === "/" ? p === "/" : p === r || p.startsWith(r + "/")))
      .sort((a, b) => b.length - a.length)[0];
    return owner ?? p; // unowned -> stays itself -> set mismatch -> guard fails
  }
  // expect(new Set(concrete.map(toOwningPrefix))).toEqual(new Set(SPA_ROUTES));
  ```

  This keeps both guard directions: an unowned route maps to itself (absent from
  `SPA_ROUTES` → fail), and every manifest entry must own ≥1 concrete leaf.

## 5. Backend

Both endpoints are added to the **existing** `src/call_manager/web/api/workspaces.py`
(the cockpit JSON surface), whose `router` is already mounted under `/api`. They
are **collection** routes (`/workspaces`, no `{workspace_id}` segment), so they
do not collide with the existing `/workspaces/{workspace_id}/...` routes. The
runtime error envelope (`{error:{code,message,detail?}}`) is produced by the
global HTTPException handler; handlers just `raise HTTPException`.

New imports needed in that module: `Call`, `Profile`, `Workspace`,
`WorkspaceSetupJob` (test only) from `db.models`; `or_` from `sqlalchemy`;
`create_workspace` from `call_manager.workspaces`. The three constants and the
deadline parser are
defined **locally** in this module (the api layer must not import the heavy
Jinja `proposal_assist.web.workspaces`); a comment ties `_WORKSPACE_STATUSES`
to the `ck_workspaces_status` CHECK constraint as the single source of truth.

```python
# Mirror ck_workspaces_status (db/models.py). Allowlist for the ?status filter.
_WORKSPACE_STATUSES = frozenset({"drafting", "submitted", "awarded", "rejected", "abandoned"})
_MAX_WORKSPACE_TITLE_LEN = 500
_WORKSPACE_LIST_LIMIT = 200
```

### 5.1 `GET /api/workspaces`

Lists workspaces the user owns OR is a viewer of, not soft-deleted, most-recently
updated first, capped at `_WORKSPACE_LIST_LIMIT`. Mirrors the Jinja
`list_workspaces` query exactly. Optional `?status=` filter, allowlisted against
`_WORKSPACE_STATUSES` (unknown → 400, not a silent empty list).

```python
class WorkspaceListItemOut(BaseModel):
    id: str
    title: str
    status: str            # lifecycle: drafting|submitted|awarded|rejected|abandoned
    current_phase: str     # "setup" | "active"
    deadline: datetime | None
    updated_at: datetime
    linked_call_title: str | None   # None if call_id is null (call deleted -> SET NULL)
    is_owner: bool

@router.get("/workspaces", response_model=list[WorkspaceListItemOut])
def list_workspaces_api(request, status: str | None = None, user = Depends(get_current_user)):
    if status is not None and status not in _WORKSPACE_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status filter")
    with sf() as session:
        viewer_ids = session.scalars(
            select(WorkspaceViewer.workspace_id).where(WorkspaceViewer.user_id == user.id)
        ).all()
        q = (select(Workspace)
             .where(Workspace.soft_deleted_at.is_(None),
                    or_(Workspace.owner_id == user.id, Workspace.id.in_(viewer_ids)))
             .order_by(Workspace.updated_at.desc())
             .limit(_WORKSPACE_LIST_LIMIT))
        if status:
            q = q.where(Workspace.status == status)
        rows = list(session.scalars(q))
        # Batch the linked-call titles in ONE query (no N+1); a row whose call was
        # deleted (call_id SET NULL) has no title.
        call_ids = {w.call_id for w in rows if w.call_id is not None}
        titles = dict(session.execute(
            select(Call.id, Call.title).where(Call.id.in_(call_ids))
        ).all()) if call_ids else {}
        return [WorkspaceListItemOut(
            id=str(w.id), title=w.title, status=w.status, current_phase=w.current_phase,
            deadline=w.deadline, updated_at=w.updated_at,
            linked_call_title=titles.get(w.call_id), is_owner=(w.owner_id == user.id),
        ) for w in rows]
```

Any authenticated user may call it (lists only their own + viewer-of); no
`require_member`/`require_owner` (those are per-workspace gates).

### 5.2 `POST /api/workspaces`

From-call creation. Reuses `create_workspace` (inserts the row with
`current_phase` defaulting to `setup`, writes the `created` audit event, enqueues
the Phase-0 `WorkspaceSetupJob`). The handler validates first so a bad id is a
clean 400, not a foreign-key 500 — mirroring the Jinja `post_new_workspace`.

```python
class WorkspaceCreateIn(BaseModel):
    call_id: int
    title: str
    deadline: str | None = None    # ISO-8601; parsed + validated server-side
    profile_id: int | None = None  # the dashboard's resolved effective profile

class WorkspaceCreatedOut(BaseModel):
    id: str

@router.post("/workspaces", response_model=WorkspaceCreatedOut, status_code=201)
def create_workspace_api(body: WorkspaceCreateIn, request, user = Depends(get_current_user)):
    title = body.title.strip()
    if not title or len(title) > _MAX_WORKSPACE_TITLE_LEN:
        raise HTTPException(status_code=400, detail="invalid title")
    deadline = _parse_deadline(body.deadline)   # None | datetime | raise 400
    with sf() as session:
        if session.get(Call, body.call_id) is None:
            raise HTTPException(status_code=400, detail=f"No such call: {body.call_id}")
        if body.profile_id is not None and session.get(Profile, body.profile_id) is None:
            raise HTTPException(status_code=400, detail=f"No such profile: {body.profile_id}")
        ws = create_workspace(session=session, owner_id=user.id, title=title,
                              call_id=body.call_id, profile_id=body.profile_id)
        if deadline:
            ws.deadline = deadline
        session.commit()
        new_id = str(ws.id)
    return WorkspaceCreatedOut(id=new_id)
```

`_parse_deadline(s)` returns `None` for empty/None, a `datetime` for valid
ISO-8601, and raises `HTTPException(400, "invalid deadline (expected ISO-8601)")`
otherwise (copy of the Jinja `_parse_deadline_form` body, sans the `Form`
coupling). **Profile** is the dashboard's resolved `effective.profile_id` when
present (validated to exist or 400, mirroring the Jinja create); `create_workspace`
threads it onto the row, and Phase-0 snapshots its description
(`profile_desc_snapshot`, `setup.py`) to feed rubric/section extraction — so
carrying it preserves drafting quality parity with the current Jinja "Start
drafting" flow. `None` is fine (no profile selected).

Returns **201 Created**. openapi-fetch treats any 2xx as success; the codegen
adds a 201 response shape. Read `str(ws.id)` *before* the `with` block closes
(the ORM object is bound to that session).

### 5.3 OpenAPI codegen

Regenerate `frontend/src/api/schema.gen.ts` with
`EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh` (the `bge-m3` prefix
is mandatory — the `.env` mxbai default logs a stdout WARNING that corrupts the
file). The diff must be **additive only**: the two new `/api/workspaces` paths
(GET list, POST create) and their schemas.

## 6. Frontend

### 6.1 API hooks (`frontend/src/api/workspace.ts`)

Add collection hooks beside the existing per-workspace ones. The list query key
is shared by the list body and the sidebar filter (React Query dedupes):

```ts
export function useWorkspaces(status: string | null) {
  return useQuery({
    queryKey: ["workspaces", status ?? ""],
    queryFn: async () => unwrap(await api.GET("/api/workspaces", {
      params: { query: status ? { status } : {} },
    })),
  });
}

export function useCreateWorkspace() {
  return useMutation({
    mutationFn: async (vars: {
      call_id: number; title: string; deadline?: string | null; profile_id?: number | null;
    }) => unwrap(await api.POST("/api/workspaces", { body: vars })),
  });
}
```

(No `onSettled` invalidation needed: after create we leave the SPA for the Jinja
setup page; the list refetches naturally on next mount.)

### 6.2 `WorkspaceList` (`frontend/src/routes/WorkspaceList.tsx` + `.module.css`)

Main-area list mirroring the Dashboard's row-list shape. Reads `?status` from the
URL, calls `useWorkspaces(status)`. Renders a `<ul>` of `WorkspaceRow`. Shows a
"New workspace" affordance that routes to the Dashboard (`/`) — *"pick a call to
start drafting"* — because creation is from-call. Empty state: "No workspaces yet
— pick a call to start drafting" linking to `/`. Loading/error states mirror the
Dashboard (`Loading…` / `role="alert"`).

`WorkspaceRow` (can be inline or its own file): title, linked-call line, a status
badge, a themed `Deadline`, a "shared" marker when `!is_owner`, and last-activity
(`updated_at`). The row's primary link is **phase-aware**:
- `current_phase === "active"` → react-router `<Link to={`/cockpit/${id}`}>` (React cockpit).
- `current_phase === "setup"` → `<a href={`/workspaces/${id}`}>` (Jinja setup flow, full load) with a visible "Setting up" marker.

### 6.3 `WorkspacesFilters` (`frontend/src/routes/WorkspacesFilters.tsx` + `.module.css`)

Sidebar filter declared via `handle.Sidebar`, mirroring `CallsFilters`: a Mantine
`Select` of the five lifecycle statuses (plus "All"). Binds to the URL `?status`
(any change updates `useSearchParams`); renders independently of the list data.

### 6.4 `WorkspaceCreate` (`frontend/src/routes/WorkspaceCreate.tsx` + `.module.css`)

The `/cockpit/new` create form. Reads `call_id`, `call_title`, and the optional
`profile_id` from `useSearchParams`:
- **`call_id` present:** show a "Drafting for: {call_title}" context line, a Title
  input pre-filled with `call_title` (editable, required), and an optional
  Deadline input (`type="datetime-local"` → ISO string). Submit calls
  `useCreateWorkspace().mutateAsync({ call_id, title, deadline, profile_id })`
  (forwarding `profile_id` straight through, unparsed in the UI), then
  `window.location.assign(`/workspaces/${id}`)` to hand off to the Jinja setup
  flow (a full-page navigation — `/workspaces` is not SPA-served). Disable the
  submit while the mutation is pending; surface a 400 inline.
- **`call_id` absent** (direct hit / refresh without params): render a guard —
  "Pick a call from the dashboard to start drafting" with a link to `/` — instead
  of a broken form. Creation requires a call.

### 6.5 Dashboard `CallRow` (`frontend/src/routes/CallRow.tsx`)

Add a "Start drafting" action to each call row — a react-router `<Link>` to
`/cockpit/new?call_id=${call.id}&call_title=${encodeURIComponent(call.title)}`,
plus `&profile_id=${profileId}` when the dashboard has a resolved profile.
`CallRow` gains a `profileId?: number | null` prop; `Dashboard` passes
`data.effective.profile_id` — the profile the visible rankings are computed
against, i.e. the natural drafting context (and parity with the Jinja
`best_profile_id` carried by `call_detail.html`'s "Start drafting"). The existing
title link (Jinja `/calls/${id}`) is unchanged; `CallOut` already carries `id` +
`title`, so no new fetch.

## 7. Data flow — the setup→rubric interim

A newly created workspace is `status=drafting`, `current_phase=setup`, with its
Phase-0 job queued; its git repo and `proposal.md` **do not exist yet**, so the
React cockpit editor cannot render it. The whole setup → extract-rubric →
rubric-review → activate experience is **Slice E**, still Jinja.

Therefore, **until Slice E**:
- **Create lands in Jinja** `/workspaces/{id}` (the live setup-status + rubric
  review flow). This is where the user must be to activate the workspace.
- **The React list deep-links setup-phase rows** to the same Jinja `/workspaces/{id}`,
  marked "Setting up". Active rows open the React `/cockpit/{id}`.

This is the same setup-is-Jinja interim already accepted for the chat slice
(Slice B). When Slice E lands, both link targets flip to React and this section
is deleted. The interim is explicit and self-contained — no throwaway React
setup stub is built now.

## 8. Error handling

- `GET /api/workspaces`: unknown `?status` → **400** `invalid status filter`.
- `POST /api/workspaces`: empty/too-long title → **400** `invalid title`;
  unknown `call_id` → **400** `No such call: {id}`; malformed deadline → **400**
  `invalid deadline (expected ISO-8601)`. All via the standard envelope.
- Frontend: list loading/error mirror the Dashboard; the create form disables
  submit while pending and renders the 400 `message` inline. A non-2xx create
  leaves the user on the form (no navigation).
- No 409/429/503 paths: creation does not touch the commit mutex or the cloud
  gate (Phase-0 runs async in the worker).

## 9. Testing

**Backend** (`tests/test_api_workspaces.py`, reuse `_app`/`_seed`/`_client`):
- `GET /api/workspaces` returns owned + viewer-of, excludes soft-deleted, orders
  by `updated_at` desc, and each item carries `linked_call_title`,
  `current_phase`, `is_owner`.
- `?status=submitted` filters; `?status=bogus` → 400.
- `POST /api/workspaces` → **201**, returns an id, persists `current_phase="setup"`,
  and enqueues exactly one `WorkspaceSetupJob`.
- `POST` validation: empty title → 400; title > 500 chars → 400; missing/unknown
  `call_id` → 400; malformed `deadline` → 400; valid `deadline` is stored.
- `POST` with a valid `profile_id` stores it on the row (so Phase-0 can snapshot
  the profile); an unknown `profile_id` → 400; omitting it succeeds (`None`).

**Frontend** (vitest + Testing Library):
- `useWorkspaces` / `useCreateWorkspace` hooks (mock `api.GET`/`api.POST`).
- `WorkspaceList`: renders rows; an active row links to `/cockpit/{id}`, a setup
  row links to `/workspaces/{id}` and shows "Setting up"; empty state links to `/`.
- `WorkspaceCreate`: prefilled title from `?call_title`; submit calls create
  (forwarding `?profile_id` into the body) then navigates (assert on a stubbed
  `window.location.assign`); missing `call_id` shows the guard, not a form.
- `WorkspacesFilters`: changing the select updates the URL `?status`.
- `CallRow`: the "Start drafting" link targets
  `/cockpit/new?call_id=…&call_title=…`, and includes `&profile_id=…` when a
  `profileId` prop is supplied (and omits it when not).
- Router: `/cockpit` → WorkspaceList, `/cockpit/new` → WorkspaceCreate (out-ranks
  `:id`), `/cockpit/{uuid}` → Cockpit. The `routes.gen.ts` manifest is unchanged,
  but the `router.test.tsx` drift guard is generalized to collapse each concrete
  leaf to its owning manifest prefix (so `/cockpit/new` reduces to `/cockpit`) —
  see §4.3.
- Shell chrome (§4.3): `cockpitIdFromPath("/cockpit/new")` → `""` and
  `("/cockpit")` → `""`; TitleBar shows "Workspaces" on the list, "New workspace"
  on create, and the workspace title on the editor; ActivityBar's "Current
  workspace" chip renders only on `/cockpit/:id` (a real id), not on the list or
  create routes.
- `nav-items`: the "workspaces" item is `kind:"spa"`, `to:"/cockpit"`.

Type gate: `pnpm exec tsc --noEmit`. Build: `pnpm build`.

## 10. Global constraints / gotchas (for the plan)

- **No schema change.** Alembic head stays where it is; `create_workspace` and
  the `Workspace`/`WorkspaceSetupJob` models are untouched.
- **No `routes.gen.ts` change** — `/cockpit` prefix already owns these paths; do
  not "fix" the manifest to param form (breaks prod serving). The
  `router.test.tsx` drift-guard **test** DOES change (generalize its reduction to
  collapse manifest-owned subpaths to their owning prefix; §4.3) — the manifest
  data does not.
- **Always-mounted shell chrome must tolerate the new non-editor cockpit routes**
  — fix `cockpitIdFromPath` + TitleBar titling + the ActivityBar chip together
  (§4.3), or the list/create routes fire workspace queries against `"new"`/`""`
  and mis-render the editor chip.
- **OpenAPI regen requires `EMBEDDING_MODEL=bge-m3`** (mxbai default corrupts the
  output via a stdout WARNING). Diff must be additive only.
- **api layer must not import `proposal_assist.web.workspaces`** — redefine the
  three small constants + `_parse_deadline` locally in `web/api/workspaces.py`.
- **Backend tests:** run only the targeted `tests/test_api_workspaces.py` ids —
  never the whole suite (testcontainers Postgres drops connections under
  whole-suite load → false infra failures). `uv run ruff check/format` touched
  files (F401 fails the gate — import only what's used).
- **Frontend:** Node 22 via nvm + pnpm; type gate is `tsc --noEmit` (no eslint);
  `pnpm vitest run <file>`; any test mounting a cockpit component resets the
  module-global `editorStore` in `afterEach` (not relevant to the new list/create
  components, which don't touch it).
- **`window.location.assign` to `/workspaces/{id}`** is a deliberate full-page
  hand-off to Jinja (that route is not SPA-served) — not a react-router navigate.
```
