# SPA Redesign — Phase 2 (Dashboard) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-skin the Calls dashboard (`/`) onto the Phase-1 IDE shell — filters into a real sidebar region, a breadcrumb + summary-counts header strip, banded match pills, themed deadline coloring — with zero backend/API/route change.

**Architecture:** `AppShell` gains a route-driven sidebar region (a matched route opts in via `handle: { Sidebar }`, rendered through `useMatches`). The Calls route supplies `CallsFilters`; the sidebar filters and the main list stay in sync purely through the shared React Query cache + URL (no props across the shell boundary). The dashboard body is decomposed into focused presentational components.

**Tech Stack:** React 19 + Vite 5 + react-router-dom 7 (data router) + TanStack Query 5 + Mantine v9 (`Select`, `Switch`) + Vitest 2 + Testing Library (+ `@testing-library/user-event`).

**Spec:** `docs/superpowers/specs/2026-06-23-spa-vscode-redesign-phase2-dashboard-design.md` (read it; this plan implements it).

## Global Constraints

- **Package manager pnpm 10.16 (Node ≥ 20).** Run `pnpm install` after any `package.json` change.
- **Gate after every task, all green, from `frontend/`:** `pnpm exec tsc --noEmit` AND `pnpm vitest run` AND `pnpm build`. There is no TS linter — `tsc` is the gate (`strict`, `noUnusedLocals`, `noUnusedParameters`: no unused imports/params); `pnpm build` (`tsc && vite build`) additionally catches bundle/import regressions. (Every task — Tasks 1–6 — runs all three.)
- **No backend, API, or DB changes.** `/api/calls` and `POST /api/me/default-profile` are consumed as-is. No new SPA routes; `router.test.tsx` drift guard stays green.
- **Re-skin, not rewrite.** Preserve behavior + component contracts (spec §6). The exact behaviors to keep: read-only URL filter state; Profile selection persists the default (`POST /api/me/default-profile`) **awaited first**, then reflects via a **functional** `setSearchParams` updater; selects bind to **server-resolved** `effective.*`; `useDashboard` poll-while-pending + drain-refetch; `unwrap` 401→`/login`; titles are plain `/calls/{id}` anchors.
- **Theme tokens only, dark-first.** Use the `--app-*` variables from `frontend/src/theme/theme.css`; no hardcoded hex in component CSS. Key tokens: `--app-link #2dd4bf`, `--app-good #4ec9b0`, `--app-warn #cca700`, `--app-bad #f14c4c`, `--app-muted #969ca6`, `--app-dim #6e7681`, `--app-sidebar #202327`, `--app-border #2c2f35`, `--app-hover #25282e`.
- **MatchBadge bands** (frontend map on `aggregate_score`, rounded): strong ≥ 80 → `--app-good`; good 60–79 → `--app-muted`; < 60 → `--app-dim`; all share one chip surface (`--app-hover`); a null score renders no badge. **Deadline** (keep `deadlineView` logic): closed → `--app-dim`; soon ≤ 14d → `--app-bad`; watch ≤ 30d → `--app-warn`; open → `--app-muted`.
- **Header "top matches" count is `summary.high_score` displayed verbatim** (server: open, profile-scoped, `aggregate_score ≥ 70`) — NOT recomputed from badge bands.
- **Git — every commit uses THIS exact form** (the per-task snippets below show the same form; do not substitute a bare `git commit`). All work is in the worktree `WT=/home/parlar_ai/dev/call-monitor/.worktrees/spa-vscode-redesign-phase2` on branch `feat/spa-vscode-redesign-phase2` (off `master`). Dispatched subagents default git/cwd to the MAIN tree (the WRONG branch), so **before any commit** run `git -C "$WT" branch --show-current` and confirm it prints exactly `feat/spa-vscode-redesign-phase2`. Then:
  ```bash
  git -C "$WT" add <paths>
  git -C "$WT" -c commit.gpgsign=false commit -m "<message>

  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
  ```

---

## File Structure

| File | Responsibility |
|---|---|
| `frontend/src/shell/AppShell.tsx` (modify) | Add the `useMatches`-driven sidebar region between activity bar and main |
| `frontend/src/shell/Sidebar.tsx` (new) + `.module.css` | Thin sidebar chrome wrapper (`<aside aria-label="Sidebar">`) |
| `frontend/src/shell/AppShell.test.tsx` (modify) | Cover sidebar present/absent via route `handle` |
| `frontend/src/components/MatchBadge.tsx` (new) + `.module.css` + `.test.tsx` | Banded score pill |
| `frontend/src/routes/Deadline.tsx` (new) + `.module.css` + `.test.tsx` | `deadlineView` + `<Deadline>` extracted from Dashboard, reskinned |
| `frontend/src/routes/CallRow.tsx` (new) + `.module.css` + `.test.tsx` | One call row: title link + meta + `MatchBadge` |
| `frontend/src/routes/CallsFilters.tsx` (new) + `.module.css` + `.test.tsx` | Sidebar filters (Mantine `Select`×3 + `Switch`); profile-persist; loading/error contract |
| `frontend/src/routes/DashboardHeader.tsx` (new) + `.module.css` + `.test.tsx` | Header strip: breadcrumb + counts + sign-out |
| `frontend/src/api/dashboard.ts` (modify) | Export `CallItem`/`DashboardOut` types + `dashboardParamsFrom` helper |
| `frontend/src/routes/Dashboard.tsx` (modify) | Shrinks to: `DashboardHeader` + ranking lines + `CallRow` list + `Pagination` |
| `frontend/src/routes/Dashboard.module.css` (modify) | Drop `.closed/.soon/.watch/.open` (moved) + `.summary` (moved); add `.list` |
| `frontend/src/router.tsx` (modify) | Calls index route gains `handle: { Sidebar: CallsFilters }` |
| `frontend/src/test-setup.ts` (modify) | Add a `scrollIntoView` no-op (Mantine `Combobox` calls it) |
| `frontend/package.json` (modify) | + `@testing-library/user-event` (dev) |

