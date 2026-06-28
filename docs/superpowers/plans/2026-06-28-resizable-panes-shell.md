# Resizable Panes — Slice 1 (Shell Panes) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the cockpit IDE shell's left `Sidebar` and right `CockpitChat` draggable-to-resize AND collapsible (drag-collapse + a reopen control), with widths/collapse persisted across reloads, via `react-resizable-panels`.

**Architecture:** Wrap the `AppShell` body's resizable regions (everything right of the fixed 48px `ActivityBar`) in a horizontal `<PanelGroup autoSaveId="cm-shell">`. The left `Sidebar`, the `main` Outlet, and the right `RightPane` each become a `<Panel>`, separated by `<PanelResizeHandle>`s. The side panels are `collapsible`; reopen is wired through the existing chrome — the active `ActivityBar` item toggles the sidebar, a new `TitleBar` button toggles the chat. Persistence is automatic (`autoSaveId` → localStorage, keyed by the set of panel ids, so cockpit `{sidebar,main,rightpane}` and the list routes `{sidebar,main}` persist independently).

**Tech Stack:** React 19 + Mantine v9 + react-router 7 + Vite + Vitest. Spec: `docs/superpowers/specs/2026-06-28-resizable-panes-design.md`.

## Global Constraints

Every task's requirements implicitly include this section.

- **Library version is pinned EXACTLY to `react-resizable-panels@2.1.9`.** Install with `pnpm add -E react-resizable-panels@2.1.9` (the codebase pins exact versions — no `^`/`~`; `package.json` must read `"react-resizable-panels": "2.1.9"`). ⚠️ Do NOT install `latest` or `^2` — the current major `4.x` is a *different API* (`Group`/`Separator`/`useDefaultLayout`, no `autoSaveId`/`onCollapse`) and none of this plan's code compiles against it. The 2.x API used here: `PanelGroup` (props `direction`, `autoSaveId`, `className`), `Panel` (props `id`, `order`, `minSize`, `defaultSize`, `collapsible`, `collapsedSize`, `ref` → `ImperativePanelHandle`, `onCollapse`, `onExpand`), `PanelResizeHandle` (prop `className`; renders `role="separator"` + `data-resize-handle-state="inactive|hover|drag"`). `ImperativePanelHandle` = `{ collapse(), expand(), isCollapsed(), isExpanded(), getSize(), resize(), getId() }`.
- **Sizes are PERCENTAGES of the group (0–100), not pixels.** The old fixed widths become approximate %: sidebar `defaultSize={18}` (`minSize={12}`), main `minSize={30}` (auto default), right chat `defaultSize={22}` (`minSize={15}`). Collapsibles use `collapsedSize={0}`.
- **Conditional panels MUST carry both a stable `id` AND `order`** (`sidebar`=1, `main`=2, `rightpane`=3) — the library needs them to persist/restore layouts when panels mount/unmount across routes.
- **Preserve the per-cockpit remount keys on the INNER content, not the Panels.** The `key={`main:${idSuffix}`}` (on the `.main` div) and `key={`pane:${idSuffix}`}` (on `RightPaneContent`) stay exactly as today, so a workspace switch still resets local state without thrashing the PanelGroup. Do NOT put a changing React `key` on a `<Panel>` (it would remount the panel and drop its size).
- **Preserve all landmarks/ARIA** — tests assert them: `<nav aria-label="Primary">` (ActivityBar), `<aside aria-label="Sidebar">` (Sidebar), the chat's `<aside aria-label="Chat">`, `<footer aria-label="Status bar">` (StatusBar). New resize handles get `aria-label="Resize sidebar"` / `aria-label="Resize chat"`.
- **No API/schema/copy change.** No backend touched, no OpenAPI regen. Pure frontend.
- **Type gate is `tsc`** — there is NO TypeScript ESLint. `tsconfig` has `noUnusedLocals: true` (a destructured-but-unused prop or unused `useState` tuple member is a COMPILE ERROR — only introduce state/props you actually consume). Per task: `pnpm exec tsc --noEmit` + the task's named test file(s). All commands run from `frontend/`.
- **jsdom has no layout.** `react-resizable-panels` can't measure real sizes under jsdom; it renders panels at `defaultSize` and may emit a benign console warning. `ResizeObserver` is already stubbed in `src/test-setup.ts`. **Tests assert structure / landmarks / ARIA / roles / callback wiring — NEVER pixel widths.** (Verified empirically against v2.1.9 + this repo's vitest/jsdom config: the `PanelGroup`/`Panel`/`PanelResizeHandle` tree mounts cleanly, separators are queryable by `getByRole("separator", { name })`, and the per-id remount keys on inner content still work.)
- **⚠️ The imperative collapse API throws under jsdom — DO NOT unit-test the real collapse path.** Calling `ImperativePanelHandle.isCollapsed()` / `collapse()` / `expand()` under jsdom raises `Error: Panel size not found for panel "…"` (the library never measured a layout; the failure is order/persistence-dependent — it may pass once and throw the next time). The collapse *helpers live in `AppShell`*; every unit test in this plan exercises them only through **mocked** callbacks (`onToggleSidebar` / `onToggleChat` = `vi.fn()`), never the real handle. **Do NOT add a test that renders the real `AppShell` and clicks a toggle through to a real `collapse()`/`isCollapsed()`** — it will crash the suite. Real collapse/expand/persist behaviour is covered by the manual verification step on `:8000`.

