# React SPA Foundation — Plan 3: The React App (client, shell, dashboard, SSE hook, e2e)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the React app over the Plan-1 `/api` and Plan-2 serving: a typed API client (openapi-fetch) + TanStack Query, React Router (Data Mode) shell with an error boundary, design tokens split from `app.css`, the dashboard ported to parity (filters/facets/pagination, `{pending, failed}` ranking with conditional polling + drain refetch, deep-link read-only, explicit-selection POST), a reusable `useEventStream` hook validated against a dev-gated `/events/ping`, native-form logout, and a dual-mode Playwright smoke. The final task flips `/` to the SPA and retires the Jinja dashboard.

**Architecture:** Client-rendered React, same-origin over FastAPI; the session cookie authenticates every request and the SSE connection. Server data flows through TanStack Query; the typed client is generated from FastAPI's OpenAPI schema with a CI drift check. SSE is validated at contract level only (mocked `EventSource` + the `/events/ping` backend test); nothing shipping here depends on SSE at runtime (the dashboard polls).

**Tech Stack:** React 18 + TS + Vite, React Router (Data Mode), TanStack Query, openapi-typescript + openapi-fetch, Vitest + React Testing Library, Playwright; FastAPI `StreamingResponse` for `/events/ping`.

**Depends on:** Plans 1 & 2.

---

## Rev 2 — review fixes (apply these; from the multi-agent plan review)

- **[HIGH] Task 9 `/`-flip: invert the proxy via the manifest, and DON'T remove the Jinja `/` until React serves `/`.** Re-sequence Task 9 as: (1) flip `routes.json` `spa_routes` → `["/"]` and regenerate (Plan 2's generator now drives the prod `is_spa_route` AND the dev proxy from `BACKEND_PREFIXES`); (2) point the React Router route at `/`; (3) build + **verify** — dev: `GET /` serves the SPA, `/login` + `/calls/1` reach FastAPI, unknown → FastAPI JSON 404; prod: catch-all serves index for `/` but a real 404 for `/api/x`; (4) **only then** delete the Jinja dashboard handler (`src/call_manager/web/routes/calls.py:60 @router.get("/")`), keeping `dashboard.build_dashboard_view` for `/api/calls`. No hand-edited Vite regex — the proxy is generated.
- **[HIGH] Task 9 Step 5: remove only the `GET /` tests.** Deleting `calls_index` makes `GET /` 404 under `SPA_MODE=disabled` (tests), so the `GET /` cases in `tests/test_web_calls*.py` can't pass. FIX: delete/replace just those GET-`/` Jinja tests (read-model parity is covered by `tests/test_dashboard_readmodel.py` + `tests/test_api_calls.py`); keep `/calls/{id}` + ranking-status tests.
- **[MED] Task 7 `/events/ping` must be `async` + sleep + honor `?since`** (today it busy-loops on `max=0`; `if max: continue` is a no-op; `?since` catch-up is unimplemented):
  ```python
  @router.get("/events/ping")
  async def ping(request: Request, max: int = Query(0, ge=0), since: int | None = Query(None)):
      start = since + 1 if since is not None else 0
      last = request.headers.get("last-event-id")
      if last and last.isdigit(): start = int(last) + 1
      async def gen():
          n, sent = start, 0
          while max == 0 or sent < max:
              yield f"id: {n}\nevent: ping\ndata: {json.dumps({'n': n})}\n\n".encode()
              n += 1; sent += 1
              if max == 0: await asyncio.sleep(2)
      return StreamingResponse(gen(), media_type="text/event-stream",
                               headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
  ```
  (`import asyncio`.)