---

### Task 1: AppShell sidebar region

**Files:**
- Modify: `frontend/src/shell/AppShell.tsx`
- Create: `frontend/src/shell/Sidebar.tsx`, `frontend/src/shell/Sidebar.module.css`
- Modify: `frontend/src/shell/AppShell.test.tsx`

**Interfaces:**
- Produces: `Sidebar` (wrapper `({children}) => JSX`, renders `<aside aria-label="Sidebar">`); `AppShell` now renders a sidebar when the deepest matched route's `handle` is `{ Sidebar: ComponentType }`.
- Consumes: nothing from earlier tasks. The existing `.body` flex row needs no CSS change (the sidebar is a fixed-width flex child between `ActivityBar` and `.main`).

- [ ] **Step 1: Create the Sidebar wrapper + CSS**

`frontend/src/shell/Sidebar.tsx`:
```tsx
import type { ReactNode } from "react";

import styles from "./Sidebar.module.css";

// Thin shell wrapper providing the sidebar chrome (surface, right border,
// scroll) around whatever the active route supplies via its `handle.Sidebar`.
export function Sidebar({ children }: { children: ReactNode }) {
  return (
    <aside className={styles.sidebar} aria-label="Sidebar">
      {children}
    </aside>
  );
}
```

`frontend/src/shell/Sidebar.module.css`:
```css
.sidebar {
  flex: 0 0 240px;
  min-height: 0;
  overflow: auto;
  background: var(--app-sidebar);
  border-right: 1px solid var(--app-border);
}
```

- [ ] **Step 2: Write the failing AppShell tests**

Append to `frontend/src/shell/AppShell.test.tsx` (the file already imports `RouterProvider`, `createMemoryRouter`, `render`, `screen`, `MantineProvider`, `theme`):
```tsx
import type { RouteObject } from "react-router-dom";

function renderShellWithRoutes(route: string, children: RouteObject[]) {
  const router = createMemoryRouter([{ element: <AppShell />, children }], {
    initialEntries: [route],
  });
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <RouterProvider router={router} />
    </MantineProvider>,
  );
}

it("renders the sidebar region when the active route declares handle.Sidebar", () => {
  function TestSidebar() {
    return <p>SIDEBAR CONTENT</p>;
  }
  renderShellWithRoutes("/", [
    { index: true, element: <p>BODY</p>, handle: { Sidebar: TestSidebar } },
  ]);
  expect(screen.getByLabelText("Sidebar")).toBeInTheDocument();
  expect(screen.getByText("SIDEBAR CONTENT")).toBeInTheDocument();
});

it("omits the sidebar region when no matched route declares one", () => {
  renderShellWithRoutes("/", [{ index: true, element: <p>BODY</p> }]);
  expect(screen.queryByLabelText("Sidebar")).toBeNull();
});
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `pnpm vitest run src/shell/AppShell.test.tsx`
Expected: FAIL — `AppShell` does not render a `Sidebar` region yet (the "renders the sidebar region" test fails to find the aria-label).

- [ ] **Step 4: Modify AppShell to render the route-driven sidebar**

Replace `frontend/src/shell/AppShell.tsx` with:
```tsx
import type { ComponentType } from "react";
import { Outlet, useMatches } from "react-router-dom";

import { ActivityBar } from "./ActivityBar";
import styles from "./AppShell.module.css";
import { Sidebar } from "./Sidebar";
import { StatusBar } from "./StatusBar";
import { TitleBar } from "./TitleBar";

type SidebarHandle = { Sidebar?: ComponentType };

// The persistent IDE shell: title / activity / [sidebar] / main / status. A
// route opts into the contextual sidebar by declaring `handle: { Sidebar }` (a
// component). AppShell renders the deepest matched route that declares one;
// routes without one (Cockpit, the 404) render full-width.
export function AppShell() {
  const matches = useMatches();
  const SidebarContent = [...matches]
    .reverse()
    .map((m) => (m.handle as SidebarHandle | undefined)?.Sidebar)
    .find((S): S is ComponentType => Boolean(S));

  return (
    <div className={styles.shell}>
      <TitleBar />
      <div className={styles.body}>
        <ActivityBar />
        {SidebarContent && (
          <Sidebar>
            <SidebarContent />
          </Sidebar>
        )}
        <div className={styles.main}>
          <Outlet />
        </div>
      </div>
      <StatusBar />
    </div>
  );
}
```

- [ ] **Step 5: Run the full suite + tsc**

Run: `pnpm exec tsc --noEmit && pnpm vitest run && pnpm build`
Expected: PASS — the two new AppShell tests pass; the existing "renders the chrome regions" test still passes (its route declares no handle → no sidebar); all other suites green.

- [ ] **Step 6: Commit**

```bash
git -C "$WT" add frontend/src/shell/AppShell.tsx frontend/src/shell/Sidebar.tsx frontend/src/shell/Sidebar.module.css frontend/src/shell/AppShell.test.tsx
git -C "$WT" -c commit.gpgsign=false commit -m "feat(spa): route-driven sidebar region in the AppShell

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: MatchBadge

