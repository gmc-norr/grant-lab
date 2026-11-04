# Plan: remove the frontend edition split, browser tests, CI and docs

**Status.** Repo is at `388494c8` (READ). I edited nothing on disk.

**How I checked the source edits.** I applied every edit in §1 in memory with a `node` script and type-checked the resulting `src/` tree with the repo's own TypeScript and `tsconfig.json` (MEASURED):
- 126 root files.
- One diagnostic: an unused `fireEvent` import. The fix is in §1.4 (`CallDetail.test.tsx` line 3).
- `vite.config.ts` and `playwright.config.ts` below: 0 diagnostics.
- `smoke.spec.ts`: 1 diagnostic, `AxeBuilder({ page })` Page type mismatch. `dashboard.spec.ts` has the same call; the cause is two `playwright-core` versions in the lockfile. `e2e/` is type-checked nowhere (READ, `test_match_gates_are_wired.py` "pin 3").

**Labels.** READ = seen in the source. MEASURED = I ran it. INFERRED = reasoned, not run.

**Line numbers** refer to `388494c8`. Run all commands in the worktree `.worktrees/workspace-removal`, never in the main tree. The live app serves the main tree's `src/grant_lab/web/spa`.

---

## 1. Frontend edition removal

### 1.1 Router decision and every import to update (READ + MEASURED)

`router.simple.tsx` becomes `router.tsx`. `router.tsx` (full), `virtual-router.d.ts`, `virtual-router.test.ts`, `edition.ts` and `edition.test.ts` go. The `virtual:app-router` alias goes.

Imports to update:
- `App.tsx`: `import { router } from "virtual:app-router"` becomes `import { router } from "./router"`.
- `router.test.tsx` (moved from `router.simple.test.tsx`): `import { simpleRouter } from "./router.simple"` becomes `import { router } from "./router"`. Replace the 5 uses of `simpleRouter.routes` with `router.routes`.
- `routes/AdminNav.test.tsx`: `await import("../router.simple")` becomes `await import("../router")`.
- `routes/CallDetail.tsx`: `CallDetailBody` becomes `MatchCallOverview`. `CallDetailBody` only ever returned `MatchCallOverview` in match (READ, line 56), so it is deleted.
- `routes/MatchCallOverview.test.tsx` (moved from `CallDetailBody.test.tsx`): import `MatchCallOverview`.
- Only comments mention `router.simple` elsewhere: `e2e/match.spec.ts:22,327,535`, `scripts/serve_spa_static.mjs:21`, `AdminDashboard.tsx:193`, `TriageFeed.tsx:29`, `SimpleShell.tsx:9`, `theme.match.css:14` and `routeLeafPaths.ts:7`. See §1.5.

### 1.2 Delete and move commands

MEASURED: I recomputed reachability from `main.tsx` after the edits, excluding tests, then removed tests whose subject is gone. Every path below exists (checked with `ls`).

```bash
git rm -r -q frontend/src/cockpit frontend/src/collab frontend/src/editor
git rm -q \
  frontend/src/router.tsx frontend/src/router.test.tsx \
  frontend/src/edition.ts frontend/src/edition.test.ts \
  frontend/src/virtual-router.d.ts frontend/src/virtual-router.test.ts \
  frontend/src/checkMatchBundle.test.ts frontend/src/writepath.test.ts \
  frontend/src/api/{activity,annotations,audit,branches,citations,coherence,editor,evaluator,events,export,files,invalidateArtifact,problems,proposals,references,review,rubric,settings,turns,uncitedClaims,versions,workspace}.ts \
  frontend/src/api/testHarness.tsx \
  frontend/src/api/{annotations,citations,coherence,editor,files,proposals,references,rubric,settings,turns,versions,workspace}.test.tsx \
  frontend/src/api/{createInvalidations,events,export,invalidateArtifact}.test.ts \
  frontend/src/components/MatchBadge.tsx frontend/src/components/MatchBadge.module.css frontend/src/components/MatchBadge.test.tsx frontend/src/components/Pagination.tsx \
  frontend/src/routes/{AdminDeletedWorkspaceDetail,AdminDeletedWorkspaces,AdminDrafting,CallReadingPane,CallRow,CallsFilters,Cockpit,Dashboard,DashboardHeader,WorkspaceCreate,WorkspaceList,WorkspacesFilters,CallDetailBody}.tsx \
  frontend/src/routes/Cockpit.module.css frontend/src/routes/callSelection.ts \
  frontend/src/routes/{AdminDeletedWorkspaces,AdminDrafting,CallReadingPane,CallRow,CallsFilters,Cockpit,Cockpit.error,Dashboard,DashboardHeader,WorkspaceCreate,WorkspaceList,WorkspacesFilters}.test.tsx \
  frontend/src/routes/callSelection.test.ts \
  frontend/src/shell/{ActivityBar,AppShell,Sidebar,StatusBar,TitleBar}.tsx \
  frontend/src/shell/{ActivityBar,AppShell,Sidebar,StatusBar,TitleBar}.module.css frontend/src/shell/nav-items.ts \
  frontend/src/shell/{ActivityBar,AppShell,AppShell.reveal,StatusBar,StatusBar.branch,TitleBar}.test.tsx \
  frontend/e2e/collab.spec.ts frontend/e2e/dashboard.spec.ts frontend/vitest.probe.config.ts frontend/.env.example \
  frontend/scripts/collab-schema.d.mts frontend/scripts/collab-schema.mjs frontend/scripts/gen_collab_goldens.mjs frontend/scripts/verify_python_writepath.mjs \
  scripts/check_match_bundle.mjs scripts/collab_handshake_probe.py
git mv frontend/src/router.simple.tsx frontend/src/router.tsx
git mv frontend/src/router.simple.test.tsx frontend/src/router.test.tsx
git mv frontend/src/routes/CallDetailBody.test.tsx frontend/src/routes/MatchCallOverview.test.tsx
git mv scripts/collab_e2e_run.sh scripts/e2e_run.sh
git mv scripts/collab_e2e_seed.py scripts/e2e_seed.py
```

What this removes and keeps (MEASURED):
- **Non-test files removed from `src`:** 128 (124 found by reachability, 12,308 lines, plus `router.tsx`, `edition.ts`, `virtual-router.d.ts`, `CallDetailBody.tsx`).
- **Tests removed:** 103.
- **Tests kept:** 55. After the moves, 76 modules are reachable.
- **Unreachable but kept:** `dev-proxy.ts` and `routes.gen.ts` (imported by `vite.config.ts`) and `routeLeafPaths.ts` (used by the router and AdminNav tests).
- **`src/api/`:** 22 workspace clients plus `testHarness.tsx` go. The kept clients `api/admin.ts` and `api/adminDashboard.ts` still export workspace hooks; those must be cut (§1.3), or tsc fails once `schema.gen.ts` is regenerated.
- **Theme files:** none becomes unused. `theme.css` is imported by `main.tsx`, `theme.match.css` by `SimpleShell`, `theme.ts` by `App` (READ).
- **`frontend/.env.example`:** its only line is `VITE_APP_EDITION=full`.
- **`scripts/collab_handshake_probe.py`:** collab-only; it is the only user of `vitest.probe.config.ts`.

### 1.3 Source edits

All checked by the in-memory type check.

**`src/App.tsx`.** Delete line 4 (`virtual:app-router`) and line 8 (`APP_EDITION`). Add one import after line 7, and change line 57:
```tsx
import { ErrorBoundary } from "./components/ErrorBoundary";
import { router } from "./router";
...
      <MantineProvider theme={theme} defaultColorScheme="light">
```
`App.test.tsx` checks that the file contains `export function App()` and `client={appQueryClient}`, and exactly one `new QueryClient(`. All three still hold.

**`src/main.tsx`.** Unchanged (READ: no edition reference).

**`src/router.tsx`** (edits on the old `router.simple.tsx`):
- **Lines 25-36:** replace with:
  ```
  // The SPA's only router. Route paths MUST stay inside the manifest's owned set
  // (routes.gen.ts) or a hard refresh 404s; router.test.tsx pins both directions.
  ```
- **Line 64:** `export const router = createBrowserRouter([`
- **Lines 77-81:** delete (the "EDITION SPLIT" comment).
- **Lines 123-128:** delete (golden-set history comment).
- **Lines 143-145:** replace with:
  ```
  // component="main": SimpleShell renders no landmark (every page renders
  // its own), so without this the soft-404 page would have none at all.
  ```
- **Lines 186-189:** replace with:
  ```
  // A THIRD pathless layout, sibling to SimpleShell and PreAuthLayout: the
  // full-bleed corridor board. BoardLayout.tsx says why it does not use
  // PreAuthLayout despite §6 asking for that.
  ```
- **Lines 194-198:** delete (the alias comment and `export { simpleRouter as router };`).