- **[MED] Task 6 `setDefaultProfile` must not be fire-and-forget.** Use a TanStack `useMutation` with `onError` (surface failure; don't update the URL/selection if the persist fails) and invalidate `["me"]` on success.
- **[MED] Task 9 add the spec's missing e2e assertions:** dev unknown-path → FastAPI JSON 404; logout-through-Vite lands on `/login`. Build the Playwright login helper from the password-login flow in `tests/test_web_login_password.py`.

## Rev 3 — supply-chain hardening: pnpm commands (per the 2026-06-16 supply-chain spec)

Per the [supply-chain spec](../specs/2026-06-16-supply-chain-security-design.md) §3.1/§8 and Plan 2 Rev 3, this plan's `npm`/`npx` commands are overridden to **pnpm** (package-manager surface only — no logic changes):

- Task 1: `npm install …` → `pnpm add …`; dev deps → `pnpm add -D …`; `npx playwright install` → `pnpm exec playwright install`.
- Task 2: `npm run gen:api` → `pnpm gen:api`; CI drift check uses pnpm; every documented `uvicorn … --factory` command must set required env (`DATABASE_URL`, `SESSION_SECRET`, `ALLOWED_EMAIL_DOMAINS`).
- Tasks 5/6/7/9: `npx vitest …` → `pnpm exec vitest …`; `npm run build`/`dev` → `pnpm build`/`pnpm dev`; `npx playwright test` → `pnpm exec playwright test`.
- Lockfile is `pnpm-lock.yaml`; installs are `--frozen-lockfile` with lifecycle scripts off (Plan 2 Rev 3).

## Rev 4 — task-body reconciliation (Codex)

The Rev 2/Rev 3 decisions above were folded **into the canonical task bodies** so an implementation agent following the checkbox steps + snippets builds the fixed version, not the stale one. Specifically: Task 9 re-sequenced (flip+regenerate → React `/` → build+verify → THEN delete Jinja `/`), proxy is **manifest-generated (no hand-edit)**, server-authoritative manifest exact-match (React Router `*` is an in-app soft-404 only, never a 200 for unknown paths); Task 6 `setDefaultProfile` → awaited `useSetDefaultProfile` TanStack mutation (invalidates `["me"]`, reflects URL only on success); Task 7 `/events/ping` made `async` + sleep + `?since`/`?cursor` + `Last-Event-ID`, and `useEventStream` gained fresh-connect cursor + `Last-Event-ID` + single-flight `/api/me` probe with capped backoff + 401/403/200 outcomes; Task 9 e2e de-placeholdered with real login + the three Rev 2 serving assertions; all `npm`/`npx` → pnpm. The Rev 2/Rev 3 headers are retained as history; the **task bodies are authoritative**.

## File structure

- Modify `frontend/package.json` — add `react-router-dom`, `@tanstack/react-query`, `openapi-fetch`; dev: `openapi-typescript`, `@playwright/test`, `@axe-core/playwright`.
- Create `frontend/src/api/schema.gen.ts` — **generated** OpenAPI types.
- Create `frontend/src/api/client.ts` — openapi-fetch client + envelope unwrap.
- Create `frontend/src/api/session.ts` — `useSession()` (401 → `/login`).
- Create `frontend/src/api/events.ts` — `useEventStream()`.
- Create `frontend/src/styles/tokens.css` — extracted design tokens.
- Create `frontend/src/components/` — `Button.tsx`, `Select.tsx`, `Card.tsx`, `Pagination.tsx`, `ErrorBoundary.tsx` (+ `*.module.css`).
- Create `frontend/src/routes/Dashboard.tsx` — the ported dashboard.
- Create `frontend/src/App.tsx` (replace), `frontend/src/router.tsx` — shell + Data-Mode router.
- Create `src/call_manager/web/api/events.py` — dev-gated `GET /events/ping`.
- Modify `frontend/routes.json` + regenerate; remove the Jinja `/` handler (final task).
- Tests: `frontend/src/**/*.test.tsx` (Vitest), `frontend/e2e/dashboard.spec.ts` (Playwright), `tests/test_events_ping.py` (pytest).

---

## Task 1: Add libraries + split design tokens

**Files:** Modify `frontend/package.json`; Create `frontend/src/styles/tokens.css`

- [ ] **Step 1: Install deps**

Run (pnpm, frozen/exact-pinned per Plan 2 Rev 3 — `save-exact`/`save-prefix=''` already set, lifecycle scripts off):
```bash
cd frontend
pnpm add react-router-dom @tanstack/react-query openapi-fetch
pnpm add -D openapi-typescript @playwright/test @axe-core/playwright
pnpm exec playwright install --with-deps chromium
cd ..
```

- [ ] **Step 2: Extract tokens**

Copy the custom-property declarations from `src/call_manager/web/static/app.css` (the `[data-theme="light"], :root:not([data-theme="dark"])` block, app.css:17-~60) into `frontend/src/styles/tokens.css` verbatim (same selector so values match and Pico isn't overridden). Import it once in `frontend/src/main.tsx`:

```tsx
import "./styles/tokens.css";
```

- [ ] **Step 3: Commit**

```bash
git add frontend/package.json frontend/pnpm-lock.yaml frontend/src/styles/tokens.css frontend/src/main.tsx
git commit -m "feat(spa): add router/query/openapi-fetch libs + tokens.css"
```

---

## Task 2: Generate OpenAPI types + CI drift check

**Files:** Create `frontend/src/api/schema.gen.ts` (generated); add a package script + CI step

- [ ] **Step 1: Add the generate script**

In `frontend/package.json` scripts:

```json
"gen:api": "openapi-typescript http://localhost:8000/openapi.json -o src/api/schema.gen.ts"
```

- [ ] **Step 2: Generate (backend must be running with the Plan-1 /api routes)**

Run (every `uvicorn … --factory` recipe sets the env `config.py` requires — `DATABASE_URL`, `SESSION_SECRET`, `ALLOWED_EMAIL_DOMAINS` — or the app won't start; Plan 2 Rev 2):
```bash
DATABASE_URL=postgresql://… SESSION_SECRET=test-secret-at-least-16-chars ALLOWED_EMAIL_DOMAINS=ki.se \
  SPA_MODE=disabled APP_BASE_URL=http://localhost:8000 \
  uv run uvicorn call_manager.main:create_app --factory --port 8000 &
sleep 3 && cd frontend && pnpm gen:api && cd .. && kill %1
test -f frontend/src/api/schema.gen.ts && echo "TYPES OK"
```
Expected: `TYPES OK`; `schema.gen.ts` contains `paths` with `/api/me`, `/api/calls`, `/api/me/default-profile`.

- [ ] **Step 3: Add the CI drift check**

In CI (after the pnpm frozen install): regenerate and fail if the committed file changed. **Start uvicorn, generate, and diff must all live in ONE `run` block** — a process backgrounded in one GitHub Actions step does NOT survive into the next step (each step is a fresh shell; the orphan is reaped), so the generate step would hit no server. Use a `trap` for cleanup and poll `/healthz` for readiness instead of a blind `sleep`:

```yaml
- run: |
    DATABASE_URL=$DATABASE_URL SESSION_SECRET=$SESSION_SECRET ALLOWED_EMAIL_DOMAINS=ki.se \
      SPA_MODE=disabled APP_BASE_URL=http://localhost:8000 \
      uv run uvicorn call_manager.main:create_app --factory --port 8000 &
    pid=$!
    trap 'kill "$pid" 2>/dev/null || true' EXIT
    for i in $(seq 1 30); do curl -sf http://localhost:8000/healthz && break; sleep 1; done
    cd frontend && pnpm gen:api && cd ..
    git diff --exit-code frontend/src/api/schema.gen.ts   # fails on drift
```

- [ ] **Step 4: Commit**

```bash
git add frontend/package.json frontend/src/api/schema.gen.ts .github/workflows/
git commit -m "feat(spa): generate OpenAPI TS types + CI drift check"
```

---

## Task 3: Typed API client + `useSession`

**Files:** Create `frontend/src/api/client.ts`, `frontend/src/api/session.ts`; Test: `frontend/src/api/client.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/api/client.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import { unwrap } from "./client";

describe("unwrap", () => {
  it("returns data on success", () => {
    expect(unwrap({ data: { ok: true }, error: undefined })).toEqual({ ok: true });
  });
  it("throws the envelope on error", () => {
    expect(() => unwrap({ data: undefined, error: { error: { code: "forbidden", message: "no" } } }))
      .toThrowError(/forbidden/);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/api/client.test.ts`
Expected: FAIL — `./client` not found.

- [ ] **Step 3: Implement the client + unwrap**

```ts
// frontend/src/api/client.ts
import createClient from "openapi-fetch";
import type { paths } from "./schema.gen";

// Same-origin: the httpOnly session cookie is sent automatically.
export const api = createClient<paths>({ baseUrl: "", credentials: "same-origin" });

export class ApiError extends Error {
  code: string;
  constructor(code: string, message: string) { super(message); this.code = code; }
}

export function unwrap<T>(res: { data?: T; error?: unknown }): T {
  if (res.error) {
    const e = res.error as { error?: { code?: string; message?: string } };
    throw new ApiError(e.error?.code ?? "error", e.error?.message ?? "request failed");
  }
  return res.data as T;
}
```

- [ ] **Step 4: Implement `useSession`**

```ts
// frontend/src/api/session.ts
import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "./client";

export function useSession() {
  return useQuery({
    queryKey: ["me"],
    queryFn: async () => {
      const res = await api.GET("/api/me", {});
      if (res.response.status === 401) {
        window.location.assign("/login");           // full-page nav to server-rendered login
        throw new Error("unauthenticated");
      }
      return unwrap(res);
    },
    retry: false,
    staleTime: 60_000,
  });
}
```

- [ ] **Step 5: Run to verify it passes**

Run: `cd frontend && pnpm exec vitest run src/api/client.test.ts`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/client.ts frontend/src/api/session.ts frontend/src/api/client.test.ts
git commit -m "feat(spa): typed api client + unwrap + useSession (401 -> /login)"
```

---

## Task 4: App shell + React Router Data Mode + error boundary

**Files:** Create `frontend/src/router.tsx`, `frontend/src/components/ErrorBoundary.tsx`; Replace `frontend/src/App.tsx`

- [ ] **Step 1: Error boundary**

```tsx
// frontend/src/components/ErrorBoundary.tsx
import { Component, type ReactNode } from "react";

export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  render() {
    if (this.state.error) {
      return (
        <main role="alert">
          <h1>Something went wrong</h1>
          <p>{this.state.error.message}</p>
          <button onClick={() => this.setState({ error: null })}>Try again</button>
        </main>
      );
    }
    return this.props.children;
  }
}
```

- [ ] **Step 2: Router + shell**

```tsx
// frontend/src/router.tsx
import { createBrowserRouter } from "react-router-dom";
import { Dashboard } from "./routes/Dashboard";
import { ErrorBoundary } from "./components/ErrorBoundary";

export const router = createBrowserRouter([
  // SPA-owned page route. v1 mounts at the temporary `/app` (Plan 2 manifest);
  // Task 9 flips this to `/`. Keep this table in sync with `routes.json` —
  // the SERVER (manifest) is authoritative for what gets index.html.
  { path: "/app", element: <Dashboard /> },
  // In-app catch-all ONLY: a soft 404 for paths the SPA reached client-side via
  // React Router navigation. It is NOT the route authority — the server returns
  // index.html only for manifest SPA routes, so a typed/unknown URL never loads
  // the SPA (it hits FastAPI's real 404). This MUST NOT turn unknown paths into 200s.
  { path: "*", element: <main role="alert"><h1>Not found</h1></main> },
]);
```

```tsx
// frontend/src/App.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router-dom";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { router } from "./router";

const qc = new QueryClient({ defaultOptions: { queries: { refetchOnWindowFocus: true } } });

export function App() {
  return (
    <ErrorBoundary>
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
```

> The router `path` is `/app` during Plan 3 to match the Plan-2 manifest; Task 9 switches it to `/`. The `*` route is an in-app fallback for client-side React Router navigations only — the manifest-driven server is authoritative and returns `index.html` solely for SPA routes, so unknown/typed URLs reach FastAPI's real 404 (they never load the SPA).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/router.tsx frontend/src/components/ErrorBoundary.tsx frontend/src/App.tsx
git commit -m "feat(spa): app shell + React Router Data Mode + error boundary"
```

---

## Task 5: Design-system base components

**Files:** Create `frontend/src/components/{Button,Select,Card,Pagination}.tsx` (+ `.module.css`); Test: one component test

Build only what the dashboard needs (Button, Select, Card, Pagination), styled from `tokens.css` custom properties. Defer Modal/Drawer/Toast (Radix when needed). Keep markup/classes parity-faithful to `calls_index.html`.

- [ ] **Step 1: Failing component test**

```tsx
// frontend/src/components/Button.test.tsx
import { render, screen } from "@testing-library/react";
import { Button } from "./Button";

it("renders its label and handles disabled", () => {
  render(<Button disabled>Save</Button>);
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/components/Button.test.tsx`
Expected: FAIL — `./Button` not found. (Add `import "@testing-library/jest-dom"` setup or use `toBeDisabled` via `@testing-library/jest-dom`; install `@testing-library/jest-dom` and reference it in a `vitest.setup.ts` if needed.)

- [ ] **Step 3: Implement Button (others analogous)**

```tsx
// frontend/src/components/Button.tsx
import type { ButtonHTMLAttributes } from "react";
import styles from "./Button.module.css";

export function Button(props: ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button className={styles.btn} {...props} />;
}
```

```css
/* frontend/src/components/Button.module.css */
.btn { background: var(--cm-primary); color: #fff; border: 1px solid var(--cm-primary-border);
  border-radius: 8px; padding: 0.5rem 0.9rem; cursor: pointer; }
.btn:hover { background: var(--cm-primary-hover); }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
```

Create `Select.tsx`, `Card.tsx`, `Pagination.tsx` the same way (native elements + token-based module CSS). `Pagination` takes `{ page, hasPrev, hasNext, onPage }`.

- [ ] **Step 4: Run to verify it passes**

Run: `cd frontend && pnpm exec vitest run src/components/Button.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components frontend/vitest.setup.ts 2>/dev/null; git commit -m "feat(spa): token-based base components (Button/Select/Card/Pagination)"
```

---

## Task 6: Dashboard page (parity)

**Files:** Create `frontend/src/routes/Dashboard.tsx`, `frontend/src/api/dashboard.ts`; Test: `frontend/src/routes/Dashboard.test.tsx`

Behavior to match: filters (source/sort/show_closed) + profile select drive the query; **URL params are read-only view state** (deep link does not persist the default); **selecting a profile** fires `POST /api/me/default-profile` (Plan 1) via a TanStack `useMutation` — **awaited, with error handling**, invalidating `["me"]` on success, and the URL/selection is reflected **only after the persist succeeds** (NOT fire-and-forget); **ranking** polls only while `pending > 0`, shows `failed` without polling, and refetches on the `pending → 0` drain; rows link to the un-ported `/calls/{id}` via plain anchors.

- [ ] **Step 1: Data hook**

```ts
// frontend/src/api/dashboard.ts
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api, unwrap } from "./client";

export type DashboardParams = {
  source?: string; sort?: "newest" | "deadline"; profile?: string;
  page?: number; show_closed?: boolean;
};

export function useDashboard(params: DashboardParams) {
  const qc = useQueryClient();
  const prevPending = useRef<number | null>(null);
  const q = useQuery({
    queryKey: ["calls", params],
    queryFn: async () => unwrap(await api.GET("/api/calls", { params: { query: params } })),
    placeholderData: keepPreviousData,
    // Poll ONLY while ranking is pending.
    refetchInterval: (query) => {
      const pending = (query.state.data as any)?.ranking?.pending ?? 0;
      return pending > 0 ? 5000 : false;
    },
  });
  // Drain refetch: when pending transitions >0 -> 0, refresh so new scores appear.
  useEffect(() => {
    const pending = (q.data as any)?.ranking?.pending ?? 0;
    if (prevPending.current && prevPending.current > 0 && pending === 0) {
      qc.invalidateQueries({ queryKey: ["calls"] });
    }
    prevPending.current = pending;
  }, [q.data, qc]);
  return q;
}

// Explicit preference write (Plan 1: POST /api/me/default-profile). A TanStack
// mutation — awaited by the caller, invalidates ["me"] on success so the cached
// session reflects the new sticky default. NOT fire-and-forget.
export function useSetDefaultProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (profileId: number | null) =>
      unwrap(await api.POST("/api/me/default-profile", { body: { profile_id: profileId } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["me"] }),
  });
}
```

- [ ] **Step 2: Failing component test (deep-link read-only + selection persists)**

```tsx
// frontend/src/routes/Dashboard.test.tsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { vi, it, expect } from "vitest";
import { Dashboard } from "./Dashboard";
import * as clientMod from "../api/client";

function renderAt(url: string) {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}><Dashboard /></MemoryRouter>
    </QueryClientProvider>,
  );
}

it("deep link does NOT POST default; selecting a profile DOES", async () => {
  const GET = vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { items: [], summary: { open_count: 0, high_score: 0, soon_deadline: 0 },
      pagination: { page: 1, limit: 50, has_prev: false, has_next: false, total: 0 },
      ranking: { pending: 0, failed: 0 }, effective: {}, facets: { sources: [], profiles: [{ id: 7, name: "G" }] } },
    error: undefined, response: new Response(),
  } as any);
  const POST = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { ok: true }, error: undefined, response: new Response() } as any);

  renderAt("/?profile=7");                       // deep link
  await waitFor(() => expect(GET).toHaveBeenCalled());
  expect(POST).not.toHaveBeenCalled();           // read-only

  fireEvent.change(await screen.findByLabelText(/profile/i), { target: { value: "7" } });
  await waitFor(() => expect(POST).toHaveBeenCalledWith("/api/me/default-profile", { body: { profile_id: 7 } }));
});
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/routes/Dashboard.test.tsx`
Expected: FAIL — `Dashboard` not implemented.

- [ ] **Step 4: Implement the Dashboard**

```tsx
// frontend/src/routes/Dashboard.tsx
import { useSearchParams } from "react-router-dom";
import { useDashboard, useSetDefaultProfile, type DashboardParams } from "../api/dashboard";
import { Pagination } from "../components/Pagination";

export function Dashboard() {
  const [sp, setSp] = useSearchParams();
  const params: DashboardParams = {
    source: sp.get("source") ?? undefined,
    sort: (sp.get("sort") as "newest" | "deadline") ?? undefined,
    profile: sp.get("profile") ?? undefined,
    page: sp.get("page") ? Number(sp.get("page")) : 1,
    show_closed: sp.get("show_closed") === "1",
  };
  const { data, isLoading, isError } = useDashboard(params);
  const setDefault = useSetDefaultProfile();

  async function onSelectProfile(value: string) {
    // Explicit selection: PERSIST FIRST (awaited), then reflect the view only on success.
    try {
      await setDefault.mutateAsync(value ? Number(value) : null);
    } catch {
      return;  // persist failed → leave URL/selection unchanged (error surfaced below)
    }
    const next = new URLSearchParams(sp);
    if (value) next.set("profile", value); else next.delete("profile");
    setSp(next);
  }

  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <p role="alert">Couldn't load calls.</p>;

  return (
    <main>
      <header>
        <label>Profile
          <select aria-label="profile" value={params.profile ?? ""}
            disabled={setDefault.isPending}
            onChange={(e) => onSelectProfile(e.target.value)}>
            <option value="">— No profile —</option>
            {data.facets.profiles.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </label>
        {setDefault.isError && <p role="alert">Couldn't save the default profile.</p>}
        {/* source/sort/show_closed controls update sp the same way (no POST). */}
      </header>

      {data.ranking.pending > 0 && <p>Scoring {data.ranking.pending} calls…</p>}
      {data.ranking.failed > 0 && <p>{data.ranking.failed} calls couldn't be scored automatically.</p>}

      <ul>
        {data.items.map((c) => (
          <li key={c.id}>
            {/* plain anchor to the un-ported call-detail HTML page (coexistence) */}
            <a href={`/calls/${c.id}`}>{c.title}</a>
            {c.aggregate_score != null && <span> · {Math.round(c.aggregate_score)}</span>}
          </li>
        ))}
      </ul>

      <Pagination page={data.pagination.page as number}
        hasPrev={data.pagination.has_prev as boolean} hasNext={data.pagination.has_next as boolean}
        onPage={(p) => { const n = new URLSearchParams(sp); n.set("page", String(p)); setSp(n); }} />
    </main>
  );
}
```

(Render the remaining row fields — deadline, source, badges — to match `calls_index.html`. Add `@testing-library/jest-dom` setup if not already.)

- [ ] **Step 5: Run to verify it passes**

Run: `cd frontend && pnpm exec vitest run src/routes/Dashboard.test.tsx`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/routes/Dashboard.tsx frontend/src/api/dashboard.ts frontend/src/routes/Dashboard.test.tsx
git commit -m "feat(spa): dashboard page (deep-link read-only, selection persists, ranking poll+drain)"
```

---

## Task 7: `useEventStream` hook + dev-gated `/events/ping`

**Files:** Create `src/call_manager/web/api/events.py`, `frontend/src/api/events.ts`; Tests: `tests/test_events_ping.py`, `frontend/src/api/events.test.ts`

- [ ] **Step 1: Failing backend test**

```python
# tests/test_events_ping.py
from fastapi.testclient import TestClient
from call_manager.main import create_app


def _app(monkeypatch, pg_url, mode):
    for k, v in {"DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
                 "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver",
                 "SPA_MODE": mode}.items():
        monkeypatch.setenv(k, v)
    return create_app()


def test_ping_dev_gated(monkeypatch, pg_url, pg_session_factory):
    c = TestClient(_app(monkeypatch, pg_url, "disabled"))
    assert c.get("/events/ping").status_code == 404


def test_ping_emits_named_json_events_with_ids(monkeypatch, pg_url, pg_session_factory):
    c = TestClient(_app(monkeypatch, pg_url, "dev"))
    r = c.get("/events/ping?max=2")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"        # anti-buffering (Vite proxy hop)
    assert r.headers["x-accel-buffering"] == "no"
    body = r.text
    assert "event: ping" in body and "id: 0" in body and '"n": 0' in body


def test_ping_honors_since_for_fresh_connect_catch_up(monkeypatch, pg_url, pg_session_factory):
    # Fresh-connect catch-up: ?since=4 resumes at id 5 (browser sends no Last-Event-ID on a new EventSource).
    c = TestClient(_app(monkeypatch, pg_url, "dev"))
    body = c.get("/events/ping?max=2&since=4").text
    assert "id: 5" in body and "id: 0" not in body


def test_ping_last_event_id_wins_over_since(monkeypatch, pg_url, pg_session_factory):
    # In-session reconnect: Last-Event-ID overrides ?since.
    c = TestClient(_app(monkeypatch, pg_url, "dev"))
    body = c.get("/events/ping?max=1&since=4", headers={"last-event-id": "9"}).text
    assert "id: 10" in body
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_events_ping.py -v`
Expected: FAIL — `/events/ping` not implemented.

- [ ] **Step 3: Implement the endpoint (mounted only in dev)**

The endpoint MUST be `async` so the event loop isn't blocked, MUST `await asyncio.sleep` between events on an unbounded stream (no busy-loop), and MUST honor BOTH catch-up mechanisms — fresh-connect `?since`/`?cursor` (browsers omit `Last-Event-ID` on a brand-new `EventSource`, e.g. reload / SPA remount) AND in-session `Last-Event-ID` on reconnect — with `Last-Event-ID` winning when both are present. Anti-buffering headers are required because dev traffic routes through the Vite proxy (a buffering hop). (Spec lines 154-158, 119.)

```python
# src/call_manager/web/api/events.py
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

router = APIRouter()


@router.get("/events/ping")
async def ping(
    request: Request,
    max: int = Query(default=0, ge=0),
    since: int | None = Query(default=None),
    cursor: int | None = Query(default=None),
) -> StreamingResponse:
    """Dev-only SSE validation endpoint: replayable named JSON `ping` events
    with ids, so the client hook exercises typed events + ids + catch-up via
    BOTH fresh-connect ?since/?cursor and in-session Last-Event-ID."""
    # Fresh-connect catch-up: ?since (or ?cursor) sets the resume point.
    seed = since if since is not None else cursor
    start = seed + 1 if seed is not None else 0
    # In-session reconnect: Last-Event-ID wins when present (mirrors workspaces.py:302).
    last = request.headers.get("last-event-id")
    if last and last.isdigit():
        start = int(last) + 1

    async def gen() -> AsyncIterator[bytes]:
        n, sent = start, 0
        while max == 0 or sent < max:
            payload = json.dumps({"n": n})
            yield f"id: {n}\nevent: ping\ndata: {payload}\n\n".encode()
            n += 1
            sent += 1
            if max == 0:  # unbounded live stream: pace it, never busy-loop
                await asyncio.sleep(2)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

Mount it only in dev — in `src/call_manager/main.py`, where routers are included:

```python
    if settings.spa_mode == "dev":
        from call_manager.web.api import events as _events
        app.include_router(_events.router)
```

(The test uses `?max=2` to bound the stream; the unbounded `max == 0` branch sleeps between events so a real consumer streams without spinning the CPU.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_events_ping.py -v`
Expected: PASS.

- [ ] **Step 5: Implement `useEventStream` (mocked-EventSource unit test)**

The hook must (spec lines 155-158): (a) `addEventListener(type, …)` per NAMED event — named events do NOT reach `onmessage`; (b) pass a fresh-connect `?since`/`?cursor` when it (re)opens (browsers omit `Last-Event-ID` on a brand-new `EventSource` after reload / SPA remount) so the server resumes — tracking the last seen `id` from `MessageEvent.lastEventId` (the browser also replays it as `Last-Event-ID` for in-session reconnects); (c) NOT slam `/api/me` on every `onerror` — probe only after N consecutive errors / T seconds, **single-flight** (each probe opens a DB session), with **capped exponential backoff**; (d) branch on the probe outcome: **401 → `/login`**, **403 AccountInactive → a no-access state**, **200 → close nothing, let `EventSource` keep retrying** (transient drop).

```ts
// frontend/src/api/events.ts
import { useEffect, useRef } from "react";

type Handlers = Record<string, (data: unknown) => void>;
type Opts = {
  onExpired?: () => void;        // 401: session gone → caller routes to /login
  onNoAccess?: () => void;       // 403 AccountInactive → no-access state
  probeAfter?: number;           // N consecutive onerror before probing (default 3)
};

export function useEventStream(url: string, handlers: Handlers, opts?: Opts) {
  const lastId = useRef<string | null>(null);   // fresh-connect cursor + reconnect catch-up
  const errors = useRef(0);
  const probing = useRef(false);                 // single-flight guard
  const backoff = useRef(1000);                  // capped exponential backoff

  useEffect(() => {
    let es: EventSource | null = null;

    const open = () => {
      // Pass the cursor on EVERY (re)open so the server resumes from where we left off
      // even on a brand-new EventSource (reload / remount) where Last-Event-ID is absent.
      const u = new URL(url, window.location.origin);
      if (lastId.current) u.searchParams.set("since", lastId.current);
      es = new EventSource(u.toString(), { withCredentials: true });

      for (const [type, fn] of Object.entries(handlers)) {
        es.addEventListener(type, (e) => {       // named events don't reach onmessage
          const ev = e as MessageEvent;
          if (ev.lastEventId) lastId.current = ev.lastEventId;
          try { fn(JSON.parse(ev.data)); }
          catch { /* parse error: skip, keep the stream alive */ }
        });
      }

      es.onopen = () => { errors.current = 0; backoff.current = 1000; };
      es.onerror = async () => {
        errors.current += 1;
        if (errors.current < (opts?.probeAfter ?? 3) || probing.current) return;
        // EventSource hides the HTTP status; probe /api/me to classify the failure.
        // Single-flight + capped exponential backoff (each probe opens a DB session).
        probing.current = true;
        try {
          const r = await fetch("/api/me", { credentials: "same-origin" });
          if (r.status === 401) { es?.close(); opts?.onExpired?.(); return; }      // → /login
          if (r.status === 403) { es?.close(); opts?.onNoAccess?.(); return; }     // AccountInactive
          // 200 (or transient): let EventSource keep retrying; just pace future probes.
          backoff.current = Math.min(backoff.current * 2, 30_000);
        } finally {
          setTimeout(() => { probing.current = false; }, backoff.current);
        }
      };
    };

    open();
    return () => { es?.close(); };
  }, [url]);
}
```

```ts
// frontend/src/api/events.test.ts
import { renderHook } from "@testing-library/react";
import { vi, it, expect } from "vitest";
import { useEventStream } from "./events";

it("subscribes per named event and passes a fresh-connect cursor on (re)open", () => {
  const add = vi.fn();
  const instances: FakeES[] = [];
  class FakeES {
    url: string;
    onerror: ((e: unknown) => void) | null = null;
    onopen: (() => void) | null = null;
    constructor(u: string) { this.url = u; instances.push(this); }
    addEventListener = add; removeEventListener = vi.fn(); close = vi.fn();
  }
  vi.stubGlobal("EventSource", FakeES as unknown as typeof EventSource);
  renderHook(() => useEventStream("/events/ping", { ping: () => {} }));
  // Named-event subscription (NOT onmessage):
  expect(add).toHaveBeenCalledWith("ping", expect.any(Function));
  // First open has no cursor; ?since is added only once an id has been seen.
  expect(instances[0].url).not.toContain("since=");
});
```

> The `?since`/cursor, single-flight backoff, and 401/403 outcomes are validated at the contract level here (mocked `EventSource`); nothing shipping in Plan 3 rides SSE at runtime (the dashboard polls). The first real transport consumer is sub-project 2.

- [ ] **Step 6: Run to verify it passes**

Run: `cd frontend && pnpm exec vitest run src/api/events.test.ts`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/web/api/events.py src/call_manager/main.py frontend/src/api/events.ts frontend/src/api/events.test.ts tests/test_events_ping.py
git commit -m "feat(spa): dev-gated /events/ping + reusable useEventStream hook"
```

---

## Task 8: Logout (native form)

**Files:** Modify the app shell to render a logout form

- [ ] **Step 1: Add the form** (full-page nav, follows the 303, carries Origin/CSRF)

In the shell/nav component:

```tsx
<form method="post" action="/logout">
  <button type="submit">Sign out</button>
</form>
```

- [ ] **Step 2: Verify manually (dev)** — clicking Sign out lands on `/login`. (Covered by Playwright in Task 9.)

- [ ] **Step 3: Commit**

```bash
git add frontend/src/  && git commit -m "feat(spa): native-form logout"
```

---

## Task 9: Flip `/` to the SPA + dual-mode Playwright e2e + a11y

**Files:** Modify `frontend/routes.json` (+ regenerate — the dev proxy and prod `is_spa_route` regenerate from it; NO hand-edit of `vite.config.ts`), `frontend/src/router.tsx`, remove the Jinja `/` handler; Create `frontend/e2e/dashboard.spec.ts`, `frontend/playwright.config.ts`

Re-sequence the flip so the SPA serves `/` BEFORE the Jinja handler is removed (don't strand `/`): **(1) flip + regenerate → (2) point React Router at `/` → (3) build + verify → (4) only then delete the Jinja `/` handler.**

- [ ] **Step 1: Flip the manifest + regenerate (NO hand-edited proxy)**

Set `frontend/routes.json` `spa_routes` to `["/"]`; regenerate so the SAME manifest drives the prod `is_spa_route` AND the Vite dev proxy (which is built from `BACKEND_PREFIXES`, per Plan 2 Rev 2/Rev 3 — the proxy is **generated, not hand-edited**):

```bash
uv run python scripts/gen_routes.py
```

This re-emits `src/call_manager/web/spa_routes.py` and `frontend/src/routes.gen.ts`; the Vite dev server keeps consuming `BACKEND_PREFIXES` from `routes.gen.ts` (Plan 2 Task 5 default-to-backend posture) so it now forwards everything to FastAPI except the SPA route `/` and Vite's own dev assets — **do not touch `vite.config.ts` by hand.** Because the manifest match is exact/pattern (not `startswith`), `is_spa_route("/") == True` while `is_spa_route("/calls/1")` / `is_spa_route("/api/x")` / `is_spa_route("/made-up-page") == False`, so unknown non-SPA paths reach FastAPI's real 404 in BOTH dev and prod (never `index.html` 200).

- [ ] **Step 2: Point React Router at `/`**

Change `frontend/src/router.tsx` SPA route `path` from `/app` to `/`. The in-app `*` catch-all stays a SOFT 404 only — it must NOT make the server serve `index.html` for unknown paths; the server (manifest) remains authoritative and returns `index.html` only for `/`.

- [ ] **Step 3: Build + verify the flip BEFORE removing the Jinja handler**

```bash
cd frontend && pnpm build && cd ..
```

Verify (don't proceed until green):
- **dev** (`SPA_MODE=dev`, Vite front door): `GET /` serves the React dashboard; `GET /login` and `GET /calls/1` reach FastAPI; an unknown path (e.g. `/made-up-page`) returns FastAPI's **JSON 404** (not Vite's HTML 404, not `index.html`).
- **prod** (`SPA_MODE=prod`): the manifest-gated catch-all serves `index.html` for `/` but returns a real 404 for `/api/x` and any unknown non-SPA path.
- **regression test (the root-collapse guard — do this here, the flip is where the bug activates):** the flip changes `spa_routes` from `["/app"]` to `["/"]`, so the Plan 2 matcher's root special-case is now load-bearing. **Update** `tests/test_gen_routes.py::test_is_spa_route_is_exact_not_prefix` to assert the FLIPPED manifest (it was written for the `/app` era). Without the root special-case in `is_spa_route`, `"/".rstrip("/")` collapses to `""` and `startswith("/")` matches everything — this test catches that:

```python
# tests/test_gen_routes.py — post-flip body (replaces the /app-era assertions)
def test_is_spa_route_is_exact_not_prefix():
    from call_manager.web.spa_routes import is_spa_route
    assert is_spa_route("/") is True            # the flipped SPA root (dashboard)
    # The root owns ONLY itself — never a subtree. A naive startswith("/") would
    # (wrongly) match all of these; the exact root special-case keeps them
    # backend-owned, so they reach FastAPI's real 404 (never index.html 200).
    assert is_spa_route("/made-up-page") is False
    assert is_spa_route("/api/me") is False
    assert is_spa_route("/calls/123") is False
    assert is_spa_route("/login") is False
```

Run `uv run python scripts/gen_routes.py && uv run pytest tests/test_gen_routes.py -v` — must be green before Step 4.

- [ ] **Step 4: Remove the Jinja dashboard handler (only now that React serves `/`)**

Delete the Jinja dashboard route `calls_index` (`src/call_manager/web/routes/calls.py:60`, `@router.get("/")`), keeping `dashboard.build_dashboard_view` (now used only by `/api/calls`). Keep `/calls/{id}` and `/calls/ranking-status` (the HTMX strip endpoint can be removed once nothing renders it — verify no template references remain).

- [ ] **Step 5: Playwright config (dual-mode)**

```ts
// frontend/playwright.config.ts
import { defineConfig } from "@playwright/test";

const MODE = process.env.E2E_MODE ?? "dev"; // "dev" (Vite proxy) | "prod" (FastAPI build)
export default defineConfig({
  testDir: "./e2e",
  use: { baseURL: MODE === "prod" ? "http://localhost:8000" : "http://localhost:5173" },
});
```

- [ ] **Step 6: e2e spec — REAL login + the Rev 2 serving verifications**

Encode the Rev 2 verifications, not a placeholder: a REAL password login (built from `tests/test_web_login_password.py`: a seeded active user, `POST /login` form fields `email`/`password`, 303→`/`), plus (a) dev unknown-path → FastAPI **JSON 404**, (b) logout via the native `<form method=post action=/logout>` through Vite lands on `/login`, (c) real navigation to an un-ported call-detail page resolves to FastAPI. Keep the dashboard visibility/select + axe a11y checks.

```ts
// frontend/e2e/dashboard.spec.ts
import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

// REAL login via the server-rendered password form (seed an active test user
// per tests/test_web_login_password.py: POST /login {email,password} → 303 /).
async function login(page) {
  await page.goto("/login");
  await page.getByLabel(/email/i).fill("e2e@ki.se");
  await page.getByLabel(/password/i).fill("correct-horse-1");
  await Promise.all([page.waitForURL("**/"), page.getByRole("button", { name: /sign in|log in/i }).click()]);
}

test("dashboard loads, filters, no critical a11y violations", async ({ page }) => {
  await login(page);
  await page.goto("/");
  await expect(page.getByRole("heading")).toBeVisible();
  await page.getByLabel(/profile/i).selectOption({ index: 1 }).catch(() => {});
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter((v) => v.impact === "critical")).toEqual([]);
});

test("dev unknown path resolves to FastAPI's JSON 404 (not index.html / Vite HTML)", async ({ page }) => {
  const res = await page.request.get("/made-up-page");
  expect(res.status()).toBe(404);
  expect(res.headers()["content-type"] ?? "").toContain("application/json");  // FastAPI envelope, not SPA 200
});

test("logout via native form (through Vite) lands on /login", async ({ page }) => {
  await login(page);
  await page.goto("/");
  await Promise.all([page.waitForURL("**/login"), page.getByRole("button", { name: /sign out/i }).click()]);
  await expect(page).toHaveURL(/\/login$/);
});

test("navigation to an un-ported call-detail page resolves to FastAPI (coexistence)", async ({ page }) => {
  await login(page);
  await page.goto("/");
  // Row links are plain anchors to the un-ported Jinja /calls/{id} (server-rendered, not the SPA).
  const link = page.locator('a[href^="/calls/"]').first();
  await link.click().catch(() => {});
  await expect(page).toHaveURL(/\/calls\/\d+/);
});
```

(`?profile` etc. are read-only deep links; logout is the native form from Task 8. The `application/json` 404 assertion is the dev-proxy real-404 check from the foundation spec lines 73/833 — Vite must forward the unknown path to FastAPI, never serve `index.html`.)

- [ ] **Step 7: Run both modes**

Run (dev): start uvicorn `SPA_MODE=dev` (with `DATABASE_URL`/`SESSION_SECRET`/`ALLOWED_EMAIL_DOMAINS` + `APP_BASE_URL=http://localhost:5173`) + `pnpm dev`, then `E2E_MODE=dev pnpm exec playwright test`.
Run (prod): `pnpm build`, start uvicorn `SPA_MODE=prod` on :8000 (same required env), then `E2E_MODE=prod pnpm exec playwright test`.
Expected: PASS in both. (Seed a test user + login helper; reuse the password-login flow from `tests/test_web_login_password.py`.)

- [ ] **Step 8: Backend parity cleanup test**

Run: `uv run pytest tests/test_web_calls.py tests/test_dashboard_readmodel.py tests/test_api_calls.py -v`
Expected: PASS — `/api/calls` + the read-model still green after removing the Jinja `/` handler. Per Rev 2 [HIGH], also delete/replace ONLY the `GET /` Jinja cases in `tests/test_web_calls*.py` (they 404 under `SPA_MODE=disabled` once `calls_index` is gone; read-model parity is covered by `tests/test_dashboard_readmodel.py` + `tests/test_api_calls.py`); keep the `/calls/{id}` + ranking-status tests.

- [ ] **Step 9: Commit**

The proxy is regenerated from `routes.json` (not a hand-edited `vite.config.ts`), so only the manifest + generated artifacts change:

```bash
git add frontend/routes.json src/call_manager/web/spa_routes.py frontend/src/routes.gen.ts frontend/src/router.tsx src/call_manager/web/routes/calls.py tests/test_gen_routes.py frontend/playwright.config.ts frontend/e2e/dashboard.spec.ts
git commit -m "feat(spa): flip / to the React dashboard; dual-mode Playwright + a11y; retire Jinja dashboard"
```

---

## Self-review checklist (completed)

- **Spec coverage:** OpenAPI→TS + CI drift ✓; cookie-auth client + `useSession` 401→login ✓; React Router Data Mode + error boundary ✓; tokens split + base components (Modal/Drawer/Toast deferred) ✓; dashboard parity incl. deep-link-read-only + selection-POST + ranking `{pending,failed}` poll/drain + anchors to un-ported detail ✓; SSE wire contract (named JSON events + id + catch-up via Last-Event-ID + anti-buffering headers + `/api/me` probe) validated at contract level ✓; native-form logout ✓; dual-mode Playwright + a11y ✓; `/`-flip + Jinja retirement ✓.
- **Placeholder scan:** the Playwright login helper is built from the real password-login flow (`tests/test_web_login_password.py`) and the e2e spec asserts the Rev 2 serving checks (dev unknown→JSON 404, logout-through-Vite→`/login`, nav to un-ported `/calls/{id}`); the exact dashboard row fields are referenced to `calls_index.html`. CI workflow paths to be adapted. No "TODO/implement later".
- **Type consistency:** `unwrap`, `api`, `useSession`, `useDashboard`, `useSetDefaultProfile` (TanStack mutation, awaited; invalidates `["me"]`), `useEventStream` names/signatures consistent across tasks; query keys `["me"]`, `["calls", params]` consistent.

## Foundation complete

After Plan 3, the whole app is one React SPA over the reused FastAPI backend, with the dashboard ported to parity, a reusable realtime hook + SSE contract, and a packaged prod build. Sub-project 2 (the workspace cockpit) builds on this foundation.