**Files:**
- Create: `frontend/src/components/MatchBadge.tsx`, `frontend/src/components/MatchBadge.module.css`, `frontend/src/components/MatchBadge.test.tsx`

**Interfaces:**
- Produces: `MatchBadge` (`({ score: number | null }) => JSX | null`). Bands: strong ≥ 80, good 60–79, < 60 low; null → `null`. Renders `<span data-band="strong|good|low">{round}</span>`.
- Consumes: nothing.

- [ ] **Step 1: Write the failing test**

`frontend/src/components/MatchBadge.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { MatchBadge } from "./MatchBadge";

it("rounds and bands at the 80 / 60 boundaries", () => {
  const { rerender } = render(<MatchBadge score={82.4} />);
  expect(screen.getByText("82")).toHaveAttribute("data-band", "strong");
  rerender(<MatchBadge score={80} />);
  expect(screen.getByText("80")).toHaveAttribute("data-band", "strong");
  rerender(<MatchBadge score={79} />);
  expect(screen.getByText("79")).toHaveAttribute("data-band", "good");
  rerender(<MatchBadge score={60} />);
  expect(screen.getByText("60")).toHaveAttribute("data-band", "good");
  rerender(<MatchBadge score={59} />);
  expect(screen.getByText("59")).toHaveAttribute("data-band", "low");
});

it("renders nothing for a null score", () => {
  const { container } = render(<MatchBadge score={null} />);
  expect(container).toBeEmptyDOMElement();
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm vitest run src/components/MatchBadge.test.tsx`
Expected: FAIL — `./MatchBadge` does not exist.

- [ ] **Step 3: Implement MatchBadge + CSS**

`frontend/src/components/MatchBadge.tsx`:
```tsx
import styles from "./MatchBadge.module.css";

// Banded score pill. The band sets the FOREGROUND color on a shared subtle chip
// surface: strong >=80 (good), good 60-79 (muted), <60 (dim). A null score (no
// ranking row for this call/profile) renders nothing.
export function MatchBadge({ score }: { score: number | null }) {
  if (score == null) return null;
  const band = score >= 80 ? "strong" : score >= 60 ? "good" : "low";
  return (
    <span className={`${styles.badge} ${styles[band]}`} data-band={band}>
      {Math.round(score)}
    </span>
  );
}
```

`frontend/src/components/MatchBadge.module.css`:
```css
.badge {
  display: inline-block;
  min-width: 2.2em;
  padding: 0.05rem 0.45rem;
  border-radius: 999px;
  background: var(--app-hover);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  text-align: center;
}
.strong { color: var(--app-good); }
.good { color: var(--app-muted); }
.low { color: var(--app-dim); }
```

- [ ] **Step 4: Run to verify pass + gate**

Run: `pnpm exec tsc --noEmit && pnpm vitest run && pnpm build`
Expected: PASS — both MatchBadge tests green; suite green.

- [ ] **Step 5: Commit**

```bash
git -C "$WT" add frontend/src/components/MatchBadge.tsx frontend/src/components/MatchBadge.module.css frontend/src/components/MatchBadge.test.tsx
git -C "$WT" -c commit.gpgsign=false commit -m "feat(spa): banded MatchBadge score pill

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Deadline (extract + reskin)

**Files:**
- Create: `frontend/src/routes/Deadline.tsx`, `frontend/src/routes/Deadline.module.css`, `frontend/src/routes/Deadline.test.tsx`
- Modify: `frontend/src/routes/Dashboard.tsx` (remove the inline `deadlineView`/`Deadline`, import from the new file)
- Modify: `frontend/src/routes/Dashboard.module.css` (remove the moved `.closed/.soon/.watch/.open`)

**Interfaces:**
- Produces: `deadlineView(iso: string): { state: "closed"|"soon"|"watch"|"open"; date: string }` and `Deadline` (`({ iso: string }) => JSX`, renders `<span data-deadline-state=...>`).
- Consumes: nothing.

- [ ] **Step 1: Write the failing test**

`frontend/src/routes/Deadline.test.tsx`:
```tsx
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { deadlineView } from "./Deadline";

const NOW = new Date("2026-06-23T12:00:00Z").getTime();
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => vi.useRealTimers());

function isoInDays(days: number): string {
  return new Date(NOW + days * 86_400_000).toISOString();
}