**`src/routes/CallDetail.tsx`** (full file). The full-edition hide/undo/note block and its state go.
```tsx
// frontend/src/routes/CallDetail.tsx
import { Anchor, Stack, Text } from "@mantine/core";
import { useEffect } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { useCallDetail } from "../api/calls";
import { useSession } from "../api/session";
import { CallDecisionActions } from "./CallDecisionActions";
import { CallFollowUp } from "./CallFollowUp";
import { MatchCallOverview } from "./MatchCallOverview";

export function CallDetail() {
  const { id = "" } = useParams();
  const callId = Number(id);
  const [sp] = useSearchParams();
  const profileParam = sp.get("profile");
  const profile = profileParam != null && profileParam !== "" ? Number(profileParam) : null;
  const navigate = useNavigate();
  // <keep the original comment block, lines 21-27, verbatim>
  const session = useSession();
  const fallbackProfile = profileParam === null ? (session.data?.user?.default_profile_id ?? null) : null;
  useEffect(() => {
    if (fallbackProfile === null) return;
    const next = new URLSearchParams(sp);
    next.set("profile", String(fallbackProfile));
    navigate({ search: `?${next.toString()}` }, { replace: true });
  }, [fallbackProfile, sp, navigate]);
  // <keep the original D11 comment, lines 36-38, verbatim>
  const backTo = sp.toString() ? `/?${sp.toString()}` : "/";
  const { data, isLoading, isError, error } = useCallDetail(callId, profile);

  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data) {
    const notFound = (error as { code?: string } | null)?.code === "not_found";
    return <Text role="alert" p="md">{notFound ? "Call not found." : "Couldn't load this call."}</Text>;
  }

  const { call } = data;

  return (
    <Stack component="main" maw="52rem" mx="auto" px="xs" pt="md" pb="xl" gap="lg">
      <Anchor component={Link} to={backTo} size="sm">← All calls</Anchor>

      <MatchCallOverview {...data} actions={<>
        <CallDecisionActions
          key={`${call.id}:${profile}`}
          callId={call.id}
          profileId={profile}
          title={call.title}
          initialState={call.decision_state ?? null}
          initialNote={call.decision_note ?? null}
        />
        <CallFollowUp key={`follow-up:${call.id}:${profile}`} call={call} profileId={profile} />
      </>} />
    </Stack>
  );
}
```

**`src/routes/AdminNav.tsx`** (full file). The link order is the same as the old match filter, which `e2e/match.spec.ts` asserts.
```tsx
import { NavLink } from "react-router-dom";

import styles from "./AdminNav.module.css";

// The admin sections router.tsx mounts. A link to anything else would render
// the in-app soft 404; AdminNav.test.tsx walks the router to keep the two in step.
const LINKS: { to: string; label: string }[] = [
  { to: "/admin", label: "Operational health" },
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

**`src/routes/ProfileOptions.tsx`** (full file):
```tsx
import type { ReactNode } from "react";

// Optional fields, folded into a closed <details> so the form leads with what the
// ranker needs. EligibilityWarning sits OUTSIDE it: a folded warning is not read.
export function ProfileOptions({ children, title = "Eligibility and funding preferences" }: { children: ReactNode; title?: string }) {
  return <details className="match-options"><summary>{title}</summary>{children}</details>;
}
```

**`src/routes/ProfileDetail.tsx`:**
- Delete line 9 (the edition import).
- Line 11 becomes:
  `function label(value: string) { return value.replaceAll("_", " ").replace(/^./, first => first.toUpperCase()); }`
- Delete lines 99-103: the two `!isMatchEdition` cards and the blank line after them.
- Line 154 becomes `      {p.can_edit && <>`

**`src/routes/ProfileEdit.tsx`:**
- Delete line 8.
- Line 84 becomes `        Research interests and funding preferences`
- Line 116 becomes `          its children into a closed <details>. */}`
- Lines 125-133 become:
  ```tsx
              <Textarea label="Description" rows={6}
                        value={description} onChange={(e) => setDescription(e.currentTarget.value)} />
              <Textarea label="Research areas" rows={3} value={struct.areas} onChange={event => setS("areas", event.currentTarget.value)} />
              {/* Eligibility up front, as on the create page (UX review
                  2026-10-31); the warning above names exactly these fields. */}
              <StructuredFields kind={p.kind} values={struct} onChange={setS} disabled={locked} part="eligibility" />
              <ProfileOptions title="Additional preferences (optional)">
  ```
- Line 138 becomes:
  `            <StructuredFields kind={p.kind} values={struct} onChange={setS} disabled={locked} hideAreas part="rest" />`
- Line 145 becomes `            <Textarea label="Must-include keywords" rows={3}`
- Line 151 becomes:
  `              <Button type="submit" disabled={locked || update.isPending}>Save preferences</Button>`

**`src/routes/ProfileNew.tsx`.** With `noUnusedLocals`, tsc rejects an unused `setLevel` (MEASURED), so the `level` state goes. Match always sent `level: ""`, so behaviour is identical.
- Delete line 7.
- Delete lines 10-14 (`LEVELS` and the blank line).
- Delete line 26 (the `level` state).
- Line 37 becomes `      name, kind, level: "", orcids, names_affiliations: namesAffiliations,`
- Line 50 becomes `        Your research interests and funding preferences`
- Lines 56-72 become:
  ```tsx
            <NativeSelect
              label="Profile type"
              data={[
                { value: "organizational_unit", label: "Research group" },
                { value: "individual", label: "Individual researcher" },
              ]}
              value={kind}
              onChange={(e) => setKind(e.currentTarget.value)}
            />
            <Textarea label="What do you research?" rows={4}
                      value={missionText} onChange={(e) => setMissionText(e.currentTarget.value)} />
  ```
- Lines 73-88 become:
  ```tsx
            <Textarea label="Research areas" description="Optional keywords, one per line — e.g. digital pathology" rows={2} value={struct.areas} onChange={event => setS("areas", event.currentTarget.value)} />
            {/* The three facts that decide eligibility are asked up front; folded,
                they were skipped and every score capped at 50 (UX review
                2026-10-31). Everything else stays optional and folded. */}
            <StructuredFields kind={kind} values={struct} onChange={setS} part="eligibility" />
            <ProfileOptions title="Additional preferences (optional)">
              <StructuredFields kind={kind} values={struct} onChange={setS} hideAreas part="rest" />
            </ProfileOptions>
  ```
- Line 98 becomes `            <Button type="submit" disabled={create.isPending}>Create profile</Button>`

**`src/routes/StructuredFields.tsx`:**
- Delete line 2.
- Lines 32-36 (doc comment) become:
  ```
  /** Which fields to render (UX review 2026-10-31). `eligibility` is the three
   *  facts the ranker's eligibility check reads -- organisation type, country and
   *  applicant types (see EligibilityWarning.tsx) -- shown up front, because left
   *  blank they cap every score at 50; `rest` is everything optional, folded.
   *  `all` is both in one block (the default; only StructuredFields.test.tsx). */
  ```
- Lines 52-57 become:
  ```tsx
    const applyingAs = (
      <MultiSelect label="Applying as" disabled={disabled} data={[
        { value: "solo_pi", label: "Individual investigator" }, { value: "consortium_coordinator", label: "Consortium lead" },
        { value: "consortium_partner", label: "Consortium partner" }, { value: "institution", label: "Institution" },
      ]} value={values.applicant_types.split("\n").filter(Boolean)} onChange={selected => onChange("applicant_types", selected.join("\n"))} />
    );
  ```
- Lines 97-106 become:
  ```tsx
          {!rest && applyingAs}
          <MultiSelect label="Funding sought" disabled={disabled} data={[
            { value: "project_grant", label: "Project grants" }, { value: "fellowship", label: "Fellowships" },
            { value: "mobility", label: "Research exchanges" }, { value: "infrastructure", label: "Infrastructure" }, { value: "programme", label: "Programmes" },
          ]} value={values.funding_instruments.split("\n").filter(Boolean)} onChange={selected => onChange("funding_instruments", selected.join("\n"))} />
  ```

**`src/api/admin.ts`:** delete lines 8-9 (the `DeletedWorkspace*` types) and lines 28-83 (`DELETED_PER_PAGE` and the 3 deleted-workspace hooks).

**`src/api/adminDashboard.ts`:** delete line 7 (`AdminDrafting` type) and lines 17-24 (`useAdminDrafting`).

**`src/test-setup.ts`.** Lines 6-34 (the `react-resizable-panels` warning filter and the CodeMirror heading) become:
```
// --- Layout APIs jsdom does not implement ---------------------------------------
// jsdom implements neither layout measurement nor matchMedia/ResizeObserver.
// These stubs return ZERO-rects / no-ops. They were written for CodeMirror and are
// kept because no test asserts pixel geometry and removing them is untested.
```

**`frontend/index.html`** (full file). The `editionTitle` plugin used to write this title; it is now static, so the tab reads the same. The `data-mantine-color-scheme` attribute is kept as is, so the first paint is unchanged.
```html
<!doctype html>
<html lang="en" data-mantine-color-scheme="dark">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg" />
    <title>Grant Lab — Call Match</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

**`frontend/vite.config.ts`** (full file):
- Gone: `editionTitle`, `moduleManifest` (its only reader was `check_match_bundle.mjs` and the package script), the router alias and `loadEnv`, `test.env`, the probe exclusion, and the `/api/ws` proxy (collab websockets; nothing kept opens a WebSocket or EventSource, READ).
- **Comment changes:** keep the original `spaIndexFallback` comment and body (lines 36-39 and 64-91) and the comments on `build`, `include` and the `^/` proxy. In the `^/` proxy comment, cut the last sentence to "Preserve the incoming Origin: APP_BASE_URL is the browser-facing Vite origin, and the HTTP CSRF check compares against it."
```ts
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import react from "@vitejs/plugin-react";
import type { Connect, PluginOption } from "vite";
// vitest/config's defineConfig is a superset of vite's that also accepts `test`.
import { defineConfig } from "vitest/config";

import { isSpaRoute, isViteAsset, shouldServeViaVite } from "./src/dev-proxy";

const BACKEND = "http://localhost:8000";

// <original comment lines 36-39>
function spaIndexFallback(): PluginOption { /* original lines 64-91 verbatim */ }

export default defineConfig({
  plugins: [react(), spaIndexFallback()],
  // <original build comment>
  build: { outDir: resolve(__dirname, "dist"), emptyOutDir: true },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    // <original "Scope unit tests to src/" comment>
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    exclude: ["**/node_modules/**"],
  },
  server: {
    port: 5173,
    // <original DEFAULT TO BACKEND comment, Origin sentence as above>
    proxy: {
      "^/": {
        target: BACKEND,
        changeOrigin: true,
        ws: false,
        bypass(req) {
          const url = (req.url ?? "/").split("?")[0];
          return shouldServeViaVite(url) ? req.url : undefined;
        },
      },
    },
  },
});
```