---

## File Structure

- `frontend/package.json` + lockfile — add `react-resizable-panels@2.1.9` (Task 1).
- `frontend/src/shell/AppShell.tsx` — wrap the body in a `PanelGroup`; Task 1 adds resize, Task 2 adds collapse refs/toggles.
- `frontend/src/shell/AppShell.module.css` — `.panes` (flex filler) + `.handle` (resize rail) added; `.main` loses `flex:1` (Task 1).
- `frontend/src/shell/Sidebar.module.css` — drop `flex:0 0 240px` → `height:100%` (Task 1).
- `frontend/src/cockpit/chat/CockpitChat.module.css` — drop `width:320px` → `height:100%` (Task 1).
- `frontend/src/shell/ActivityBar.tsx` — active item click toggles the sidebar (Task 2).
- `frontend/src/shell/TitleBar.tsx` + `TitleBar.module.css` — a chat-toggle button on cockpit routes (Task 2).
- Tests: `AppShell.test.tsx` (Task 1, +handle assertions), `ActivityBar.test.tsx` (Task 2), `TitleBar.test.tsx` (Task 2).

---

### Task 1: Resizable shell `PanelGroup` (drag-to-resize + persistence)

Deliverable: the sidebar/chat edges are draggable and widths persist across reload. Panels are NOT yet collapsible (that's Task 2) — this leaves a fully usable, resizable shell.

**Files:**
- Modify: `frontend/package.json` (+ `frontend/pnpm-lock.yaml`) — add the dependency.
- Modify: `frontend/src/shell/AppShell.tsx`
- Modify: `frontend/src/shell/AppShell.module.css`
- Modify: `frontend/src/shell/Sidebar.module.css`
- Modify: `frontend/src/cockpit/chat/CockpitChat.module.css`
- Test: `frontend/src/shell/AppShell.test.tsx`

**Interfaces:**
- Produces (for Task 2): the `<PanelGroup>` + `<Panel id order>` structure in `AppShell.tsx`; the `.handle` CSS class. Task 2 adds `collapsible`/`ref`/`onCollapse` to the side panels and wires toggles.

- [ ] **Step 1: Install the dependency (exact version)**

Run (from `frontend/`):
```bash
pnpm add -E react-resizable-panels@2.1.9
```
Verify `frontend/package.json` `dependencies` now contains `"react-resizable-panels": "2.1.9"` (exact, no `^`). If pnpm wrote a range, hand-edit it to the exact string and re-run `pnpm install`.

- [ ] **Step 2: Add `.panes` + `.handle` and adjust `.main` in `AppShell.module.css`**

Replace the whole file with:
```css
.shell {
  display: grid;
  grid-template-rows: 30px 1fr 22px;
  height: 100vh;
  background: var(--app-editor);
  color: var(--app-text);
  overflow: hidden;
}
.body {
  display: flex;
  min-height: 0;
}
/* The PanelGroup is the flex item that fills the row beside the fixed ActivityBar.
   (Width of each region now lives on its <Panel>, not here.) */
.panes {
  flex: 1;
  min-width: 0;
  min-height: 0;
}
.main {
  min-width: 0;
  height: 100%;
  overflow: auto;
}
/* PanelResizeHandle renders no visible UI unless styled. A thin rail that lights
   up on hover/drag (the library sets data-resize-handle-state) + a keyboard focus
   ring (the handle is focusable + arrow-resizable). */
.handle {
  flex: 0 0 6px;
  position: relative;
  background: transparent;
  cursor: col-resize;
  transition: background 120ms ease;
}
.handle:hover,
.handle[data-resize-handle-state="hover"],
.handle[data-resize-handle-state="drag"] {
  background: var(--app-accent);
}
.handle:focus-visible {
  outline: 2px solid var(--app-accent);
  outline-offset: -2px;
}
```

- [ ] **Step 3: Drop the fixed width in `Sidebar.module.css`**

Replace the whole file with:
```css
.sidebar {
  height: 100%;
  min-height: 0;
  overflow: auto;
  background: var(--app-sidebar);
  border-right: 1px solid var(--app-border);
}
```

- [ ] **Step 4: Drop the fixed width in `CockpitChat.module.css`**

Edit ONLY the `.chat` rule (leave every other rule in the file untouched):
```css
.chat {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--app-sidebar);
  border-left: 1px solid var(--app-border);
  min-height: 0;
}
```
(The change: `width: 320px;` → `height: 100%;`.)

- [ ] **Step 5: Wrap the `AppShell` body in a `PanelGroup`**

Replace `frontend/src/shell/AppShell.tsx` with:
```tsx
import type { ComponentType } from "react";
import { Outlet, useLocation, useMatches } from "react-router-dom";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";

import { cockpitIdFromPath } from "../cockpit/cockpitNav";
import { ActivityBar } from "./ActivityBar";
import styles from "./AppShell.module.css";
import { Sidebar } from "./Sidebar";
import { StatusBar } from "./StatusBar";
import { TitleBar } from "./TitleBar";

type SidebarHandle = { Sidebar?: ComponentType };
type RightPaneHandle = { RightPane?: ComponentType };

// The persistent IDE shell: title / activity / [sidebar] / main / [right-pane] / status. A
// route opts into the contextual sidebar by declaring `handle: { Sidebar }` (a
// component). AppShell renders the deepest matched route that declares one (the
// Calls list -> CallsFilters, the Cockpit -> CockpitSidebar); routes without one
// (the 404) render full-width. A route may also declare `handle: { RightPane }`
// to mount a right-pane region (e.g. CockpitChat); the component renders its own
// landmark element.
//
// The sidebar/main/right-pane regions live inside a horizontal react-resizable
// PanelGroup so the side edges are draggable; widths persist via autoSaveId
// (localStorage, keyed by the set of panel ids → cockpit {sidebar,main,rightpane}
// and the list routes {sidebar,main} persist independently). The ActivityBar stays
// OUTSIDE the group (fixed 48px, like VS Code — never resizable).
export function AppShell() {
  const matches = useMatches();
  const { pathname } = useLocation();
  const SidebarContent = [...matches]
    .reverse()
    .map((m) => (m.handle as SidebarHandle | undefined)?.Sidebar)
    .find((S): S is ComponentType => Boolean(S));
  const RightPaneContent = [...matches]
    .reverse()
    .map((m) => (m.handle as RightPaneHandle | undefined)?.RightPane)
    .find((R): R is ComponentType => Boolean(R));

  // Per-workspace remount key for the WHOLE cockpit subtree (main Outlet + right pane).
  // A param-only nav /cockpit/A → /cockpit/B keeps this layout mounted and would
  // otherwise reconcile the same Cockpit/CockpitChat instances, leaking A's local
  // state into B (stale jobId/streamed turns → SSE targeting the wrong workspace;
  // stale bottomTab/collapsed/openPeek). Keying by the cockpit id forces a fresh
  // mount on every workspace change, resetting all of it. Non-cockpit routes resolve
  // to "" → a stable key so they reconcile normally.
  // The main and right-pane regions are SIBLINGS, so they need DISTINCT keys (a shared
  // key collides → React warns + reuses one). Both keys carry the same cockpit id, so
  // both regions remount together on a workspace change but reconcile on a query/section
  // change. Non-cockpit routes resolve to "" → stable per-region keys. The keys live on
  // the INNER content, NOT the <Panel>s, so a workspace switch never remounts the panels
  // (which would drop their persisted sizes).
  const cockpitId = cockpitIdFromPath(pathname);
  const idSuffix = cockpitId ? `cockpit:${cockpitId}` : "shell";

  return (
    <div className={styles.shell}>
      <TitleBar />
      <div className={styles.body}>
        <ActivityBar />
        <PanelGroup direction="horizontal" autoSaveId="cm-shell" className={styles.panes}>
          {SidebarContent && (
            <>
              <Panel id="sidebar" order={1} minSize={12} defaultSize={18}>
                <Sidebar>
                  <SidebarContent />
                </Sidebar>
              </Panel>
              <PanelResizeHandle className={styles.handle} aria-label="Resize sidebar" />
            </>
          )}
          <Panel id="main" order={2} minSize={30}>
            <div className={styles.main} key={`main:${idSuffix}`}>
              <Outlet />
            </div>
          </Panel>
          {RightPaneContent && (
            <>
              <PanelResizeHandle className={styles.handle} aria-label="Resize chat" />
              <Panel id="rightpane" order={3} minSize={15} defaultSize={22}>
                <RightPaneContent key={`pane:${idSuffix}`} />
              </Panel>
            </>
          )}
        </PanelGroup>
      </div>
      <StatusBar />
    </div>
  );
}
```

- [ ] **Step 6: Add resize-handle structural tests to `AppShell.test.tsx`**

The existing 5 tests assert by text/label/role and survive the new wrapper divs unchanged — do NOT weaken or delete them; confirm they still pass. ADD these three tests (append after the existing tests, before the file ends; they reuse the `renderShellWithRoutes` / `renderShellAt` helpers already in the file):

```tsx
it("renders a resize handle beside the sidebar when a sidebar is present", () => {
  function TestSidebar() {
    return <p>SIDEBAR CONTENT</p>;
  }
  renderShellWithRoutes("/", [
    { index: true, element: <p>BODY</p>, handle: { Sidebar: TestSidebar } },
  ]);
  expect(screen.getByRole("separator", { name: "Resize sidebar" })).toBeInTheDocument();
});

it("renders a resize handle beside the right pane when one is present", () => {
  renderShellWithRoutes("/", [
    {
      index: true,
      element: <p>BODY</p>,
      handle: { RightPane: () => <aside aria-label="Chat" /> },
    },
  ]);
  expect(screen.getByRole("separator", { name: "Resize chat" })).toBeInTheDocument();
});

it("renders no resize handle when neither a sidebar nor a right pane is present", () => {
  renderShellAt("/");
  expect(screen.queryByRole("separator")).toBeNull();
});
```

- [ ] **Step 7: Run the type gate + the test file**

Run (from `frontend/`):
```bash
pnpm exec tsc --noEmit
pnpm test -- --run src/shell/AppShell.test.tsx
```
Expected: tsc clean; all AppShell tests pass (the original 5 + the 3 new ones = 8). A benign console warning from react-resizable-panels about zero-size measurement under jsdom is acceptable; a *failing* test is not — if an original assertion breaks, the regions/landmarks must still be reachable in the new tree (adjust the query to the new DOM without weakening the assertion), do not delete coverage.

- [ ] **Step 8: Commit**

```bash
git add frontend/package.json frontend/pnpm-lock.yaml frontend/src/shell/AppShell.tsx frontend/src/shell/AppShell.module.css frontend/src/shell/Sidebar.module.css frontend/src/cockpit/chat/CockpitChat.module.css frontend/src/shell/AppShell.test.tsx
git commit -m "feat(spa): resizable shell panes (sidebar/main/chat) via react-resizable-panels

Wrap the AppShell body in a horizontal PanelGroup so the left sidebar and
right chat edges are draggable; widths persist via autoSaveId. Drop the fixed
flex/width on Sidebar/CockpitChat (the Panel owns width now). Not yet
collapsible — that is the next task.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Collapse + reopen (drag-collapse, ActivityBar reopens sidebar, TitleBar toggles chat)

Deliverable: both side panels collapse (drag past `minSize` → hidden) and reopen via the existing chrome — the active ActivityBar item toggles the sidebar; a TitleBar button toggles the chat. Collapse state persists via the same `autoSaveId`.

**Files:**
- Modify: `frontend/src/shell/AppShell.tsx` (refs + `collapsible` + toggles, thread callbacks)
- Modify: `frontend/src/shell/ActivityBar.tsx` (active item toggles sidebar)
- Modify: `frontend/src/shell/TitleBar.tsx` (chat-toggle button)
- Modify: `frontend/src/shell/TitleBar.module.css` (`.paneToggle`)
- Test: `frontend/src/shell/ActivityBar.test.tsx`, `frontend/src/shell/TitleBar.test.tsx`

**Interfaces:**
- Consumes (from Task 1): the `<PanelGroup>`/`<Panel id order>` structure + `.handle` class in `AppShell.tsx`.
- `ActivityBar` gains an optional prop: `onToggleSidebar?: () => void` (provided by AppShell only when a sidebar is present; the active nav item calls it instead of navigating).
- `TitleBar` gains optional props: `hasRightPane?: boolean`, `chatCollapsed?: boolean`, `onToggleChat?: () => void`. The toggle button renders ONLY when `hasRightPane` (so no dead control off the cockpit). Both components' new props are OPTIONAL with safe defaults, so existing bare `<ActivityBar />` / `<TitleBar />` renders (and their existing tests) are unaffected.

- [ ] **Step 1: Write the failing ActivityBar tests (active item toggles, inactive navigates)**

Append to `frontend/src/shell/ActivityBar.test.tsx` (add `fireEvent` to the existing `@testing-library/react` import — it currently imports `{ screen, waitFor }`; and add `vi`/`useLocation` as needed; `vi` is already imported). Add a tiny location probe and the two tests:

```tsx
// (top of file) extend imports:
//   import { fireEvent, screen, waitFor } from "@testing-library/react";
//   import { useLocation } from "react-router-dom";

function LocationEcho() {
  const loc = useLocation();
  return <output data-testid="loc">{loc.pathname + loc.search}</output>;
}

it("active nav item toggles the sidebar instead of navigating (preserves query state)", () => {
  const onToggleSidebar = vi.fn();
  renderWithProviders(
    <>
      <ActivityBar onToggleSidebar={onToggleSidebar} />
      <LocationEcho />
    </>,
    { route: "/cockpit/abc?tab=spec&sec=2" },
  );
  // "Current workspace" is the active item on the editor route.
  fireEvent.click(screen.getByRole("link", { name: "Current workspace" }));
  expect(onToggleSidebar).toHaveBeenCalledTimes(1);
  // Navigation was prevented → the ?tab/?sec query is NOT dropped.
  expect(screen.getByTestId("loc").textContent).toBe("/cockpit/abc?tab=spec&sec=2");
});

it("an inactive nav item still navigates and does not toggle the sidebar", () => {
  const onToggleSidebar = vi.fn();
  renderWithProviders(
    <>
      <ActivityBar onToggleSidebar={onToggleSidebar} />
      <LocationEcho />
    </>,
    { route: "/" },
  );
  // On "/", Workspaces is INACTIVE → clicking it navigates to /cockpit.
  fireEvent.click(screen.getByRole("link", { name: "Workspaces" }));
  expect(onToggleSidebar).not.toHaveBeenCalled();
  expect(screen.getByTestId("loc").textContent).toBe("/cockpit");
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pnpm test -- --run src/shell/ActivityBar.test.tsx`
Expected: the two new tests FAIL (`ActivityBar` does not yet accept `onToggleSidebar`; the active item navigates, dropping the query).

- [ ] **Step 3: Wire `onToggleSidebar` through `ActivityBar`**

Replace `frontend/src/shell/ActivityBar.tsx` with:
```tsx
import type { MouseEvent } from "react";
import { Tooltip } from "@mantine/core";
import { Link, NavLink, useLocation } from "react-router-dom";

import { useSession } from "../api/session";
import { cockpitIdFromPath } from "../cockpit/cockpitNav";
import styles from "./ActivityBar.module.css";
import { ACTIVITY_BOTTOM, ACTIVITY_TOP, type ActivityItem } from "./nav-items";

function Glyph({ icon }: { icon: string }) {
  return (
    <span className={styles.icon} aria-hidden="true">
      <span className={`codicon codicon-${icon}`} />
    </span>
  );
}

function Item({
  item,
  active,
  onToggleSidebar,
}: {
  item: ActivityItem;
  active?: boolean;
  onToggleSidebar?: () => void;
}) {
  const glyph = <Glyph icon={item.icon} />;
  // VS-Code behaviour: clicking the ALREADY-ACTIVE item toggles the sidebar instead
  // of navigating. preventDefault stops react-router's Link nav (it honours
  // defaultPrevented) so we don't re-navigate to the bare route and drop ?tab/?sec.
  // Only fires when this route actually has a sidebar (onToggleSidebar provided).
  const handleClick = (e: MouseEvent<HTMLAnchorElement>) => {
    if (active && onToggleSidebar) {
      e.preventDefault();
      onToggleSidebar();
    }
  };
  let body;
  if (item.kind !== "spa") {
    body = (
      <a href={item.to} aria-label={item.label} className={styles.item}>
        {glyph}
      </a>
    );
  } else if (active !== undefined) {
    // Caller-controlled active state: a plain prefix-matching NavLink would mark
    // Workspaces active across the whole /cockpit subtree, double-lighting it with
    // the Current-workspace chip on /cockpit/:id. Drive it explicitly instead.
    const cls = active ? `${styles.item} ${styles.active}` : styles.item;
    body = (
      <Link
        to={item.to}
        aria-label={item.label}
        aria-current={active ? "page" : undefined}
        className={cls}
        onClick={handleClick}
      >
        {glyph}
      </Link>
    );
  } else {
    body = (
      <NavLink
        to={item.to}
        end={item.to === "/"}
        aria-label={item.label}
        className={({ isActive }) =>
          isActive ? `${styles.item} ${styles.active}` : styles.item
        }
      >
        {glyph}
      </NavLink>
    );
  }
  return (
    <Tooltip label={item.label} position="right" withArrow>
      {body}
    </Tooltip>
  );
}

export function ActivityBar({ onToggleSidebar }: { onToggleSidebar?: () => void }) {
  const { pathname } = useLocation();
  const { data: session } = useSession();
  const isAdmin = Boolean(session?.capabilities.admin);
  const wsId = cockpitIdFromPath(pathname);
  const onCalls = pathname === "/";
  const onCockpitList = pathname.startsWith("/cockpit") && !wsId;
  const bottom = ACTIVITY_BOTTOM.filter((it) => it.id !== "admin" || isAdmin);
  return (
    <nav className={styles.activitybar} aria-label="Primary">
      <div className={styles.group}>
        {ACTIVITY_TOP.map((it) =>
          it.id === "workspaces" ? (
            <Item key={it.id} item={it} active={onCockpitList} onToggleSidebar={onToggleSidebar} />
          ) : it.id === "calls" ? (
            <Item key={it.id} item={it} active={onCalls} onToggleSidebar={onToggleSidebar} />
          ) : (
            <Item key={it.id} item={it} />
          ),
        )}
        {wsId && (
          <Item
            item={{
              id: "active-ws",
              label: "Current workspace",
              icon: "edit",
              kind: "spa",
              to: `/cockpit/${wsId}`,
            }}
            active
            onToggleSidebar={onToggleSidebar}
          />
        )}
      </div>
      <div className={styles.spacer} />
      <div className={styles.group}>
        {bottom.map((it) => (
          <Item key={it.id} item={it} />
        ))}
      </div>
    </nav>
  );
}
```
Note: `Calls` and `Current workspace` move to the explicit-active `<Link>` branch (was NavLink/undefined). They still render an `<a>` with `aria-current="page"` when active, so the existing `ActivityBar.test` assertions (`aria-current` on Calls at `/`, on Current workspace at `/cockpit/abc`, NOT on Calls/Workspaces inside a cockpit) hold. Bottom items (Profiles/Settings/Admin) stay on the NavLink branch — their routes have no sidebar, so no toggle.

- [ ] **Step 4: Run the ActivityBar tests (all green)**

Run: `pnpm test -- --run src/shell/ActivityBar.test.tsx`
Expected: PASS — the two new tests plus all pre-existing ActivityBar tests.

- [ ] **Step 5: Write the failing TitleBar tests (chat toggle gated on hasRightPane)**

Append to `frontend/src/shell/TitleBar.test.tsx`. Add a render helper that passes the new props (the existing `renderAt` renders bare `<TitleBar />` and must keep working). Mock `/api/me` so the component mounts cleanly:

```tsx
function renderWithProps(path: string, props: {
  hasRightPane?: boolean;
  chatCollapsed?: boolean;
  onToggleChat?: () => void;
}) {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(ME as never);
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <TitleBar {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

it("shows a chat-toggle on cockpit routes and calls onToggleChat when clicked", async () => {
  const onToggleChat = vi.fn();
  renderWithProps("/cockpit/ws-1", { hasRightPane: true, chatCollapsed: false, onToggleChat });
  const btn = await screen.findByRole("button", { name: "Toggle chat panel" });
  expect(btn).toHaveAttribute("aria-pressed", "true"); // expanded → pressed
  await userEvent.click(btn);
  expect(onToggleChat).toHaveBeenCalledTimes(1);
});

it("reflects collapsed state on the chat-toggle (aria-pressed=false when collapsed)", async () => {
  renderWithProps("/cockpit/ws-1", { hasRightPane: true, chatCollapsed: true, onToggleChat: vi.fn() });
  const btn = await screen.findByRole("button", { name: "Toggle chat panel" });
  expect(btn).toHaveAttribute("aria-pressed", "false");
});

it("omits the chat-toggle when the route has no right pane", () => {
  renderWithProps("/", { hasRightPane: false, onToggleChat: vi.fn() });
  expect(screen.queryByRole("button", { name: "Toggle chat panel" })).toBeNull();
});
```

- [ ] **Step 6: Run them to verify they fail**

Run: `pnpm test -- --run src/shell/TitleBar.test.tsx`
Expected: the three new tests FAIL (`TitleBar` accepts no props / renders no toggle yet).

- [ ] **Step 7: Add the chat-toggle button + props to `TitleBar.tsx`**

Edit `frontend/src/shell/TitleBar.tsx`:

Change the signature line:
```tsx
export function TitleBar({
  hasRightPane = false,
  chatCollapsed = false,
  onToggleChat,
}: {
  hasRightPane?: boolean;
  chatCollapsed?: boolean;
  onToggleChat?: () => void;
} = {}) {
```

Then add the toggle button as the FIRST child of the `.account` span (so it sits in the right-hand control cluster, always reachable; gated on `hasRightPane`):
```tsx
      <span className={styles.account}>
        {hasRightPane && (
          <button
            type="button"
            className={styles.paneToggle}
            aria-label="Toggle chat panel"
            aria-pressed={!chatCollapsed}
            onClick={onToggleChat}
          >
            <span
              className={`codicon codicon-layout-sidebar-right${chatCollapsed ? "-off" : ""}`}
              aria-hidden="true"
            />
          </button>
        )}
        {email && <span className={styles.chip} title={email}>{email}</span>}
        <button type="button" className={styles.signout} onClick={signOut} disabled={logout.isPending}>
          Sign out
        </button>
      </span>
```

- [ ] **Step 8: Add `.paneToggle` to `TitleBar.module.css`**

Append:
```css
.paneToggle {
  background: none;
  border: 0;
  cursor: pointer;
  color: var(--app-text);
  font-size: 14px;
  display: inline-flex;
  align-items: center;
  padding: 0 2px;
}
.paneToggle:hover {
  color: var(--app-accent);
}
```

- [ ] **Step 9: Run the TitleBar tests (all green)**

Run: `pnpm test -- --run src/shell/TitleBar.test.tsx`
Expected: PASS — the three new tests plus all pre-existing TitleBar tests.

- [ ] **Step 10: Make the side panels collapsible + wire the toggles in `AppShell.tsx`**

Apply these edits to `frontend/src/shell/AppShell.tsx`:

1. Extend the React import and add the type import:
```tsx
import type { ComponentType } from "react";
import { useRef, useState } from "react";
import { Outlet, useLocation, useMatches } from "react-router-dom";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import type { ImperativePanelHandle } from "react-resizable-panels";
```

2. After the `idSuffix` line, add the refs/state/toggles:
```tsx
  // Collapsible side panels. The toggle helpers read the panel's LIVE collapsed
  // state (robust across persisted-layout restore), so the action is always correct
  // even if React state lags. We track `chatCollapsed` only — it drives the TitleBar
  // toggle's icon/aria-pressed; the sidebar's reopen control is the always-present
  // active ActivityBar item, which needs no collapse-state affordance.
  const sidebarRef = useRef<ImperativePanelHandle>(null);
  const chatRef = useRef<ImperativePanelHandle>(null);
  const [chatCollapsed, setChatCollapsed] = useState(false);

  const toggleSidebar = () => {
    const p = sidebarRef.current;
    if (!p) return;
    if (p.isCollapsed()) p.expand();
    else p.collapse();
  };
  const toggleChat = () => {
    const p = chatRef.current;
    if (!p) return;
    if (p.isCollapsed()) p.expand();
    else p.collapse();
  };
```

3. Pass props to `TitleBar`:
```tsx
      <TitleBar
        hasRightPane={Boolean(RightPaneContent)}
        chatCollapsed={chatCollapsed}
        onToggleChat={toggleChat}
      />
```

4. Pass `onToggleSidebar` to `ActivityBar` (only when a sidebar exists):
```tsx
        <ActivityBar onToggleSidebar={SidebarContent ? toggleSidebar : undefined} />
```

5. Make the sidebar Panel collapsible + ref'd:
```tsx
              <Panel
                id="sidebar"
                order={1}
                collapsible
                collapsedSize={0}
                minSize={12}
                defaultSize={18}
                ref={sidebarRef}
              >
```

6. Make the rightpane Panel collapsible + ref'd + state-tracked:
```tsx
              <Panel
                id="rightpane"
                order={3}
                collapsible
                collapsedSize={0}
                minSize={15}
                defaultSize={22}
                ref={chatRef}
                onCollapse={() => setChatCollapsed(true)}
                onExpand={() => setChatCollapsed(false)}
              >
```
(The sidebar panel needs NO `onCollapse`/`onExpand` — nothing consumes a sidebar boolean, and an unused `useState` member would fail `noUnusedLocals`. The `collapsible` flag is still required so `toggleSidebar`'s `collapse()` works.)

- [ ] **Step 11: Run the full type gate + the three shell test files**

Run (from `frontend/`):
```bash
pnpm exec tsc --noEmit
pnpm test -- --run src/shell/AppShell.test.tsx src/shell/ActivityBar.test.tsx src/shell/TitleBar.test.tsx
```
Expected: tsc clean; all three files green.

- [ ] **Step 12: Commit**

```bash
git add frontend/src/shell/AppShell.tsx frontend/src/shell/ActivityBar.tsx frontend/src/shell/TitleBar.tsx frontend/src/shell/TitleBar.module.css frontend/src/shell/ActivityBar.test.tsx frontend/src/shell/TitleBar.test.tsx
git commit -m "feat(spa): collapse + reopen for shell panes (ActivityBar + TitleBar toggles)

Side panels are collapsible (drag past minSize → hidden). Reopen via existing
chrome: the active ActivityBar item toggles the sidebar (preventDefault so it
does not drop ?tab/?sec); a TitleBar button toggles the chat, gated to cockpit
routes. Collapse state persists via the same autoSaveId.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Slice-end verification (after Task 2)

Run from `frontend/`:
1. `pnpm exec tsc --noEmit` — clean.
2. `pnpm test -- --run` — full suite green (whole-suite vitest is reliable here; only backend testcontainers flake, and this slice changes no backend).
3. `pnpm build` — builds (proves the new dep + imports resolve in the production bundle).
4. Visual (the backendless Playwright route-mock harness, per `reference_spa_visual_check`): render the cockpit, confirm the two resize handles render and the chat-toggle appears in the TitleBar.
5. Manual on `:8000`: drag both side edges; drag a side panel past its min to collapse it; reopen the sidebar via its active ActivityBar item (confirm `?tab`/`?sec` survive); reopen the chat via the TitleBar toggle; reload → widths AND collapse state persist; the cockpit sidebar width is independent of the calls-dashboard sidebar width.

---

## Self-Review (author checklist — completed)

- **Spec coverage:** dep add (Task 1 Step 1) ✓; PanelGroup wrap with resize (Task 1) ✓; drop fixed widths (Task 1 Steps 3–4) ✓; `.panes` flex wrapper + `.handle` styling (Task 1 Step 2) ✓; collapse + ActivityBar-reopens-sidebar with preventDefault (Task 2 Steps 1–3) ✓; TitleBar chat toggle gated on `hasRightPane` (Task 2 Steps 5–7) ✓; persistence via `autoSaveId` (Task 1 Step 5) ✓; test updates (both tasks) ✓. Out-of-scope items (bottom panel, ActivityBar width, mobile, central split) correctly untouched.
- **Placeholder scan:** none — every step carries full code/CSS/commands.
- **Type consistency:** `ImperativePanelHandle` ref type, `onToggleSidebar?: () => void` (AppShell↔ActivityBar), `hasRightPane`/`chatCollapsed`/`onToggleChat` (AppShell↔TitleBar) match across tasks. `chatCollapsed` is the only collapse boolean and it IS consumed (TitleBar icon/aria-pressed) → no `noUnusedLocals` violation; the sidebar panel intentionally omits `onCollapse`/`onExpand`.
- **jsdom safety:** all new tests assert roles/labels/callbacks/text, never pixels; `ResizeObserver` already stubbed.