it("maps deadline distance to states at the 14d / 30d cutoffs", () => {
  expect(deadlineView(isoInDays(-1)).state).toBe("closed");
  expect(deadlineView(isoInDays(5)).state).toBe("soon");
  expect(deadlineView(isoInDays(14)).state).toBe("soon"); // <= 14
  expect(deadlineView(isoInDays(20)).state).toBe("watch");
  expect(deadlineView(isoInDays(30)).state).toBe("watch"); // <= 30
  expect(deadlineView(isoInDays(45)).state).toBe("open");
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm vitest run src/routes/Deadline.test.tsx`
Expected: FAIL — `./Deadline` does not exist.

- [ ] **Step 3: Create Deadline (logic copied verbatim from Dashboard) + reskinned CSS**

`frontend/src/routes/Deadline.tsx`:
```tsx
import styles from "./Deadline.module.css";

type DeadlineState = "closed" | "soon" | "watch" | "open";

// Parity with the retired Jinja `deadline_label`: a past deadline is "Closed"
// (dim), <=14 days urgent (bad), <=30 days a watch (warn), else plain (muted).
// The instant is shown as its UTC calendar date.
export function deadlineView(iso: string): { state: DeadlineState; date: string } {
  const date = iso.slice(0, 10);
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return { state: "closed", date };
  const days = Math.floor(ms / 86_400_000);
  if (days <= 14) return { state: "soon", date };
  if (days <= 30) return { state: "watch", date };
  return { state: "open", date };
}

export function Deadline({ iso }: { iso: string }) {
  const { state, date } = deadlineView(iso);
  return (
    <span className={styles[state]} data-deadline-state={state}>
      {state === "closed" ? `Closed · ${date}` : date}
    </span>
  );
}
```

`frontend/src/routes/Deadline.module.css`:
```css
.closed { color: var(--app-dim); }
.soon { color: var(--app-bad); font-weight: 600; }
.watch { color: var(--app-warn); }
.open { color: var(--app-muted); }
```

- [ ] **Step 4: Remove the inline copy from Dashboard**

In `frontend/src/routes/Dashboard.tsx`: delete the `type DeadlineState`, the `deadlineView` function, and the `Deadline` component (everything from the first `type DeadlineState =` line through the end of the `Deadline` component, above `export function Dashboard()`). Add the import near the other route imports:
```tsx
import { Deadline } from "./Deadline";
```
Leave Dashboard's `<Deadline iso={c.deadline} />` usage in the list as-is.

In `frontend/src/routes/Dashboard.module.css`: delete the `.closed`, `.soon`, `.watch`, `.open` rules and their leading comment (now in `Deadline.module.css`). Keep `.summary`.

- [ ] **Step 5: Run the suite + tsc**

Run: `pnpm exec tsc --noEmit && pnpm vitest run && pnpm build`
Expected: PASS — `Deadline.test.tsx` green; `Dashboard.test.tsx` still green (Dashboard renders the imported `<Deadline>` identically); no unused-symbol tsc errors.

- [ ] **Step 6: Commit**

```bash
git -C "$WT" add frontend/src/routes/Deadline.tsx frontend/src/routes/Deadline.module.css frontend/src/routes/Deadline.test.tsx frontend/src/routes/Dashboard.tsx frontend/src/routes/Dashboard.module.css
git -C "$WT" -c commit.gpgsign=false commit -m "refactor(spa): extract + reskin Deadline out of Dashboard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: CallRow + the reskinned list

**Files:**
- Modify: `frontend/src/api/dashboard.ts` (export `DashboardOut` + `CallItem` types)
- Create: `frontend/src/routes/CallRow.tsx`, `frontend/src/routes/CallRow.module.css`, `frontend/src/routes/CallRow.test.tsx`
- Modify: `frontend/src/routes/Dashboard.tsx` (swap the inline `<li>` list for `<CallRow>`)
- Modify: `frontend/src/routes/Dashboard.module.css` (add `.list`)

**Interfaces:**
- Consumes: `MatchBadge` (Task 2), `Deadline` (Task 3).
- Produces: `CallItem` (= `components["schemas"]["CallOut"]`) and `DashboardOut` exported from `api/dashboard.ts`; `CallRow` (`({ call: CallItem }) => JSX`, an `<li>`).

- [ ] **Step 1: Export the types**

In `frontend/src/api/dashboard.ts`, add at the top (after the existing imports):
```tsx
import type { components } from "./schema.gen";

export type DashboardOut = components["schemas"]["DashboardOut"];
export type CallItem = components["schemas"]["CallOut"];
```

- [ ] **Step 2: Write the failing CallRow test**

`frontend/src/routes/CallRow.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
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

it("links the title to the Jinja call-detail page, shows source + match badge", () => {
  render(<CallRow call={call()} />);
  expect(screen.getByRole("link", { name: "Grant X" })).toHaveAttribute("href", "/calls/3");
  expect(screen.getByText("Vinnova")).toBeInTheDocument();
  expect(screen.getByText("82")).toHaveAttribute("data-band", "strong");
});

it("omits the match badge when the score is null", () => {
  render(<CallRow call={call({ aggregate_score: null })} />);
  expect(screen.queryByText("82")).toBeNull();
});
```

- [ ] **Step 3: Run to verify it fails**

Run: `pnpm vitest run src/routes/CallRow.test.tsx`
Expected: FAIL — `./CallRow` does not exist.

- [ ] **Step 4: Implement CallRow + CSS**

`frontend/src/routes/CallRow.tsx`:
```tsx
import type { CallItem } from "../api/dashboard";
import { MatchBadge } from "../components/MatchBadge";
import { Deadline } from "./Deadline";
import styles from "./CallRow.module.css";

// One call in the list: title link (to the un-ported Jinja detail page), a muted
// meta line (source · deadline), and the banded match pill. Plain <a>, not a
// router link — coexistence with the un-ported /calls/{id} HTML page.
export function CallRow({ call }: { call: CallItem }) {
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
      <MatchBadge score={call.aggregate_score} />
    </li>
  );
}
```

`frontend/src/routes/CallRow.module.css`:
```css
.row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.5rem 0.25rem;
  border-bottom: 1px solid var(--app-border);
  list-style: none;
}
.body { min-width: 0; }
.title {
  color: var(--app-link);
  text-decoration: none;
  font-weight: 500;
}
.title:hover { text-decoration: underline; }
.meta {
  margin-top: 0.15rem;
  color: var(--app-muted);
  font-size: 0.85rem;
}
```

- [ ] **Step 5: Swap the Dashboard list to use CallRow**

In `frontend/src/routes/Dashboard.tsx`:
- Add imports: `import { CallRow } from "./CallRow";`.
- Remove the now-unused `import { Deadline } from "./Deadline";` (CallRow owns it now).
- Replace the `<ul>…</ul>` block (the `data.items.map((c) => <li>…</li>)` list) with:
```tsx
      <ul className={styles.list}>
        {data.items.map((c) => (
          <CallRow key={c.id} call={c} />
        ))}
      </ul>
```

In `frontend/src/routes/Dashboard.module.css`, add:
```css
.list {
  margin: 0.5rem 0;
  padding: 0;
}
```

- [ ] **Step 6: Update Dashboard.test list expectations (if any) + run**

Run: `pnpm exec tsc --noEmit && pnpm vitest run && pnpm build`
If `Dashboard.test.tsx` asserts on the old `<li>` inner structure, update those assertions to the `CallRow` output (title is a `link` to `/calls/{id}`; score is a `data-band` badge). The filter/profile tests are unaffected.
Expected: PASS — CallRow tests green; Dashboard suite green.

- [ ] **Step 7: Commit**

```bash
git -C "$WT" add frontend/src/api/dashboard.ts frontend/src/routes/CallRow.tsx frontend/src/routes/CallRow.module.css frontend/src/routes/CallRow.test.tsx frontend/src/routes/Dashboard.tsx frontend/src/routes/Dashboard.module.css
git -C "$WT" -c commit.gpgsign=false commit -m "feat(spa): reskin the calls list with CallRow + MatchBadge

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: CallsFilters in the sidebar

**Files:**
- Modify: `frontend/package.json` (+ `@testing-library/user-event` dev dep)
- Modify: `frontend/src/test-setup.ts` (+ `scrollIntoView` no-op)
- Modify: `frontend/src/api/dashboard.ts` (export `dashboardParamsFrom`)
- Create: `frontend/src/routes/CallsFilters.tsx`, `frontend/src/routes/CallsFilters.module.css`, `frontend/src/routes/CallsFilters.test.tsx`
- Modify: `frontend/src/router.tsx` (Calls index route `handle: { Sidebar: CallsFilters }`)
- Modify: `frontend/src/routes/Dashboard.tsx` (remove the filter controls + their handlers; keep sign-out, summary, ranking, list, pagination)
- Modify: `frontend/src/routes/Dashboard.test.tsx` (filters now live in `CallsFilters`)

**Interfaces:**
- Consumes: `Sidebar` mechanism (Task 1), `useDashboard`/`useSetDefaultProfile`/`DashboardParams` (existing), `CallItem`/`DashboardOut` (Task 4).
- Produces: `CallsFilters` (`() => JSX`, reads its own `useSearchParams`+`useDashboard`); `dashboardParamsFrom(sp: URLSearchParams): DashboardParams` exported from `api/dashboard.ts`.

- [ ] **Step 1: Add the test dependency + jsdom mock**

Run (in `frontend/`):
```bash
pnpm add -D @testing-library/user-event@14.6.1
```
This writes the exact version (`.npmrc` `save-exact=true`).

In `frontend/src/test-setup.ts`, add a `scrollIntoView` no-op (Mantine's `Combobox` calls it; jsdom lacks it):
```tsx
if (typeof Element !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}
```

- [ ] **Step 2: Export the shared params helper**

In `frontend/src/api/dashboard.ts`, add (it parses the URL the same way `Dashboard` does today):
```tsx
export function dashboardParamsFrom(sp: URLSearchParams): DashboardParams {
  return {
    source: sp.get("source") ?? undefined,
    sort: (sp.get("sort") as "newest" | "deadline" | null) ?? undefined,
    profile: sp.get("profile") ?? undefined,
    page: sp.get("page") ? Number(sp.get("page")) : 1,
    show_closed: sp.get("show_closed") === "1",
  };
}
```

- [ ] **Step 3: Write the failing CallsFilters tests**

`frontend/src/routes/CallsFilters.test.tsx`:
```tsx
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation } from "react-router-dom";
import { expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { CallsFilters } from "./CallsFilters";

function LocationProbe() {
  const loc = useLocation();
  return <output data-testid="search">{loc.search}</output>;
}

function dashData(over: { profile_id?: number | null; source?: string | null } = {}) {
  return {
    data: {
      items: [],
      summary: { open_count: 0, high_score: 0, soon_deadline: 0 },
      pagination: { page: 1, limit: 50, has_prev: false, has_next: false, total: 0 },
      ranking: { pending: 0, failed: 0 },
      effective: {
        source: over.source ?? null,
        sort: "newest",
        profile_id: over.profile_id ?? null,
        show_closed: false,
      },
      facets: {
        sources: [{ slug: "vinnova", name: "Vinnova" }],
        profiles: [
          { id: 7, name: "Group G" },
          { id: 9, name: "Group H" },
        ],
      },
    },
    error: undefined,
    response: new Response(),
  };
}

it("reflects the server-resolved profile (Jinja parity), not the URL param", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData({ profile_id: 7 }) as never);
  renderWithProviders(<CallsFilters />, { route: "/" });
  const profile = (await screen.findByLabelText("Profile")) as HTMLInputElement;
  await waitFor(() => expect(profile).toHaveValue("Group G"));
});