**`frontend/tsconfig.json`:** unchanged (READ).

**`frontend/routes.json`.** Drop `/cockpit`; `/workspaces` is optional and documentation-only. Then run `uv run python scripts/gen_routes.py`, which rewrites `src/grant_lab/web/spa_routes.py` and `frontend/src/routes.gen.ts`. The new reverse guard test in §1.4 needs this.
```json
  "spa_routes": ["/", "/calls", "/me", "/profiles", "/admin", "/boards", "/b",
    "/login", "/register", "/auth/pending", "/auth/no-access", "/auth/error"],
  "backend_prefixes": ["/api", "/healthz", "/auth", "/static"],
```
Expected `routes.gen.ts` (INFERRED from the generator's sort: length descending, stable):
```ts
export const SPA_ROUTES: string[] = ["/auth/no-access", "/auth/pending", "/auth/error", "/profiles", "/register", "/boards", "/calls", "/admin", "/login", "/me", "/b", "/"];
export const BACKEND_PREFIXES: string[] = ["/healthz", "/static", "/auth", "/api"];
```

### 1.4 Test edits

Vitest runs with `VITE_APP_EDITION` unset, so **unmocked tests currently assert the FULL edition** (READ). Before editing anything, measure which ones assert it: run `cd frontend && VITE_APP_EDITION=match pnpm run test` on the base. My static prediction for unmocked failures is `ProfileNew.test.tsx` ×2, `ProfileEdit.test.tsx` ×1 and `ProfileDetail.test.tsx` ×1. jsdom does not hide closed `<details>` content (READ, `default-stylesheet.js`), so the transfer-card tests still pass.

- **`router.test.tsx`** (moved):
  - Line 20: the import fix from §1.1.
  - Lines 24-27 become: "The route set, written out. This is an ALLOWLIST, not a subset check: a subset assertion would pass a router that mounted extra pages. Adding a line here is the reviewed act."
  - Delete lines 41-42.
  - Lines 52-54 become: "Prefixes of the removed proposal workspace. The exact-equality test above already holds them out; this names them, so a red run says what came back."
  - Line 61 becomes `it("the router mounts exactly the release-1 route set", () => {`
  - Lines 68, 74-75, 92 and 114: `simpleRouter` becomes `router`; test 2 is renamed "the router mounts no excluded path".
  - Lines 98-105 become a one-line section comment, then `it("every top-level route declares the RouteError errorElement", () => {` and `const tops = router.routes as RouteObject[];`.
  - Append two tests:
```tsx
it("every SPA_ROUTES entry owns at least one mounted page (no stale manifest entry)", () => {
  // The other direction of the test above. A manifest entry with no page behind it
  // makes the server answer index.html, then the in-app soft 404, where FastAPI's
  // real JSON 404 belongs. That is what /cockpit was once the workspace left.
  const leaves = leafPaths(router.routes as RouteObject[]).filter((p) => p !== "/*");
  const owns = (r: string, p: string) => (r === "/" ? p === "/" : p === r || p.startsWith(r + "/"));
  expect(SPA_ROUTES.filter((r) => !leaves.some((p) => owns(r, p)))).toEqual([]);
});

it("/login renders the pre-auth Login, not the shell's soft 404", () => {
  const r = createMemoryRouter(router.routes as RouteObject[], { initialEntries: ["/login"] });
  render(
    <MantineProvider theme={theme} defaultColorScheme="light">
      <QueryClientProvider client={new QueryClient()}>
        <RouterProvider router={r} />
      </QueryClientProvider>
    </MantineProvider>,
  );
  expect(screen.getByRole("heading", { name: /sign in/i })).toBeInTheDocument();
  expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
});
```
  Must-reject: on the base `routes.gen.ts`, the first test fails with `["/cockpit"]` (INFERRED).

- **`routes/AdminNav.test.tsx`**, full replacement:
```tsx
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it } from "vitest";

import { AdminNav } from "./AdminNav";

// The sections router.tsx mounts under /admin, in LINKS order. Written out rather
// than imported from AdminNav's LINKS, which would assert a relation with itself.
const MOUNTED_SECTIONS = ["/admin", "/admin/golden-set", "/admin/users", "/admin/users/pending"];

function hrefs() {
  return screen.getAllByRole("link").map((a) => a.getAttribute("href") ?? "");
}

it("renders exactly the four admin sections, in order", () => {
  render(<MemoryRouter><AdminNav /></MemoryRouter>);
  expect(hrefs()).toEqual(MOUNTED_SECTIONS); // equality, not "none of": an empty nav must not pass
  for (const label of ["Operational health", "Golden set", "Users", "Pending"]) {
    expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
  }
  for (const label of ["Drafting", "Deleted workspaces"]) {
    expect(screen.queryByRole("link", { name: label })).not.toBeInTheDocument();
  }
});

// The coherence gate: every link AdminNav renders is a route the router mounts.
it("every admin link is a route the router mounts", async () => {
  const { router } = await import("../router");
  const { leafPaths } = await import("../routeLeafPaths");
  const mounted = new Set(leafPaths(router.routes as never));
  render(
    <MemoryRouter initialEntries={["/admin"]}>
      <AdminNav />
    </MemoryRouter>,
  );
  const links = hrefs();
  expect(links.length).toBeGreaterThan(0);
  expect(links.filter((h) => !mounted.has(h))).toEqual([]);
  // The dynamic import counts against the timeout: 1.5 s alone, over 5 s in a loaded run (2026-10-05).
}, 20_000);
```

- **`routes/CallDetail.test.tsx`:**
  - Line 3 becomes `import { act, render, screen, waitFor } from "@testing-library/react";`
  - Lines 11-26 become `afterEach(() => { vi.restoreAllMocks(); });`
  - Delete the `edition.isMatch = true;` lines 77, 93, 107, 115, 155, 585, 594 and 604.
  - Delete the full-edition tests, ranges inclusive: 131-150 (hero, FULL Start-drafting), 166-172 (low band), 185-262 (Hide confirm/undo, Gate 14), 287-330 (Gate 15), 351-563 (Gate 16, the note tests and PD-31b arrival).
  - Kept: the 4 + 3 MATCH tests, "renders NO Start-drafting control", not-found, `deadline_local_date`, and both back-link tests.
  - The `useHideCall` invalidation contract stays covered in `api/calls.test.tsx:51` (READ).
- **`routes/MatchCallOverview.test.tsx`** (moved):
  - Line 1 becomes `// frontend/src/routes/MatchCallOverview.test.tsx`
  - Line 5 becomes `import { expect, it } from "vitest";`
  - Line 9 becomes `import { MatchCallOverview } from "./MatchCallOverview";`
  - Delete lines 11-30.
  - Line 53 becomes `<MemoryRouter>{<MatchCallOverview {...data} />}</MemoryRouter>`
  - Delete lines 58-84, 98-102, 127-133, 163-172 and 229-237.
  - Delete the `edition.isMatch = true;` lines 89, 139, 184, 217, 264, 271, 280.
  - Lines 305-332 become:
    ```tsx
    it("says it when the re-rank keeps failing", () => {
      const { container } = renderBody(RANK_FAILED);
      expect(container.querySelector("[data-band]")).toBeNull();
      expect(container.querySelector('[data-field="ranking-failed"]')).toHaveTextContent(
        "Couldn't rank this call with the local model (last tried 2026-10-25)",
      );
    });

    it("says nothing for a ranking that is current", () => {
      const { container } = renderBody(DATA);
      expect(container.querySelector('[data-field="ranking-failed"]')).toBeNull();
    });
    ```
- **`routes/AdminDashboard.test.tsx`:**
  - Lines 10-31 become `afterEach(() => {\n  vi.restoreAllMocks();\n});`
  - Lines 127-156 become one test:
    ```tsx
    it("links out to the golden-set admin page", async () => {
      mockDash();
      renderWithProviders(<AdminDashboard />);
      // Wait for the page to render, so this is not a claim about a loading screen.
      expect(await screen.findByText("Vinnova")).toBeInTheDocument();
      expect(screen.getByText(/Ranking quality/)).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /manage golden set/i })).toHaveAttribute(
        "href",
        "/admin/golden-set",
      );
    });
    ```
- **`api/adminDashboard.test.tsx`:** delete line 9 and lines 28-35.
- **`routes/ProfileDetail.test.tsx`:** lines 45-47 expect `"Oncology"`, `"Biobank"` and `"Project grant"`.
- **`routes/ProfileEdit.test.tsx`:** line 88 becomes `/save preferences/i`.
- **`routes/ProfileNew.test.tsx`:** line 34 becomes `getByLabelText(/^profile type/i)`; line 43 becomes `/create profile/i`.
- **`routes/ProfileNewMatch.test.tsx`:** lines 11-21 become a 4-line comment: "The create form asks the three facts the ranker's eligibility check reads -- organisation type, country, applicant types -- up front (UX review 2026-10-31); folded, they were skipped and every score capped at 50. Everything else stays folded."
- **`routes/ProfileEditEligibilityPlacement.test.tsx`:** delete lines 11-20.
- **`noCockpitLinks.test.tsx`.** Keep it; it guards a live rule.
  - Replace the header comment with: "No source file may emit an href into the routes the proposal workspace used to own; router.tsx no longer mounts them. A source scan because a render test misses links in conditional branches; a dynamically built href slips through (replace with an AST check if one appears). e2e/match.spec.ts asks the rendered DOM the same question."
  - Then:
    ```ts
    const EXCLUDED_PREFIXES = ["/cockpit", "/admin/drafting", "/admin/deleted-workspaces"];
    const EXEMPT = [".test.tsx", ".test.ts", ".gen.ts"];
    ```
  - Keep `walk()` and the test body; rename the test to "no source emits an href into the removed workspace routes".
- **`StructuredFields.test.tsx`, `App.test.tsx`, `SimpleShell.test.tsx`, theme tests:** no code change (INFERRED pass).

### 1.5 Comment-only mentions (no behaviour)

Reword these now: each names a deleted file or gate, so it is now false.

| File:line | Action |
|---|---|
| `api/calls.ts:48-50` | "On the call page (CallDecisionActions) no rail is mounted, so the counts half is an inexpensive no-op." |
| `api/dashboard.ts:11-15` | "`archive` is the server's default; TriageFeed maps it to `inbox`." |
| `api/decisions.ts:10,24` | drop "FULL edition's list" and "match edition ships"; say "no screen offers min_score" |
| `routes/AdminDashboard.tsx:193-198` | delete the comment |
| `routes/BoardDetail.tsx:138-141` | "A deliberate small duplicate of a date helper, not an oversight." |
| `routes/BoardLayout.tsx:3-4` | "sibling to SimpleShell and PreAuthLayout (router.tsx)" |
| `routes/PreAuthLayout.tsx:6-7` | "sibling to SimpleShell … NO SimpleShell chrome" |
| `routes/EligibilityWarning.tsx:52` | drop "in the match edition" |
| `routes/TriageCard.tsx:31-33` | delete the MatchBadge sentence |
| `routes/TriageEmptyProfile.tsx:8-11,25-27` | drop the edition and `check_match_bundle` sentences |
| `routes/TriageFeed.tsx:25-33,35-36,165-166,524-525` | delete the "EDITION SPLIT" paragraph; replace "full edition's list" with "the server's default" |
| `shell/SimpleShell.tsx:8-10,14-17,42-48,143-147` | "Imported here so its cascade order is fixed"; drop the TitleBar, AppShell and `check_match_bundle` sentences |
| `shell/SimpleShell.module.css:1-3,55` | drop "AppShell"/"two editions"; the `.edition` class can stay |
| `theme/theme.css:4`, `theme.match.css:14-15,29-30`, `theme.ts:5,21,27-30` | drop the "full build"/"edition" clauses |
| `routes/BoardScreen.module.css:66,115-119` | drop CallRow and "edition" |
| `routeLeafPaths.ts:7-10` | "Used by router.test.tsx and AdminNav.test.tsx." |
| `e2e/match.spec.ts:9-12,17-23,327-328` | "runs `pnpm run build` and serves frontend/dist"; replace `router.simple(.test).tsx` with `router(.test).tsx`; replace `test_edition_surface.py` with `e2e/smoke.spec.ts`. **Comments only:** `test_match_gates_are_wired.py` pins exactly 185 `expect()` calls and `/cockpit` in this file (READ). |

Test-file comments that mention editions (TriageFeed, TriageCard, TriageEmptyProfile, SimpleShell, contrast, CriterionChips, ProfileVisibilityCard, Settings, ProfileDetail tests) can wait for piece 5. Find everything left with:
```
git grep -nE 'router\.simple|check_match_bundle|isMatchEdition|APP_EDITION|edition\.ts|AppShell|TitleBar|Dashboard\.tsx|CallRow|CallReadingPane|MatchBadge|gate 1|full build|full edition|FULL edition' -- frontend scripts
```

### 1.6 `package.json` and libraries

**Scripts after the change:**
```json
"dev": "vite",
"build": "tsc && vite build",
"package": "../scripts/package_spa.sh",
"test": "vitest run",
"lint": "oxlint --deny-warnings src e2e",
"gen:api": "bash ../scripts/gen_openapi_types.sh",
"e2e:match": "E2E_MODE=match playwright test e2e/match.spec.ts"
```
- `build:match`, `package:match` and `check:bundle` are removed.
- `e2e:match` keeps its name and text: `test_match_gates_are_wired.py` asserts that it contains `E2E_MODE=match` and `e2e/match.spec.ts` (READ).
- In docs, write `pnpm -C frontend run package`; `run` makes it unambiguous.

**Dependencies.** Every candidate is imported only by deleted files (MEASURED import map):
- `y-prosemirror`, `markdown-it`, `prosemirror-*`: only by `frontend/scripts/*`.
- `ws`, `@types/ws`: only by `collab/handshake.probe.test.ts`.

Keep `@fontsource/noto-sans` (`BoardScreen.fonts.css`), `@vscode/codicons` (`main.tsx`) and `ics` (`callCalendar.ts`).

```bash
pnpm -C frontend remove @codemirror/commands @codemirror/lang-markdown @codemirror/language @codemirror/state @codemirror/view @lezer/highlight @tiptap/core @tiptap/extension-collaboration @tiptap/extension-collaboration-caret @tiptap/extension-table @tiptap/pm @tiptap/react @uiw/codemirror-theme-vscode lib0 react-resizable-panels y-prosemirror y-protocols yjs ws @types/ws markdown-it prosemirror-markdown prosemirror-model prosemirror-schema-list
git diff -U0 frontend/pnpm-lock.yaml | grep -c '^+[^+]'   # expect 0: pure deletions, no re-resolution
pnpm -C frontend install --frozen-lockfile                 # must succeed
```

**The release-age cooldown.** `minimumReleaseAge: 10080` is set in `frontend/pnpm-workspace.yaml`, and `.npmrc` sets only `save-exact` (READ). The cooldown filters new candidate versions during resolution only (INFERRED). A removal resolves nothing new, and a frozen install resolves nothing, so the cooldown should not block. If `pnpm remove` re-resolves anything, the lockfile diff shows `+` lines. Stop then.

### 1.7 Verify, in the worktree

Run `pnpm -C frontend exec tsc` (noEmit), `run lint`, `run test`, `run build` and `run e2e:match`. Regenerate `schema.gen.ts` only after the backend routers are gone (§3).

---

## 2. Browser tests

### 2.1 What the seed does today and what the smoke spec needs

**The current seed** (`scripts/collab_e2e_seed.py`, READ):
- **Collab-only:** 2 users, an active workspace with a viewer, collab enabled on draft and spec, a Reference and ReferenceChunk, a git repo, and the cookies and labels it prints.
- **Kept:** the password user `e2e@umu.se` / `correct-horse-1` and one Call ("Precision medicine programme", no deadline).
- **It seeds no Profile.**

**Why that matters for the match feed:**
- The `/api/calls` profile is the user's `default_profile_id` when `?profile` is absent, if it is active and readable (READ, `web/dashboard.py:148-166`).
- With no profile, TriageFeed renders `TriageEmptyProfile` ("No research profile yet."), not cards (READ, `TriageFeed.tsx:534`).
- The inbox lists every open (null or future deadline) call with no decision, ranked or not, via a LEFT JOIN (READ, `dashboard.py:88,561-630`).
- `preparing` needs a queued or running bootstrap job; with none, it is null (READ, `api/profiles.py:335-341`).

**Seed change.** Add a Profile owned by the user and set `user.default_profile_id` to it. Keep `SESSION_SECRET` `"e2e-seed-secret-at-least-16-chars!"`: `tests/test_config.py:280` needs that literal in a tracked non-test file (READ).

**Selectors** (all READ):
- Login: label "Email", textbox "Password", button "Sign in"; it redirects to `"/"` (`auth.py:139`).
- Feed: h1 "Your matches" (`TriageFeed.tsx:633`); the card title link is `h2 > Link` (`TriageCard.tsx:280`).
- Account menu: button aria-label "Account menu for <email>", then menuitem "Sign out", then `location.assign("/login")` (`SimpleShell.tsx`).
- Call page: h1 = call title (`MatchCallOverview.tsx:12`).
- `/api/me` returns 401 without a session.

**e2e helpers used only by collab:** none in `frontend/e2e/` apart from `collab.spec.ts` itself. Outside it: `frontend/scripts/*`, `scripts/collab_handshake_probe.py` and `frontend/vitest.probe.config.ts`. All are deleted in §1.2.

### 2.2 `frontend/playwright.config.ts` (full file)

```ts
import { defineConfig } from "@playwright/test";

// Two kinds of browser run, selected by E2E_MODE:
//   E2E_MODE=dev  (default) → e2e/smoke.spec.ts via the Vite dev server on :5173,
//                             which proxies to uvicorn (SPA_MODE=dev) on :8000
//   E2E_MODE=prod           → e2e/smoke.spec.ts against FastAPI serving the
//                             packaged SPA (SPA_MODE=prod) on :8000
//   E2E_MODE=match          → e2e/match.spec.ts against the production bundle,
//                             built by the webServer below and served statically
//                             on :4173 with the API mocked in-page. The only mode
//                             that starts its own server.
// dev and prod need a seeded Postgres and a running uvicorn, so
// scripts/e2e_run.sh brings those up (and Vite in dev mode) and these specs only
// drive the browser.
//
// CSRF constraint: the OriginCSRFMiddleware rejects any state-changing POST whose
// Origin != APP_BASE_URL, so uvicorn MUST run with APP_BASE_URL set to the origin
// the browser POSTs from, or login/logout 403:
//   E2E_MODE=dev  → APP_BASE_URL=http://localhost:5173 (the Vite origin)
//   E2E_MODE=prod → APP_BASE_URL=http://localhost:8000 (FastAPI serves directly)
const MODE = process.env.E2E_MODE ?? "dev";

export default defineConfig({
  testDir: "./e2e",
  // Each mode runs exactly one spec, so a bare `playwright test` can never drive
  // the mocked spec at a real backend, or the smoke spec at the static mock server.
  testMatch: MODE === "match" ? /match\.spec\.ts$/ : /smoke\.spec\.ts$/,
  webServer:
    MODE === "match"
      ? {
          // The artefact IS the thing under test, so it is always rebuilt: a
          // build that fails at tsc cannot leave a previous dist/ standing.
          command: "pnpm run build && node ../scripts/serve_spa_static.mjs dist 4173",
          url: "http://127.0.0.1:4173/index.html",
          // NOT `!process.env.CI`: reusing a server on :4173 would skip the build
          // and validate whatever bundle is already being served.
          reuseExistingServer: false,
          timeout: 180_000,
        }
      : undefined,
  use: {
    baseURL:
      MODE === "match"
        ? "http://127.0.0.1:4173"
        : MODE === "prod"
          ? "http://localhost:8000"
          : "http://localhost:5173",
    // CI uploads frontend/test-results/ on failure; a trace is the only way to
    // see WHY a step failed on a headless runner.
    trace: process.env.CI ? "retain-on-failure" : "off",
  },
});
```

### 2.3 New `frontend/e2e/smoke.spec.ts` (full file)

```ts
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";

// Real-backend browser smoke: the only browser checks that reach FastAPI.
// e2e/match.spec.ts covers the screens in depth but mocks /api in-page, so it
// cannot see the serving, session and CSRF layers this file exercises.
//
// Run by scripts/e2e_run.sh, which boots scripts/e2e_seed.py (testcontainers
// Postgres + uvicorn) and, in dev mode, Vite:
//   E2E_SPA_MODE=dev  bash scripts/e2e_run.sh   # Vite proxy on :5173
//   E2E_SPA_MODE=prod bash scripts/e2e_run.sh   # packaged bundle on :8000
// It relies on the seed's password user, that user's default profile, and one
// open call; see the seed's docstring.

// Hard-coded in scripts/e2e_seed.py too. Change both or neither.
const EMAIL = "e2e@umu.se";
const PASSWORD = "correct-horse-1";
const CALL_TITLE = "Precision medicine programme";
// The runner exports the seeded call's id; a hand-started seed may not.
const CALL_URL = process.env.E2E_CALL_ID
  ? new RegExp(`/calls/${process.env.E2E_CALL_ID}(\\?|$)`)
  : /\/calls\/\d+(\?|$)/;

// Real login through the React password form and the JSON auth endpoint.
async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel(/email/i).fill(EMAIL);
  await page.getByRole("textbox", { name: /password/i }).fill(PASSWORD);
  await Promise.all([
    page.waitForURL((url) => url.pathname === "/"),
    page.getByRole("button", { name: /sign in/i }).click(),
  ]);
}

// The feed has rendered the seeded profile's inbox (not the no-profile screen,
// not "Loading…"), and the seeded call is on it.
async function seededCallInFeed(page: Page): Promise<Locator> {
  await expect(page.getByRole("heading", { name: "Your matches" })).toBeVisible({ timeout: 15_000 });
  const link = page.getByRole("link", { name: CALL_TITLE });
  await expect(link, "the seeded call is not in the feed - check scripts/e2e_seed.py").toBeVisible();
  return link;
}

test("/ renders the triage feed with no critical a11y violations", async ({ page }) => {
  await login(page);
  await page.goto("/");
  await seededCallInFeed(page);
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter((v) => v.impact === "critical")).toEqual([]);
});

test("an unknown path gets FastAPI's JSON 404, not index.html", async ({ page }) => {
  // dev: the Vite proxy forwards unknown paths to uvicorn (default-to-backend);
  // prod: uvicorn serves index.html only for manifest-owned routes.
  const res = await page.request.get("/made-up-page");
  expect(res.status()).toBe(404);
  expect(res.headers()["content-type"] ?? "").toContain("application/json");
});

test("signing out through the account menu lands on /login and ends the session", async ({ page }) => {
  await login(page);
  await page.goto("/");
  await page.getByRole("button", { name: /account menu/i }).click();
  await Promise.all([
    page.waitForURL((url) => url.pathname === "/login"),
    page.getByRole("menuitem", { name: /sign out/i }).click(),
  ]);
  await expect(page).toHaveURL(/\/login$/);
  // The URL alone is not proof: a control that only navigated to /login would
  // pass it. The server must have dropped the session too.
  const me = await page.request.get("/api/me");
  expect(me.status()).toBe(401);
});

test("opening a call from the feed stays in the SPA and survives a reload", async ({ page }) => {
  await login(page);
  await page.goto("/");
  const link = await seededCallInFeed(page);
  // A marker on window survives a client-side navigation and dies with a full load.
  await page.evaluate(() => {
    (window as unknown as { __smoke?: string }).__smoke = "same-document";
  });
  await link.click();
  await expect(page).toHaveURL(CALL_URL);
  await expect(page.getByRole("heading", { level: 1, name: CALL_TITLE })).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { __smoke?: string }).__smoke)).toBe(
    "same-document",
  );
  // A hard refresh asks the SERVER for /calls/<id>: it must hand back the SPA
  // shell (the route is manifest-owned), not a 404.
  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: CALL_TITLE })).toBeVisible();
});
```

Every async matcher is awaited. G7 must-reject: break logout and the third test goes red, either on the URL or on the 401 (INFERRED).

### 2.4 `scripts/e2e_seed.py` (after `git mv`; full rewrite)

```python
"""Seed and serve the backend for the real-backend browser smoke.

Boots a testcontainers Postgres, runs the Alembic migrations, seeds the rows
frontend/e2e/smoke.spec.ts needs, starts uvicorn on :8000, and prints the E2E_*
lines scripts/e2e_run.sh waits for. The server stays up until Ctrl-C.

SPA_MODE follows E2E_SPA_MODE. "dev" (default): the browser talks to the Vite
proxy on :5173, so APP_BASE_URL is that origin and the CSRF Origin check passes.
"prod": FastAPI itself serves the packaged bundle on :8000 under the strict CSP;
scripts/e2e_run.sh builds and packages it first.

What it seeds, and why each row exists:
  * an ACTIVE password user, e2e@umu.se / correct-horse-1. The spec's login()
    helper hard-codes both values.
  * a profile that user owns, stored as their default profile. Without a
    resolved profile, / renders the "No research profile yet." screen
    (TriageEmptyProfile) and there is no call card to open.
  * one open call with no deadline and no decision. The inbox lists every open,
    undecided call for the profile whether or not it has been ranked, so this is
    the card the spec opens.

Usage (scripts/e2e_run.sh does all of this):
    EMBEDDING_MODEL=bge-m3 uv run python -u scripts/e2e_seed.py
then, in dev mode, `cd frontend && pnpm dev` in a second terminal, then
    cd frontend && E2E_MODE=dev pnpm exec playwright test e2e/smoke.spec.ts
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from pathlib import Path

# The source tree, so `grant_lab` imports without relying on the editable install.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Fixed, because frontend/e2e/smoke.spec.ts hard-codes all three. Change both or neither.
E2E_PASSWORD_EMAIL = "e2e@umu.se"
E2E_PASSWORD = "correct-horse-1"
E2E_CALL_TITLE = "Precision medicine programme"


def main() -> int:
    os.environ.setdefault("EMBEDDING_MODEL", "bge-m3")
    # Minimal env that create_app/Settings validates.
    os.environ.setdefault("SESSION_SECRET", "e2e-seed-secret-at-least-16-chars!")
    os.environ.setdefault("ALLOWED_EMAIL_DOMAINS", "umu.se")
    os.environ["SPA_MODE"] = os.environ.get("E2E_SPA_MODE", "dev")
    # APP_BASE_URL must match the origin the browser POSTs from (CSRF Origin check
    # and the CSP): the Vite proxy on :5173 in dev, FastAPI itself on :8000 in prod.
    os.environ["APP_BASE_URL"] = (
        "http://localhost:8000" if os.environ["SPA_MODE"] == "prod" else "http://localhost:5173"
    )

    import uvicorn
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    with PostgresContainer("pgvector/pgvector:pg16", driver="psycopg") as pg:
        db_url = pg.get_connection_url()  # postgresql+psycopg://...
        os.environ["DATABASE_URL"] = db_url

        # --- migrate ---
        from alembic.config import Config

        from alembic import command

        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(cfg, "head")

        # --- build app ---
        from grant_lab.main import create_app

        app = create_app()

        # Override session_factory so the app uses the testcontainers DB.
        from grant_lab.db.session import make_engine, make_session_factory

        Session = make_session_factory(make_engine(db_url))
        app.state.session_factory = Session

        # --- seed ---
        from grant_lab.auth.passwords import hash_password
        from grant_lab.db.models import Call, Profile, Source, User

        suffix = uuid.uuid4().hex[:8]
        with Session() as s:
            user = User(
                email=E2E_PASSWORD_EMAIL,
                status="active",
                password_hash=hash_password(E2E_PASSWORD),
            )
            s.add(user)
            s.flush()
            profile = Profile(
                name="E2E research group", kind="organizational_unit", created_by=user.id
            )
            s.add(profile)
            s.flush()
            user.default_profile_id = profile.id
            src = s.query(Source).filter_by(slug="vinnova").one_or_none()
            if src is None:
                src = Source(slug="vinnova", name="Vinnova", kind="api")
                s.add(src)
                s.flush()
            call = Call(
                source_id=src.id,
                external_id=f"E2E-{suffix}",
                title=E2E_CALL_TITLE,
                url=f"https://example.org/calls/E2E-{suffix}",
                summary="Funding for AI-based diagnostics.",
                full_text=(
                    "Body about eligibility, scope, budget, expected "
                    "outcomes, restrictions, mandatory."
                ),
            )
            s.add(call)
            s.commit()
            profile_id, call_id = profile.id, call.id

        # --- start uvicorn on :8000 (the Vite proxy target in dev) ---
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="warning")
        )
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)
        if not server.started:
            # uvicorn never bound (e.g. :8000 already in use by an orphaned run).
            # Refuse to print vars, or the runner would validate a STALE server.
            print(
                "ERROR: uvicorn did not start within 10s (is port 8000 already in "
                "use?). Refusing to print stale env vars.",
                file=sys.stderr,
            )
            sys.stderr.flush()
            return 1

        print()
        print("=== e2e seed ready ===")
        print()
        print(f"E2E_PASSWORD_EMAIL={E2E_PASSWORD_EMAIL}")
        print(f"E2E_PASSWORD={E2E_PASSWORD}")
        print(f"E2E_PROFILE_ID={profile_id}")
        # LAST: scripts/e2e_run.sh waits for this line, so every line above is
        # already in its log when it reads them.
        print(f"E2E_CALL_ID={call_id}")
        print()
        print("Dev mode: start Vite in another terminal (cd frontend && pnpm dev),")
        print("or use the one-command runner: scripts/e2e_run.sh")
        print("Press Ctrl-C to stop uvicorn (the testcontainers Postgres stops too).")
        sys.stdout.flush()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

        server.should_exit = True
        return 0


if __name__ == "__main__":
    sys.exit(main())
```

Two hazards for this file (READ):
- `test_package_spa.py` scans `scripts/*.py` for a write verb on a line that names `src/grant_lab/web/spa`. Never put `replace` or `install` on such a line.
- `ruff` checks E, F, I, B, UP, SIM and ASYNC with line-length 100. The alembic import split above copies the original file's ruff-clean form.

### 2.5 `scripts/e2e_run.sh` (after `git mv`)

Keep lines 28-106 verbatim: `set -euo pipefail`, `set -m`, ROOT, mode validation, logs, cleanup trap and preflight. `tests/architecture/test_collab_e2e_run_cleanup.py` pins that logic. In the preflight comment, "fresh workspace" becomes "fresh seed".

**Header (lines 1-27) becomes:**
```bash
#!/usr/bin/env bash
# One-command real-backend browser smoke (frontend/e2e/smoke.spec.ts).
#
#   scripts/e2e_run.sh [extra playwright args...]
#
# Two modes, selected by E2E_SPA_MODE (default "dev"):
#   dev  (default) — boots scripts/e2e_seed.py (testcontainers PG + uvicorn:8000,
#                     SPA_MODE=dev), starts Vite dev (:5173), waits for both ports.
#   prod (E2E_SPA_MODE=prod) — builds the SPA (`pnpm build`), packages it into
#                     src/grant_lab/web/spa/ through scripts/package_spa.sh, and
#                     boots the seed with SPA_MODE=prod so uvicorn:8000 serves the
#                     bundle under the strict CSP. No Vite. This REPLACES the
#                     served tree: never run prod mode in a checkout that serves a
#                     live app.
# Either way it waits for the seed's E2E_* vars, runs the specs (playwright.config
# .ts's E2E_MODE switch picks the baseURL) and tears everything down on exit.
#
# Requirements: `uv`, Docker (testcontainers), frontend deps (pnpm i), and a
# Chromium usable by Playwright. On a fresh machine the browser OS libs may be
# missing:  cd frontend && pnpm exec playwright install --with-deps chromium  (sudo)
```

**Lines 119-128 (the prod build and package) become:**
```bash
  echo "[run] E2E_SPA_MODE=prod — building the SPA (pnpm build)…"
  ( cd frontend && pnpm build )
  # package_spa.sh is the only writer of the served tree
  # (tests/architecture/test_package_spa.py); a copy here would be a second one.
  echo "[run] packaging frontend/dist/ into the served tree…"
  bash "$ROOT/scripts/package_spa.sh"
```

**Lines 131-153 become:**
```bash
echo "[run] starting seed (testcontainers PG + uvicorn:8000, SPA_MODE=$E2E_SPA_MODE)…"
# `uv run python -u` → unbuffered stdout so the printed E2E_* vars reach $SEED_LOG
# immediately, and no pre-activated venv required.
env EMBEDDING_MODEL=bge-m3 E2E_SPA_MODE="$E2E_SPA_MODE" \
  uv run python -u scripts/e2e_seed.py >"$SEED_LOG" 2>&1 &
SEED_PGID=$!

# Wait up to ~150s for the seed to print its vars (PG image pull + migrate).
for _ in $(seq 1 300); do
  grep -q '^E2E_CALL_ID=' "$SEED_LOG" && break
  kill -0 "$SEED_PGID" 2>/dev/null || { echo "[run] seed exited early:"; cat "$SEED_LOG"; exit 1; }
  sleep 0.5
done
grep -q '^E2E_CALL_ID=' "$SEED_LOG" || { echo "[run] seed never printed vars:"; tail -40 "$SEED_LOG"; exit 1; }

get() { grep "^$1=" "$SEED_LOG" | head -1 | cut -d= -f2-; }
export E2E_CALL_ID; E2E_CALL_ID="$(get E2E_CALL_ID)"
export E2E_PROFILE_ID; E2E_PROFILE_ID="$(get E2E_PROFILE_ID)"
echo "[run] seeded call=$E2E_CALL_ID profile=$E2E_PROFILE_ID"
```

Keep lines 155-181 (Vite start and the readiness wait) verbatim.

**Lines 183-193 become:**
```bash
echo "[run] running the browser smoke (E2E_SPA_MODE=$E2E_SPA_MODE)…"
cd frontend
# E2E_MODE drives playwright.config.ts's baseURL: "prod" -> :8000 (no proxy), else
# :5173. E2E_SPECS overrides the spec list.
E2E_SPECS="${E2E_SPECS:-e2e/smoke.spec.ts}"
# shellcheck disable=SC2086  # deliberate word-splitting: E2E_SPECS is a spec list
E2E_MODE="$E2E_SPA_MODE" pnpm exec playwright test $E2E_SPECS "$@"
```

### 2.6 CI `e2e` job: replace lines 346-409

```yaml
  # Playwright against the REAL backend: frontend/e2e/smoke.spec.ts (a11y at /,
  # FastAPI's JSON 404 for an unknown path, sign-out ending the session, a call
  # page opened from the feed and reloaded). Both front doors are gated: `dev` is
  # the Vite proxy, `prod` builds the SPA, packages it through
  # scripts/package_spa.sh and serves it from uvicorn under the strict CSP.
  # match.spec.ts (`match-e2e` below) mocks the API in-page, so this job is the
  # only browser check that reaches FastAPI. Its own job so a browser-level
  # failure is reported on its own and the unit gates keep running.
  e2e:
    # Pinned, not ubuntu-latest: GitHub moved ubuntu-latest to 26.04 and Playwright
    # 1.60 refuses to install Chromium there ("does not support chromium on
    # ubuntu26.04-x64", CI run 40789842634). Unpin when Playwright supports it.
    runs-on: ubuntu-24.04
    timeout-minutes: 25
    strategy:
      fail-fast: false
      matrix:
        spa_mode: [dev, prod]
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4

      - name: Install uv
        uses: astral-sh/setup-uv@caf0cab7a618c569241d31dcd442f54681755d39 # v3
        with:
          version: "0.11.x"

      - name: Set up Python
        run: uv python install 3.12

      - name: Sync deps (frozen)
        run: uv sync --all-extras --frozen

      - name: Set up Node
        uses: actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020 # v4
        with:
          node-version-file: frontend/.nvmrc

      - name: Enable corepack (pnpm pinned via package.json packageManager)
        run: corepack enable

      - name: Frontend deps (frozen, scripts off)
        run: cd frontend && pnpm install --frozen-lockfile

      - name: Playwright Chromium (+ OS libs)
        run: cd frontend && pnpm exec playwright install --with-deps chromium

      # The harness boots a testcontainers Postgres (Docker is on the runner),
      # migrates, seeds a password user, that user's default profile and one open
      # call (scripts/e2e_seed.py), starts uvicorn (and Vite in dev mode), runs the
      # smoke spec, and tears it all down.
      - name: Playwright smoke (real backend) — SPA_MODE=${{ matrix.spa_mode }}
        env:
          E2E_SPA_MODE: ${{ matrix.spa_mode }}
        run: bash scripts/e2e_run.sh

      - name: Upload Playwright traces on failure
        if: failure()
        uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
        with:
          name: playwright-${{ matrix.spa_mode }}
          path: |
            frontend/test-results/
            .e2e-vite.log
          if-no-files-found: ignore
```

---

## 3. CI, scripts, Dockerfile

### `.github/workflows/ci.yml`

- **Lines 162-163** become:
  ```yaml
    # Frontend gates: lint, vitest, the typecheck + production build, the OpenAPI
    # drift check, and the pnpm/OSV scans over the JS lockfile.
  ```
- **Lines 212-235** (the bundle-gate step and the second vitest run, `VITE_APP_EDITION=match …virtual-router.test.ts`) become:
  ```yaml
        # tsc over src/ (tests included) and the production `vite build`. vitest
        # does not typecheck, so without this step a type error would surface only
        # in the match-e2e and package jobs.
        - name: Frontend typecheck + production build
          run: cd frontend && pnpm run build
  ```
- **Lines 237-243** (the OpenAPI drift check): unchanged.
- **Lines 289-295** become:
  ```yaml
    # The shippable artefact: the container SPA build, the packaging step, and the
    # wheel. No `needs:` — it builds the SPA inside frontend/Dockerfile with its own
    # pnpm install, so there is nothing to share and nothing to wait for.
  ```
- **Lines 310-313** ("Set up Node" in `package`): delete. Its only consumer was `check_match_bundle.mjs` via `package_spa.sh`; hatchling has no build hook (READ, `pyproject.toml`). Optional, INFERRED.
- **Lines 324-332** become:
  ```yaml
        # Extract to frontend/dist and let scripts/package_spa.sh write the served
        # tree. It is the ONLY writer of src/grant_lab/web/spa (pinned by
        # tests/architecture/test_package_spa.py); a `docker cp` straight into that
        # directory would be a second one.
  ```
  The run block stays the same; `test_package_spa.py` asserts `bash scripts/package_spa.sh` appears in `ci.yml`.
- **Lines 346-409:** replace with the YAML in §2.6.
- **Lines 411-422** become:
  ```yaml
    # The production bundle in a real browser with the API mocked in-page
    # (e2e/match.spec.ts). Its own job, and deliberately not a third value in the
    # `e2e` matrix above: that matrix drives scripts/e2e_run.sh, which needs uv, a
    # testcontainers Postgres and a seeded user. This needs none of them —
    # playwright.config.ts's match mode builds the bundle, serves frontend/dist
    # statically and mocks /api in-page. ~2 min, no Docker, no Python.
    #
    # Why it exists: release 1 shipped with no way to sign out and a call list
    # whose rows navigated nowhere — obvious in a browser, invisible to unit tests.
  ```
- **Lines 446-450** become:
  ```yaml
        # `pnpm run e2e:match` is E2E_MODE=match; the webServer chains `pnpm run
        # build && serve`, so a build that fails at tsc cannot leave the previous
        # dist/ standing for the browser to pass over.
        - name: Browser smoke (production bundle, mocked API)
          run: cd frontend && pnpm run e2e:match
  ```
  `test_match_gates_are_wired.py` keys on the job name `match-e2e` and the run text, not the step name (READ).

### `scripts/package_spa.sh` (full file)

The edition marker and the bundle gate go.
```bash
#!/usr/bin/env bash
# Copy a built SPA into the directory FastAPI serves under SPA_MODE=prod.
#
# SPA_DIR is src/grant_lab/web/spa (web/spa_serving.py). `vite build` writes to
# frontend/dist and NOTHING copies it across, so a smoke test run straight after a
# build serves the PREVIOUS bundle unless this script runs.
#
# THIS IS THE ONLY WRITER OF THE SERVED DIRECTORY, and that is load-bearing:
# ci.yml (the wheel) and scripts/e2e_run.sh (prod-mode e2e) both call it instead of
# copying on their own. tests/architecture/test_package_spa.py::
# test_no_other_writer_of_the_served_tree scans scripts/ and .github/workflows/
# for a write to that path outside this file and fails if one reappears.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/frontend/dist"
SPA="$ROOT/src/grant_lab/web/spa"

# ROOT is derived from BASH_SOURCE, so a symlinked scripts/ dir resolves it to the
# wrong tree. Cheap positive identity check before anything destructive.
[ -d "$ROOT/src/grant_lab/web" ] ||
  { echo "FATAL: $ROOT is not the repo root (no src/grant_lab/web)" >&2; exit 1; }
[ -f "$DIST/index.html" ] || { echo "FATAL: no build at $DIST — run a build first" >&2; exit 1; }

# Everything above this line is validation. Nothing destructive may move above it:
# a script that deletes before it validates destroys the served tree on the way to
# failing. tests/architecture/test_package_spa.py mutates exactly that.
rm -rf "$SPA"
mkdir -p "$SPA"
cp -R "$DIST"/. "$SPA"/

echo "packaged $(du -sb "$SPA" | cut -f1) bytes into src/grant_lab/web/spa"
```
The detector test needs at least 3 flagged lines, including `rm -rf` and `cp -R` (READ). It still gets exactly 3.

### `frontend/Dockerfile`

Replace lines 28-40 with:
```dockerfile
# The bundle under /build/dist is copied out by CI (ci.yml `package` job) and
# packaged through scripts/package_spa.sh, the only writer of the served tree.
RUN pnpm run build   # outDir = /build/dist (vite.config.ts -> resolve(__dirname, "dist"))
```

### Other scripts

- **`scripts/serve_spa_static.mjs`:** keep. Lines 6-21 become:
  ```
  // Why this exists: frontend/e2e/match.spec.ts drives the production bundle in a
  // real browser with the API mocked in-page. That needs no Postgres, uvicorn or
  // seeded user (the real-backend smoke, scripts/e2e_run.sh, does), so this is
  // zero-dependency Node stdlib and starts instantly.
  //
  // HOW IT DIFFERS FROM PRODUCTION, stated so nobody reads more into a green run:
  // FastAPI's web/spa_serving.py serves index.html only for routes the generated
  // manifest OWNS and returns a real JSON 404 for anything else. This serves
  // index.html for any GET whose path has no file extension. It is NOT a test of
  // the manifest's ownership rules; frontend/src/router.test.tsx and
  // frontend/e2e/smoke.spec.ts cover those.
  ```
- **`scripts/gen_openapi_types.sh`:** delete lines 35-39 (the `APP_EDITION=full` comment) and line 46 (`APP_EDITION=full \`). Keep everything else.
- **`scripts/check_match_bundle.mjs`:** delete. **`scripts/collab_e2e_run.sh` and `scripts/collab_e2e_seed.py`:** move as in §2. **`frontend/scripts/*`:** delete.

### OpenAPI types (READ)

- **How it runs:** `bash scripts/gen_openapi_types.sh` from the repo root (or `pnpm -C frontend gen:api`). The script itself pins `EMBEDDING_MODEL=bge-m3`, `SPA_MODE=disabled`, dummy DB/secret env and `--python 3.12`.
- **The stdout gotcha:** `create_app().openapi()` is dumped to STDOUT into a temp file, so any stray stdout line corrupts the JSON. The local `.env`'s `mxbai` model prints a calibration WARNING; the pinned `EMBEDDING_MODEL` is what stops it.
- **What it needs:** `uv` (run `uv sync` in the worktree first) and frontend deps, for `openapi-typescript`.
- **What checks sync:**
  - The CI `frontend` step "OpenAPI types in sync (drift check)": `bash scripts/gen_openapi_types.sh && git diff --exit-code frontend/src/api/schema.gen.ts`.
  - `tests/architecture/test_decision_routes_require_a_profile.py`, which reads `schema.gen.ts`.
- **Order:** regenerate only after the backend has removed the full-only routers. After §1.3, no kept file references `AdminDraftingOut`, `DeletedWorkspace*` or a workspace path, so tsc passes against both the old and the new schema (MEASURED for the old one).

---

## 4. Operational docs

### `README.md` (exact replacements)

**Lines 8-10:**
> The proposal-writing workspace that used to ship alongside the dashboard is not part of this branch. It is preserved, unmaintained, on the `workspace` branch and the `workspace-v1` tag (`docs/superpowers/specs/2026-11-03-workspace-removal-design.md`). Its database tables and its files on disk are kept.

**Other single-line edits:**
- Delete lines 18-19.
- Line 20 becomes `- Administration for users, source health, queues, and ranking evaluation.`
- Line 27 becomes `- **Frontend:** React, TypeScript, Vite, Mantine, and TanStack Query.`
- Line 29 becomes `- **Hosted model:** Anthropic, optionally enabled for rank escalation.`
- Line 32 becomes `login rate limiters are per-process state.` (drop "live-editing rooms, drafting subprocesses and").
- Lines 51-52 become `bootstrap need the configured local models.`
- Lines 105-107 become:
  ```
  # Optional: hosted re-ranking of the strongest matches.
  # ANTHROPIC_API_KEY=
  ```
- Line 148: "proxies API and authentication traffic to FastAPI on port 8000."
- Delete lines 206-212 (the `DRAFTING_ENABLED` block).

**Lines 229-233:**
```bash
cd frontend
pnpm run lint
pnpm run test
pnpm run build
pnpm run e2e:match           # production bundle in Chromium, API mocked
cd ..
bash scripts/e2e_run.sh      # real backend: testcontainers Postgres + uvicorn + Vite
```

**Lines 237-313** become:
````markdown
### Build and package

`vite build` writes to `frontend/dist`, and **nothing copies it into the directory
FastAPI serves**. Run the packaging script — it is the only supported writer of
`src/grant_lab/web/spa/`:

```bash
pnpm -C frontend install --frozen-lockfile
pnpm -C frontend build
pnpm -C frontend run package
```

Do **not** hand-copy a bundle into `src/grant_lab/web/spa/`;
`tests/architecture/test_package_spa.py::test_no_other_writer_of_the_served_tree`
fails if a second writer appears.

Building in Docker:

```bash
docker build -f frontend/Dockerfile -t grant-lab-spa .
```

Extract the result to `frontend/dist` and run `./scripts/package_spa.sh` — do not
`docker cp` straight into the served directory.

### Configure

<lines 287-295 unchanged: SPA_MODE=prod, https APP_BASE_URL, SESSION_SECRET, migrate, one uvicorn>

No `ANTHROPIC_API_KEY` is required; leaving it unset keeps the hosted tier off and
ranking runs entirely on the local Ollama. `APP_EDITION`, `DRAFTING_ENABLED` and the
other workspace settings are no longer read; lines for them left in an existing `.env`
are ignored.
````

**Lines 315-323:**
> `--timeout-graceful-shutdown` bounds how long Uvicorn waits for in-flight requests. The lifespan teardown then waits for scheduler ticks, so give the service manager a stop timeout above that: `TimeoutStopSec=120` in a systemd unit.
>
> Keep the database on persistent storage. Configure the reverse proxy to cap request bodies and set the trusted-proxy settings to match the real proxy topology.

(INFERRED: the backend removal also removes drafting-subprocess teardown.)

**Lines 327-344:**
> Three things hold state, and all three must be in the backup:
> - **PostgreSQL** — every table, including the tables of the removed workspace, which keep their data. `pg_dump` of the database is sufficient.
> - **The archived workspace files** — `var/workspaces` by default, or wherever `WORKSPACES_DIR` pointed. The app no longer reads or writes them, but they are the only copy of the proposal git histories and uploaded references, and the `workspace` branch needs them to be restored.
> - **`.env`** — the configuration, including the session-signing secret. Rotating `SESSION_SECRET` signs every user out.
>
> Nothing is pruned automatically. The database grows with calls and rankings.

**Line 355:** `var/   Local data, including the archived workspace files; ignored and confidential`

### `frontend/README.md`

**Lines 3-6:**
> It owns all browser page routes: the calls triage feed and call pages, profiles, boards (including the public corridor wall), admin, auth, and settings. FastAPI provides the JSON APIs, OAuth endpoints and production bundle serving.

**Lines 37-53:**
````markdown
## Build

```bash
pnpm run build      # tsc typecheck + vite build -> frontend/dist/
pnpm run package    # scripts/package_spa.sh: frontend/dist/ -> src/grant_lab/web/spa/
```

`package` is the only supported writer of `src/grant_lab/web/spa/`, which FastAPI
serves when `SPA_MODE=prod`. CI builds the same bundle in the secretless container
(`frontend/Dockerfile`) and packages it through the same script.

## Tests

```bash
pnpm run test       # vitest
pnpm run lint       # oxlint over src/ and e2e/
pnpm run e2e:match  # the production bundle in Chromium, API mocked in-page
bash ../scripts/e2e_run.sh   # real backend: testcontainers Postgres + uvicorn (+ Vite)
```
````

**Lines 55-63** (they claim the e2e is "deferred"; it is wired):
````markdown
## Two-process end-to-end

The dev front-door routing decision is unit-tested in `src/dev-proxy.test.ts`. The
two-process path (login and logout POSTs through Vite, an unknown path reaching
FastAPI's JSON 404, a call page surviving a reload) is `e2e/smoke.spec.ts`, run by
`scripts/e2e_run.sh` in CI's `e2e` job in both `dev` and `prod` modes.
````

### `CLAUDE.md`

- **Line 3:** replace the last sentence with:
  > The proposal-assist workspace was removed from master (`docs/superpowers/specs/2026-11-03-workspace-removal-design.md`); it is frozen on the `workspace` branch and the `workspace-v1` tag, and its tables and on-disk files are kept but no longer read or written.
- **Lines 35-37:** "…every FK and ON DELETE, and every cap setting." Delete "the fence primitives with their contracts, the armed-gate allowlist sizes,": both come from `proposal_assist/fencing.py` and `tests/fencing/` (READ: the fence inventory covers only `proposal_assist` and `web/api/workspaces.py`).
- **Lines 77-81:**
  > - **`0 skipped` is the expectation. The pass total is not.** The only skip marker left is `tests/test_secret_file_exclusion.py`'s `skipif`, which needs a Docker daemon; this machine has one, so a skip here means Docker was unreachable. (Until the workspace removal the expectation was `1 skipped`: a real-SDK drafting test gated on `ANTHROPIC_API_KEY` that left with the workspace.) Read a different total as "a slice merged".

  READ: that `skipif` is the only skip marker or xfail outside `tests/proposal_assist`. "0 skipped" is PREDICTED; measure it after the removal before committing this line.
- **Line 91:**
  > It owns the calls triage feed and call pages, profiles, boards (including the public corridor wall), admin, auth, and settings pages.
- **Line 92:**
  > `prod` (FastAPI serves the bundle that `pnpm -C frontend build && pnpm -C frontend run package` put in `src/grant_lab/web/spa/` — **prod MUST ship a built SPA**)
- **Line 101:** delete the Citation audit line (READ: `citation_llm_provider` is read only by `proposal_assist/citations/llm.py`).
- **Line 105:** delete the proposal-assist drafting line.
- Lines 9 and 65 are historical examples or measurements; leave them.

### `scripts/gen_architecture.py` (READ)

The 2 lines are:
- **Line 24:** `FENCING = …/proposal_assist/fencing.py`, read by `parse_fence_primitives()` (line 165). It crashes once the package is gone.
- **Line 284:** the heading text of the generated "## The epoch fence" section.

Exact edit: delete line 24 (`FENCING`), line 25 (`INVENTORY`, since `tests/fencing/` is workspace-only), `parse_fence_primitives` and `parse_gate_allowlists` (lines 164-191), and the `# --- fence` block (lines 281-303). Line 4's "without the drafting SDK" can go too. Then regenerate `docs/ARCHITECTURE.md`.

**Separate defect, also found (READ):** `_model_modules()` (lines 67-77) resolves `from grant_lab.db.models.<tail>` only to a flat `models/<tail>.py` and globs `*.py` non-recursively. Once the workspace tables move into the `db/models/workspace/` subpackage, all 31 of them silently drop out of `ARCHITECTURE.md`, and its assert does not fire. Fix: resolve the module path from `REPO/"src"`, follow the subpackage's `__init__` imports, and `rglob` the found-set.

---

## Backend tests coupled to these edits (owner: backend helper; READ)

- **`tests/architecture/test_package_spa.py`:**
  - Its fixture copies `check_match_bundle.mjs`.
  - These tests go: the edition and gate tests, the bundle-gate mechanism tests and `test_the_container_build_does_not_hard_code_the_edition`.
  - Line 248's name set becomes `e2e_run.sh` instead of `collab_e2e_run.sh`.
  - The not-repo-root test should delete `src/grant_lab/web` instead of the `.mjs`.
  - Add a validation-before-delete case (missing `dist/index.html` leaves the served tree intact).
- **`test_collab_e2e_run_cleanup.py`:** `SCRIPT` becomes `scripts/e2e_run.sh`; rename the file.
- **`test_match_gates_are_wired.py`:**
  - Delete `test_gate_1_the_bundle_check_runs_in_ci` and `test_the_match_direction_of_the_router_alias_runs_in_ci`.
  - Suggested: add a wiring gate asserting that `ci.yml` runs `bash scripts/e2e_run.sh` and that the runner defaults to `e2e/smoke.spec.ts`.
- **`test_spa_edition_match.py`:** goes with the `edition.txt` check.
- **`tests/test_config.py:280`:** still passes because the seed keeps the secret literal; update the comment to name `scripts/e2e_seed.py`.
- **`test_secret_file_exclusion.py:84`:** parametrises `frontend/.env.example`. It checks ignore rules, not existence, so it should still pass (INFERRED); drop that parameter.

## Open questions

1. **`routes.json`:** dropping `/cockpit` regenerates the backend's `spa_routes.py` too, and the new reverse guard in `router.test.tsx` needs it. Agree with the backend helper who commits this. Dropping `/workspaces` is optional and documentation-only.
2. **`noCockpitLinks.test.tsx`:** keep it in simplified form (recommended) and maybe rename it, or delete it?
3. **`index.html`:** I kept `data-mantine-color-scheme="dark"` so the app is byte-identical before hydration. Flipping it to `light` is a separate UX change.
4. **`StructuredFields` `part="all"`** is now used only by its own test. Remove it in piece 5?
5. **Package job "Set up Node":** removing it is optional (INFERRED unused).
6. **`pnpm -C frontend package` vs `run package`:** I used `run` everywhere to avoid any collision with a pnpm built-in.
7. **Prod-mode `e2e_run.sh` overwrites `src/grant_lab/web/spa`.** Run it only in the worktree. The same hazard existed before.
8. **Smoke a11y on the real feed:** "no critical violations" is PREDICTED. G7 still needs logout broken once on purpose and the red run recorded.
9. **Should `api/dashboard.ts` keep the `archive` view type?** The server still serves it as the no-profile default and TriageFeed maps it to `inbox`. I left it, since the spec says the match feed does not change.