it("selecting a Source writes ?source= and resets the page", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData() as never);
  const user = userEvent.setup();
  renderWithProviders(
    <>
      <CallsFilters />
      <LocationProbe />
    </>,
    { route: "/?page=3" },
  );
  await user.click(await screen.findByLabelText("Source"));
  await user.click(await screen.findByRole("option", { name: "Vinnova" }));
  await waitFor(() => {
    const s = screen.getByTestId("search").textContent ?? "";
    expect(s).toContain("source=vinnova");
    expect(s).not.toContain("page=3");
  });
});

it("selecting a Profile persists FIRST: the URL updates only after the POST resolves", async () => {
  // Hold the POST unresolved so the test PROVES the URL doesn't change before
  // persistence (a resolved-immediately mock would pass even if order were wrong).
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData() as never);
  let resolvePost: (v: unknown) => void = () => {};
  const POST = vi
    .spyOn(clientMod.api, "POST")
    .mockReturnValue(new Promise((res) => (resolvePost = res)) as never);
  const user = userEvent.setup();
  renderWithProviders(
    <>
      <CallsFilters />
      <LocationProbe />
    </>,
    { route: "/" },
  );
  await user.click(await screen.findByLabelText("Profile"));
  await user.click(await screen.findByRole("option", { name: "Group H" }));
  await waitFor(() =>
    expect(POST).toHaveBeenCalledWith("/api/me/default-profile", { body: { profile_id: 9 } }),
  );
  // URL must NOT reflect the selection until the POST resolves (persist-first).
  expect(screen.getByTestId("search").textContent).not.toContain("profile=9");
  resolvePost({ data: { ok: true }, error: undefined, response: new Response() });
  await waitFor(() => expect(screen.getByTestId("search").textContent).toContain("profile=9"));
});

it("a failed persist leaves the URL unchanged and surfaces an error", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData() as never);
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: undefined,
    error: { error: { code: "x", message: "no" } },
    response: new Response(),
  } as never);
  const user = userEvent.setup();
  renderWithProviders(
    <>
      <CallsFilters />
      <LocationProbe />
    </>,
    { route: "/" },
  );
  await user.click(await screen.findByLabelText("Profile"));
  await user.click(await screen.findByRole("option", { name: "Group H" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/default profile/i));
  expect(screen.getByTestId("search").textContent).not.toContain("profile=9");
});

it("renders without crashing while the query is loading (facet selects disabled)", async () => {
  // GET never resolves -> data stays undefined (the first-load case).
  vi.spyOn(clientMod.api, "GET").mockReturnValue(new Promise(() => {}) as never);
  renderWithProviders(<CallsFilters />, { route: "/" });
  expect((await screen.findByLabelText("Profile")) as HTMLInputElement).toBeDisabled();
  expect((screen.getByLabelText("Source")) as HTMLInputElement).toBeDisabled();
  // Sort + Show-closed bind to the URL and stay usable.
  expect(screen.getByLabelText("Sort")).not.toBeDisabled();
  expect(screen.getByLabelText("Show closed")).toBeInTheDocument();
});

it("renders without crashing when /api/calls errors before any data exists", async () => {
  // No-prior-data error path (the other half of the loading/error contract).
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: undefined,
    error: { error: { code: "x", message: "boom" } },
    response: new Response(null, { status: 500 }),
  } as never);
  renderWithProviders(<CallsFilters />, { route: "/" });
  expect((await screen.findByLabelText("Profile")) as HTMLInputElement).toBeDisabled();
  expect(screen.getByLabelText("Sort")).not.toBeDisabled();
  expect(screen.getByLabelText("Show closed")).toBeInTheDocument();
});
```

- [ ] **Step 4: Run to verify they fail**

Run: `pnpm vitest run src/routes/CallsFilters.test.tsx`
Expected: FAIL — `./CallsFilters` does not exist.

- [ ] **Step 5: Implement CallsFilters + CSS**

`frontend/src/routes/CallsFilters.tsx`:
```tsx
import { Select, Switch } from "@mantine/core";
import { useSearchParams } from "react-router-dom";

import { dashboardParamsFrom, useDashboard, useSetDefaultProfile } from "../api/dashboard";
import styles from "./CallsFilters.module.css";

// Sidebar filters (declared on the Calls route via `handle.Sidebar`). Reads the
// SAME useDashboard query + URL as the Dashboard list — React Query dedupes by
// key, the URL holds filter state, so the two stay in sync with no props across
// the shell boundary. Renders independently, so it must tolerate `data ===
// undefined` (first load / error): facet-driven selects disable until data
// arrives; Sort + Show-closed bind to the URL and always work.
export function CallsFilters() {
  const [sp, setSp] = useSearchParams();
  const params = dashboardParamsFrom(sp);
  const { data } = useDashboard(params);
  const setDefault = useSetDefaultProfile();
  const loading = data === undefined;

  function updateParam(key: string, value: string) {
    setSp((prev) => {
      const next = new URLSearchParams(prev);
      if (value) next.set(key, value);
      else next.delete(key);
      next.delete("page"); // any filter change resets to page 1
      return next;
    });
  }

  async function onSelectProfile(value: string) {
    // Explicit selection: PERSIST FIRST (awaited), reflect the view only on success.
    try {
      await setDefault.mutateAsync(value ? Number(value) : null);
    } catch {
      return; // persist failed -> leave the URL unchanged (error surfaced below)
    }
    // Functional updater: merge against the LATEST params, not the `sp` captured
    // before the awaited mutation.
    setSp((prev) => {
      const next = new URLSearchParams(prev);
      if (value) next.set("profile", value);
      else next.delete("profile");
      return next;
    });
  }

  const profileOptions = [
    { value: "", label: "— No profile —" },
    ...(data?.facets.profiles ?? []).map((p) => ({ value: String(p.id), label: p.name })),
  ];
  const sourceOptions = [
    { value: "", label: "All sources" },
    ...(data?.facets.sources ?? []).map((s) => ({ value: s.slug, label: s.name })),
  ];

  return (
    <div className={styles.filters}>
      <h2 className={styles.heading}>Filters</h2>

      <Select
        label="Profile"
        data={profileOptions}
        value={data?.effective.profile_id != null ? String(data.effective.profile_id) : ""}
        onChange={(v) => onSelectProfile(v ?? "")}
        disabled={loading || setDefault.isPending}
        allowDeselect={false}
        comboboxProps={{ withinPortal: false }}
      />
      {setDefault.isError && <p role="alert">Couldn&apos;t save the default profile.</p>}

      <Select
        label="Source"
        data={sourceOptions}
        value={data?.effective.source ?? ""}
        onChange={(v) => updateParam("source", v ?? "")}
        disabled={loading}
        allowDeselect={false}
        comboboxProps={{ withinPortal: false }}
      />

      <Select
        label="Sort"
        data={[
          { value: "newest", label: "Newest" },
          { value: "deadline", label: "Deadline" },
        ]}
        value={params.sort ?? "newest"}
        onChange={(v) => updateParam("sort", v ?? "newest")}
        allowDeselect={false}
        comboboxProps={{ withinPortal: false }}
      />

      <Switch
        label="Show closed"
        checked={params.show_closed ?? false}
        onChange={(e) => updateParam("show_closed", e.currentTarget.checked ? "1" : "")}
      />
    </div>
  );
}
```

`frontend/src/routes/CallsFilters.module.css`:
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

- [ ] **Step 6: Run CallsFilters tests to verify they pass**

Run: `pnpm vitest run src/routes/CallsFilters.test.tsx`
Expected: PASS. (If a Mantine option query needs adjusting, keep the assertion intent — open the `Select`, click the `option` — and adjust the query; `comboboxProps={{ withinPortal: false }}` keeps options in the same container.)

- [ ] **Step 7: Wire the sidebar handle + strip filters out of Dashboard**

In `frontend/src/router.tsx`:
- Add `import { CallsFilters } from "./routes/CallsFilters";`.
- Change the index route to:
```tsx
      { index: true, element: <Dashboard />, handle: { Sidebar: CallsFilters } },
```

In `frontend/src/routes/Dashboard.tsx`, remove what now lives in `CallsFilters`:
- Delete `updateParam`, `onSelectProfile`, and the `const setDefault = useSetDefaultProfile();` line.
- Change the `../api/dashboard` import to exactly `import { dashboardParamsFrom, useDashboard } from "../api/dashboard";` (drop both `useSetDefaultProfile` and `DashboardParams` — now unused, which `noUnusedLocals` would otherwise reject). Replace the `const params: DashboardParams = {…}` block with `const params = dashboardParamsFrom(sp);`.
- In the JSX `<header>`, delete the Profile/Source/Sort `<label><select>…` blocks and the Show-closed `<label><input type="checkbox">…`, plus the `{setDefault.isError && …}` line. **Keep** the Sign-out `<form method="post" action="/logout">` block.
- `sp` is still needed (for `dashboardParamsFrom(sp)` and the `Pagination` page setter); `setSp` is still used by `Pagination`'s `onPage`. Leave those.

- [ ] **Step 8: Update Dashboard.test.tsx (filters moved out)**

The filter/profile-select tests now belong to `CallsFilters.test.tsx` (Step 3 covers them). In `frontend/src/routes/Dashboard.test.tsx`, delete the tests that assert on the Profile/Source/Sort selects + Show-closed (they query controls Dashboard no longer renders), and update the `dashData` mock's `effective` to the full shape `{ source, sort, profile_id, show_closed }`. Keep tests that assert on the list, summary, ranking, pagination, and the 401→login redirect.

- [ ] **Step 9: Run the suite + tsc + build**

Run: `pnpm exec tsc --noEmit && pnpm vitest run && pnpm build`
Expected: PASS — CallsFilters green; Dashboard green (no unused `useSetDefaultProfile`/`DashboardParams` import); AppShell shows the sidebar on `/`; `pnpm build` succeeds.

- [ ] **Step 10: Commit**

```bash
git -C "$WT" add frontend/package.json frontend/pnpm-lock.yaml frontend/src/test-setup.ts frontend/src/api/dashboard.ts frontend/src/routes/CallsFilters.tsx frontend/src/routes/CallsFilters.module.css frontend/src/routes/CallsFilters.test.tsx frontend/src/router.tsx frontend/src/routes/Dashboard.tsx frontend/src/routes/Dashboard.test.tsx
git -C "$WT" -c commit.gpgsign=false commit -m "feat(spa): move calls filters into the sidebar (CallsFilters)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: DashboardHeader (breadcrumb + counts + sign-out)

**Files:**
- Create: `frontend/src/routes/DashboardHeader.tsx`, `frontend/src/routes/DashboardHeader.module.css`, `frontend/src/routes/DashboardHeader.test.tsx`
- Modify: `frontend/src/routes/Dashboard.tsx` (replace the `<h1>` + `<header>` + summary `<section>` with `<DashboardHeader>`)
- Modify: `frontend/src/routes/Dashboard.module.css` (remove `.summary`)

**Interfaces:**
- Consumes: `useDashboard`/`dashboardParamsFrom` (existing), the shared cache.
- Produces: `DashboardHeader` (`() => JSX`, reads its own `useSearchParams`+`useDashboard`; renders the breadcrumb, the 3 counts from `summary` verbatim, and the sign-out form).

- [ ] **Step 1: Write the failing test**

`frontend/src/routes/DashboardHeader.test.tsx`:
```tsx
import { screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { DashboardHeader } from "./DashboardHeader";

function dashData() {
  return {
    data: {
      items: [],
      summary: { open_count: 12, high_score: 5, soon_deadline: 3 },
      pagination: { page: 1, limit: 50, has_prev: false, has_next: false, total: 0 },
      ranking: { pending: 0, failed: 0 },
      effective: { source: null, sort: "newest", profile_id: null, show_closed: false },
      facets: { sources: [], profiles: [] },
    },
    error: undefined,
    response: new Response(),
  };
}

it("shows the breadcrumb, the summary counts verbatim, and a logout form", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(dashData() as never);
  renderWithProviders(<DashboardHeader />, { route: "/" });

  expect(screen.getByText("Calls")).toBeInTheDocument();
  await waitFor(() => expect(screen.getByText("12")).toBeInTheDocument()); // open_count
  expect(screen.getByText("5")).toBeInTheDocument(); // high_score (verbatim)
  expect(screen.getByText("3")).toBeInTheDocument(); // soon_deadline

  const signout = screen.getByRole("button", { name: /sign out/i });
  expect(signout.closest("form")).toHaveAttribute("action", "/logout");
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm vitest run src/routes/DashboardHeader.test.tsx`
Expected: FAIL — `./DashboardHeader` does not exist.

- [ ] **Step 3: Implement DashboardHeader + CSS**

`frontend/src/routes/DashboardHeader.tsx`:
```tsx
import { useSearchParams } from "react-router-dom";

import { dashboardParamsFrom, useDashboard } from "../api/dashboard";
import styles from "./DashboardHeader.module.css";

// The dashboard header strip: breadcrumb · the 3 summary counts (shown verbatim
// from the read-model — `high_score` is the server's >=70 top-match count, not
// recomputed from MatchBadge bands) · sign-out. Reads the shared useDashboard
// query (deduped with the list + filters).
export function DashboardHeader() {
  const [sp] = useSearchParams();
  const { data } = useDashboard(dashboardParamsFrom(sp));
  const s = data?.summary;

  return (
    <header className={styles.strip}>
      <span className={styles.crumb}>Calls</span>
      <div className={styles.counts}>
        <span>
          <strong>{s?.open_count ?? 0}</strong> open
        </span>
        <span>
          <strong>{s?.high_score ?? 0}</strong> top matches
        </span>
        <span>
          <strong>{s?.soon_deadline ?? 0}</strong> closing within 30 days
        </span>
      </div>
      {/* Native POST logout: a full-page nav follows the 303 to /login and
          carries Origin/CSRF (no client fetch / token plumbing). */}
      <form className={styles.signout} method="post" action="/logout">
        <button type="submit">Sign out</button>
      </form>
    </header>
  );
}
```

`frontend/src/routes/DashboardHeader.module.css`:
```css
.strip {
  display: flex;
  align-items: center;
  gap: 1rem;
  padding: 0.5rem 0.25rem;
  border-bottom: 1px solid var(--app-border);
}
.crumb {
  font-weight: 600;
  color: var(--app-text);
}
.counts {
  display: flex;
  flex-wrap: wrap;
  gap: 1rem;
  color: var(--app-muted);
  font-size: 0.85rem;
}
.signout {
  margin-left: auto;
}
```

- [ ] **Step 4: Use it in Dashboard; drop the old header/summary**

In `frontend/src/routes/Dashboard.tsx`:
- Add `import { DashboardHeader } from "./DashboardHeader";`.
- Replace the `<h1>Funding calls</h1>`, the remaining `<header>` (the sign-out form left after Task 5), and the summary `<section aria-label="Summary">…</section>` with a single `<DashboardHeader />` at the top of the returned `<main>`.
- Remove the now-unused `styles.summary` reference. **Keep** the ranking-status lines, the `<ul className={styles.list}>` `CallRow` list, and `<Pagination>`.

In `frontend/src/routes/Dashboard.module.css`, delete the `.summary` rule (and its comment).

- [ ] **Step 5: Update Dashboard.test.tsx (counts/sign-out moved out) + run**

In `frontend/src/routes/Dashboard.test.tsx`, move/delete any assertions on the summary counts or the sign-out button (now covered by `DashboardHeader.test.tsx`). Keep list/ranking/pagination/401 tests.

Run: `pnpm exec tsc --noEmit && pnpm vitest run && pnpm build`
Expected: PASS — DashboardHeader green; Dashboard green; `pnpm build` succeeds.

- [ ] **Step 6: Commit**

```bash
git -C "$WT" add frontend/src/routes/DashboardHeader.tsx frontend/src/routes/DashboardHeader.module.css frontend/src/routes/DashboardHeader.test.tsx frontend/src/routes/Dashboard.tsx frontend/src/routes/Dashboard.module.css
git -C "$WT" -c commit.gpgsign=false commit -m "feat(spa): dashboard header strip (breadcrumb + counts + sign-out)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final validation (after Task 6)

- [ ] `pnpm exec tsc --noEmit` clean; `pnpm vitest run` all green; `pnpm build` succeeds.
- [ ] Dev-mode visual check (now that `pnpm dev` works): `CHOKIDAR_USEPOLLING=true pnpm dev`, load `http://localhost:5173/`, confirm the reskinned dashboard — sidebar filters, header strip with counts, banded match pills, colored deadlines — renders inside the shell. (Backendless render shows the shell + filters; the list shows the load-error state, which is expected without a backend.)
