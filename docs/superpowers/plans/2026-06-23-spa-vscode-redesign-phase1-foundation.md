# SPA Redesign — Phase 1 (Foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the Mantine v9 + custom dark teal theme and a persistent VS Code-style IDE shell (title bar / activity bar / main / status bar) that wraps the existing SPA routes, plus the research-crystal brand identity — so every downstream phase has the theme, shell, and primitives to build on.

**Architecture:** Add Mantine v9 with a custom teal/slate dark theme object + a CSS-variable palette layer. Introduce an `AppShell` layout component rendered as a pathless React Router layout route, so the dashboard (`/`) and cockpit (`/cockpit/:id`) render unchanged inside `<Outlet/>` under persistent chrome. The activity bar distinguishes SPA-native views from Jinja deep-links. This phase is **presentation/scaffolding only** — no route bodies are reskinned (that is Phases 2–3) and no backend/API/schema changes (the sole backend touch is swapping the static `favicon.svg` asset).

**Tech Stack:** React 18.3 + Vite 5 + react-router-dom 7 + TanStack Query 5 + Vitest 2 + Testing Library; new: `@mantine/core`/`@mantine/hooks` v9, `@vscode/codicons`, `@uiw/codemirror-theme-vscode`; pnpm 10.16.

## Global Constraints

- **Package manager is pnpm 10.16 (Node ≥ 20).** Run `pnpm install` after any `package.json` change. A freshly-added worktree may have no `node_modules` — install before testing.
- **Frontend gate after every task: `pnpm exec tsc --noEmit` AND `pnpm vitest run`, both green.** There is no TS linter — `tsc` is the gate. `tsconfig.json` sets `strict`, `noUnusedLocals`, `noUnusedParameters` — no unused imports/params.
- **No backend, API, or DB schema changes.** The only file touched outside `frontend/` is `src/call_manager/web/static/favicon.svg` (a static asset swap; `frontend/index.html` already links it).
- **Re-skin, do not rewrite.** Preserve behavior and component contracts. Existing `*.test.tsx` must stay green. Do NOT migrate the Dashboard or Cockpit bodies in this phase (Phase 2 / Phase 3 own those).
- **Custom dark palette (exact values, from the spec §4), defined once in `src/theme/theme.css`:** editor `#1a1b1e`, sidebar `#202327`, activity bar `#2a2d33`, tab bar `#25282d`, title bar `#30343b`, status bar `#0f766e`, accent `#14b8a6`, bright accent / link `#2dd4bf`, text `#cdd2d8`, muted `#969ca6`, muted-chrome `#9da3ad`, dim `#6e7681`, border `#2c2f35`, hover `#25282e`, selected `#0d3b38`, good `#4ec9b0`, warn `#cca700`, bad `#f14c4c`, editor heading `#4dc4d9`.
- **Per-surface contrast (spec §8):** muted `#969ca6` clears AA on editor/sidebar/activity-bar; the title bar uses the lighter `--app-muted-chrome` (`#9da3ad`). The dim `#6e7681` is for tertiary text on dark surfaces only.
- **Activity bar destinations:** SPA-native = **Calls** (`/`) and the **active Workspace/cockpit** (`/cockpit/:id`, shown only while in a cockpit). External Jinja deep-links = **Workspaces** (`/workspaces`), **Profiles** (`/profiles`), **Settings** (`/me`). **References is NOT a top-level item.** Do not add Admin (needs `is_admin`, out of scope).
- **SPA owns only `/` and `/cockpit`** (`frontend/routes.json` → `routes.gen.ts` `SPA_ROUTES`). Do not add SPA routes. The hand-maintained router drift guard (`router.test.tsx`) must stay green.
- **Dark-first.** `<html data-mantine-color-scheme="dark">` is hard-set; light theme is a later fast-follow (out of scope).
- **Git:** work in the worktree `.worktrees/spa-vscode-redesign` on branch `feat/spa-vscode-redesign`. Subagents must `git -C <abs-worktree-path>` and verify the branch before committing. Commit with `git -c commit.gpgsign=false` and end every commit message with:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`

### Scope notes (deliberate deviations from the spec's Phase-1 bullet)

- The spec lists "themed primitives/wrappers (Button, Select, Tabs, Card, Badge/pill, list row, tree row)" under Phase 1. With Mantine + the theme object, **all Mantine components are themed automatically** — consumers just import from `@mantine/core`. So the theme (Task 1) *is* the themed-primitive delivery. The IDE-specific primitives that have no Mantine equivalent (list row, tree row) and the Select migration (the spec explicitly schedules Select replacement in Phase 2) are built **with their first consumers** in Phases 2–3, per YAGNI — building them now with no usage cannot be meaningfully reviewed.
- The spec lists "sidebar" as a Phase-1 shell region. Its content is contextual (Calls filters in P2; cockpit outline in P3). Phase 1 ships the **title / activity / main / status** regions; the AppShell grid gains the sidebar **column** in Phase 2 alongside its first content. An always-empty rail now would be dead UI.

---

## File Structure

| File | Responsibility |
|---|---|
| `frontend/package.json` | + `@mantine/core`, `@mantine/hooks`, `@vscode/codicons`, `@uiw/codemirror-theme-vscode` |
| `frontend/src/theme/theme.ts` | The Mantine theme object (teal primary scale, fonts, radius) |
| `frontend/src/theme/theme.css` | `--app-*` chrome palette + per-surface muted + base body styling + dark `--cm-*` compatibility remap |
| `frontend/src/test-utils.tsx` | `renderWithProviders` (MantineProvider + QueryClient + MemoryRouter) for component tests |
| `frontend/src/main.tsx` | + import Mantine styles, Codicons CSS, and `theme.css` (after the legacy CSS) |
| `frontend/src/App.tsx` | + wrap with `MantineProvider theme defaultColorScheme="dark"` |
| `frontend/index.html` | + `data-mantine-color-scheme="dark"` on `<html>` (no flash) |
| `frontend/src/shell/BrandMark.tsx` | The research-crystal mark as inline themeable SVG |
| `frontend/src/shell/TitleBar.tsx` (+`.module.css`) | Slim top bar: brand + per-route title |
| `frontend/src/shell/StatusBar.tsx` (+`.module.css`) | Teal status bar (minimal global content in P1) |
| `frontend/src/shell/nav-items.ts` | Activity-bar destination config (SPA vs external) |
| `frontend/src/shell/ActivityBar.tsx` (+`.module.css`) | Vertical nav; Codicons; active state; deep-links |
| `frontend/src/shell/AppShell.tsx` (+`.module.css`) | Grid layout composing the chrome + `<Outlet/>` |
| `frontend/src/router.tsx` | Wrap routes in the `AppShell` pathless layout route |
| `frontend/src/router.test.tsx` | Drift guard updated to recurse into layout-route children |
| `src/call_manager/web/static/favicon.svg` | Replaced with the research-crystal favicon |

---

### Task 1: Mantine foundation — deps, theme, palette, providers, test-utils

**Files:**
- Modify: `frontend/package.json` (dependencies)
- Create: `frontend/src/theme/theme.ts`
- Create: `frontend/src/theme/theme.css`
- Create: `frontend/src/theme/theme.test.tsx`
- Create: `frontend/src/test-utils.tsx`
- Modify: `frontend/src/main.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/index.html`

**Interfaces:**
- Produces: `theme` (a Mantine `MantineThemeOverride` from `createTheme`) exported from `frontend/src/theme/theme.ts`; `renderWithProviders(ui, opts?)` from `frontend/src/test-utils.tsx` (wraps MantineProvider + QueryClientProvider + MemoryRouter; `opts.route?: string` default `"/"`). All later tasks' component tests use `renderWithProviders`.
- Consumes: nothing from earlier tasks.

- [ ] **Step 1: Add the dependencies**

Run (in `frontend/`):
```bash
pnpm add @mantine/core@9.4.0 @mantine/hooks@9.4.0 @vscode/codicons@0.0.45 @uiw/codemirror-theme-vscode@4.25.10
```
Pin exact versions on the command line — `.npmrc`'s `save-exact=true` does **not** override an explicit `^`/`~` range passed on the CLI (pnpm 10.16 writes the range verbatim), so an `@^9` here would land a caret in `package.json` and violate the repo's exact-pin policy. With the exact versions above, `package.json` + `pnpm-lock.yaml` both get exact pins. (`@uiw/codemirror-theme-vscode` is installed now per the spec; it is wired into the editor in Phase 3 — unused in P1 is fine, it is a dependency, not an import.)

Expected: `package.json` `dependencies` now contains the four packages; `pnpm install` completes without error.

- [ ] **Step 2: Write the failing theme test**

Create `frontend/src/theme/theme.test.tsx`:
```tsx
import { Button } from "@mantine/core";
import { screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { renderWithProviders } from "../test-utils";
import { theme } from "./theme";

it("uses the teal primary palette with 10 shades", () => {
  expect(theme.primaryColor).toBe("teal");
  expect(theme.colors?.teal).toHaveLength(10);
  expect(theme.colors?.teal?.[5]).toBe("#2dd4bf");
  expect(theme.colors?.teal?.[6]).toBe("#14b8a6");
});

it("renders Mantine components through the provider in test-utils", () => {
  renderWithProviders(<Button>Save</Button>);
  const btn = screen.getByRole("button", { name: "Save" });
  expect(btn.className).toMatch(/mantine-Button/);
});
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `pnpm vitest run src/theme/theme.test.tsx`
Expected: FAIL — cannot resolve `./theme` and `../test-utils` (modules not created yet).

- [ ] **Step 4: Create the Mantine theme object**

Create `frontend/src/theme/theme.ts`:
```ts
import { createTheme, type MantineColorsTuple } from "@mantine/core";

// Teal scale, light -> dark. Index 6 = primary accent (#14b8a6); index 5 =
// the brighter accent (#2dd4bf) used as the visible primary on the dark scheme.
const teal: MantineColorsTuple = [
  "#e6fcf7",
  "#c8f6ec",
  "#9aeeda",
  "#66e3c8",
  "#3ed8b8",
  "#2dd4bf",
  "#14b8a6",
  "#0f9d8f",
  "#0f766e",
  "#0c5d57",
];

export const theme = createTheme({
  primaryColor: "teal",
  primaryShade: { light: 6, dark: 5 },
  colors: { teal },
  fontFamily:
    '-apple-system, "Segoe UI", system-ui, "Helvetica Neue", Arial, sans-serif',
  fontFamilyMonospace:
    '"Cascadia Code", "JetBrains Mono", "SF Mono", "Consolas", ui-monospace, monospace',
  defaultRadius: "sm",
  radius: { sm: "4px" },
});
```

- [ ] **Step 5: Create the test-utils render helper**

Create `frontend/src/test-utils.tsx`:
```tsx
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderOptions } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";

import { theme } from "./theme/theme";

type Options = { route?: string } & Omit<RenderOptions, "wrapper">;

// Single source of truth for rendering components that need the app providers.
// MantineProvider is forced to the dark scheme to match production. Components
// using useLocation / NavLink get a MemoryRouter at `route` (default "/").
export function renderWithProviders(ui: ReactElement, opts: Options = {}) {
  const { route = "/", ...rest } = opts;
  const qc = new QueryClient();
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
        </QueryClientProvider>
      </MantineProvider>
    );
  }
  return render(ui, { wrapper: Wrapper, ...rest });
}
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `pnpm vitest run src/theme/theme.test.tsx`
Expected: PASS (both tests). (Mantine needs `window.matchMedia` + `ResizeObserver`; these are already polyfilled in `src/test-setup.ts`.)

- [ ] **Step 7: Create the palette + base CSS layer**

Create `frontend/src/theme/theme.css`:
```css
/* frontend/src/theme/theme.css
 * Custom dark "VS Code-style, our identity" palette. Imported AFTER
 * @mantine/core/styles.css and AFTER the legacy styles/tokens.css so these
 * declarations win the cascade. Two jobs:
 *  1. Define the --app-* chrome variables the IDE shell uses.
 *  2. Remap the legacy light --cm-* tokens (styles/tokens.css) to dark values
 *     so the not-yet-reskinned Dashboard/Cockpit bodies read as a dark IDE
 *     instead of light panels on a dark shell. Phases 2-3 rewrite those rules
 *     onto --app-* and this remap block goes away. */

:root {
  --app-editor: #1a1b1e;
  --app-sidebar: #202327;
  --app-activitybar: #2a2d33;
  --app-tabbar: #25282d;
  --app-titlebar: #30343b;
  --app-statusbar: #0f766e;
  --app-accent: #14b8a6;
  --app-accent-bright: #2dd4bf;
  --app-link: #2dd4bf;
  --app-text: #cdd2d8;
  --app-muted: #969ca6; /* AA on editor/sidebar/activity-bar */
  --app-muted-chrome: #9da3ad; /* lighter, for the title-bar surface */
  --app-dim: #6e7681; /* timestamps / tertiary, dark surfaces only */
  --app-border: #2c2f35;
  --app-border-strong: #343941;
  --app-hover: #25282e;
  --app-selected: #0d3b38;
  --app-good: #4ec9b0;
  --app-warn: #cca700;
  --app-bad: #f14c4c;
  --app-heading: #4dc4d9;
  color-scheme: dark;
}

body {
  background: var(--app-editor);
  color: var(--app-text);
  font-family:
    -apple-system, "Segoe UI", system-ui, "Helvetica Neue", Arial, sans-serif;
}

/* Phase-1 dark compatibility remap of the legacy --cm-* tokens. Specificity
   :root[...] (0,2,0) matches styles/tokens.css's :root:not([data-theme=dark])
   (0,2,0); this file is imported later, so it wins on source order. */
:root[data-mantine-color-scheme="dark"] {
  --cm-primary: #2dd4bf;
  --cm-primary-hover: #5fe0d0;
  --cm-primary-soft: #14302e;
  --cm-primary-border: #1f4a46;
  --cm-ink: #cdd2d8;
  --cm-ink-strong: #ffffff;
  --cm-ink-muted: #969ca6;
  --cm-muted: #969ca6;
  --cm-canvas: #1a1b1e;
  --cm-surface: #202327;
  --cm-surface-sunken: #1d2024;
  --cm-border: #2c2f35;
  --cm-border-soft: #25282d;

  --cm-good-text: #4ec9b0;
  --cm-good-bg: #11302b;
  --cm-good-border: #1f5048;
  --cm-warn-text: #e0b341;
  --cm-warn-bg: #322a12;
  --cm-warn-border: #5a4a1c;
  --cm-bad-text: #f4807f;
  --cm-bad-bg: #3a1f1f;
  --cm-bad-border: #5e2e2e;
  --cm-neutral-text: #969ca6;
  --cm-neutral-bg: #25282d;
  --cm-neutral-border: #2c2f35;
  --cm-deadline-soon: #f14c4c;
  --cm-deadline-watch: #e0b341;
}
```

- [ ] **Step 8: Wire styles into the entry point**

Replace the import block at the top of `frontend/src/main.tsx`. Current:
```tsx
import { App } from "./App";
import "./styles/tokens.css";
import "./styles/cockpit.css";
```
New (Mantine + Codicons first, then legacy tokens, then our theme.css LAST so its dark remap wins):
```tsx
import "@mantine/core/styles.css";
import "@vscode/codicons/dist/codicon.css";

import { App } from "./App";
import "./styles/tokens.css";
import "./styles/cockpit.css";
import "./theme/theme.css";
```
(Leave the rest of `main.tsx` — the `createRoot(...).render(<StrictMode><App /></StrictMode>)` — unchanged.)

- [ ] **Step 9: Add MantineProvider in App.tsx**

In `frontend/src/App.tsx`, add the import and wrap the existing providers. Current body:
```tsx
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
New:
```tsx
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router-dom";

import { ErrorBoundary } from "./components/ErrorBoundary";
import { router } from "./router";
import { theme } from "./theme/theme";

const qc = new QueryClient({ defaultOptions: { queries: { refetchOnWindowFocus: true } } });

export function App() {
  return (
    <ErrorBoundary>
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <QueryClientProvider client={qc}>
          <RouterProvider router={router} />
        </QueryClientProvider>
      </MantineProvider>
    </ErrorBoundary>
  );
}
```

- [ ] **Step 10: Hard-set the dark color scheme in index.html (no flash)**

In `frontend/index.html`, change the opening html tag from:
```html
<html lang="en">
```
to:
```html
<html lang="en" data-mantine-color-scheme="dark">
```
(Leave the existing `<link rel="icon" type="image/svg+xml" href="/static/favicon.svg" />` — Task 2 replaces the target file, not the link.)

- [ ] **Step 11: Run the full frontend gate**

Run: `pnpm exec tsc --noEmit && pnpm vitest run`
Expected: tsc clean; all tests pass (the new theme tests + all pre-existing tests unchanged — the existing Dashboard/Cockpit tests render their components directly without Mantine and are unaffected).

- [ ] **Step 12: Commit**

```bash
git -c commit.gpgsign=false add -A
git -c commit.gpgsign=false commit -m "feat(spa): Mantine v9 + custom dark teal theme foundation

Add @mantine/core/hooks v9, @vscode/codicons, @uiw/codemirror-theme-vscode.
Custom dark teal/slate palette in src/theme/theme.css (--app-* chrome vars +
per-surface muted + a dark --cm-* compat remap so the not-yet-reskinned bodies
read dark). MantineProvider (defaultColorScheme=dark) wraps the app; html is
hard-set to the dark scheme. Adds renderWithProviders test helper.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Brand identity — BrandMark, TitleBar, StatusBar, favicon

**Files:**
- Create: `frontend/src/shell/BrandMark.tsx`
- Create: `frontend/src/shell/BrandMark.test.tsx`
- Create: `frontend/src/shell/TitleBar.tsx`
- Create: `frontend/src/shell/TitleBar.module.css`
- Create: `frontend/src/shell/TitleBar.test.tsx`
- Create: `frontend/src/shell/StatusBar.tsx`
- Create: `frontend/src/shell/StatusBar.module.css`
- Create: `frontend/src/shell/StatusBar.test.tsx`
- Replace: `src/call_manager/web/static/favicon.svg`

**Interfaces:**
- Consumes: `renderWithProviders` (Task 1).
- Produces: `BrandMark` (`{ size?: number }`), `TitleBar` (no props; uses `useLocation`), `StatusBar` (no props) — all from `frontend/src/shell/`. Task 4's `AppShell` composes `TitleBar` and `StatusBar`.

- [ ] **Step 1: Write the failing BrandMark test**

Create `frontend/src/shell/BrandMark.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { BrandMark } from "./BrandMark";

it("renders an accessible crystal mark at the requested size", () => {
  render(<BrandMark size={24} />);
  const svg = screen.getByLabelText("Call Manager");
  expect(svg).toBeInTheDocument();
  expect(svg.getAttribute("width")).toBe("24");
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pnpm vitest run src/shell/BrandMark.test.tsx`
Expected: FAIL — `./BrandMark` does not exist.

- [ ] **Step 3: Create BrandMark (the research-crystal, themeable)**

Create `frontend/src/shell/BrandMark.tsx`:
```tsx
// The "research crystal" brand mark: a hexagonal knowledge-network with a teal
// center node. Simplified radial form (clean at small sizes). Strokes/nodes use
// theme CSS vars so it adapts; the center node is the teal accent.
export function BrandMark({ size = 20 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 256 256"
      role="img"
      aria-label="Call Manager"
    >
      <path
        d="M128 22 218 74v108l-90 52-90-52V74z"
        fill="none"
        stroke="var(--app-text)"
        strokeWidth="14"
        strokeLinejoin="round"
      />
      <g stroke="var(--app-dim)" strokeWidth="10" strokeLinecap="round">
        <path d="M128 128 128 56M128 128 82 84M128 128 174 84M128 128 82 172M128 128 174 172M128 128 128 200" />
      </g>
      <g fill="var(--app-text)">
        <circle cx="128" cy="56" r="13" />
        <circle cx="82" cy="84" r="13" />
        <circle cx="174" cy="84" r="13" />
        <circle cx="82" cy="172" r="13" />
        <circle cx="174" cy="172" r="13" />
        <circle cx="128" cy="200" r="13" />
      </g>
      <circle
        cx="128"
        cy="128"
        r="22"
        fill="var(--app-accent-bright)"
        stroke="var(--app-editor)"
        strokeWidth="7"
      />
    </svg>
  );
}
```

- [ ] **Step 4: Run it to verify it passes**

Run: `pnpm vitest run src/shell/BrandMark.test.tsx`
Expected: PASS.

- [ ] **Step 5: Write the failing TitleBar test**

Create `frontend/src/shell/TitleBar.test.tsx`:
```tsx
import { screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { renderWithProviders } from "../test-utils";
import { TitleBar } from "./TitleBar";

it("shows the brand and the calls title on the dashboard route", () => {
  renderWithProviders(<TitleBar />, { route: "/" });
  expect(screen.getByText("Call Manager")).toBeInTheDocument();
  expect(screen.getByText("Funding calls")).toBeInTheDocument();
});

it("shows the workspace title on a cockpit route", () => {
  renderWithProviders(<TitleBar />, { route: "/cockpit/abc" });
  expect(screen.getByText("Proposal workspace")).toBeInTheDocument();
});
```

- [ ] **Step 6: Run it to verify it fails**

Run: `pnpm vitest run src/shell/TitleBar.test.tsx`
Expected: FAIL — `./TitleBar` does not exist.

- [ ] **Step 7: Create TitleBar + its CSS**

Create `frontend/src/shell/TitleBar.tsx`:
```tsx
import { useLocation } from "react-router-dom";

import { BrandMark } from "./BrandMark";
import styles from "./TitleBar.module.css";

function titleForPath(pathname: string): string {
  if (pathname.startsWith("/cockpit")) return "Proposal workspace";
  return "Funding calls";
}

export function TitleBar() {
  const { pathname } = useLocation();
  return (
    <header className={styles.titlebar}>
      <span className={styles.brand}>
        <BrandMark size={18} />
        <span className={styles.app}>Call Manager</span>
      </span>
      <span className={styles.title}>{titleForPath(pathname)}</span>
    </header>
  );
}
```

Create `frontend/src/shell/TitleBar.module.css`:
```css
.titlebar {
  display: flex;
  align-items: center;
  gap: 14px;
  height: 30px;
  padding: 0 12px;
  background: var(--app-titlebar);
  color: var(--app-muted-chrome);
  font-size: 12px;
  border-bottom: 1px solid var(--app-border);
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--app-text);
}
.app {
  font-weight: 600;
}
.title {
  margin: 0 auto;
  color: var(--app-muted-chrome);
}
```

- [ ] **Step 8: Run it to verify it passes**

Run: `pnpm vitest run src/shell/TitleBar.test.tsx`
Expected: PASS (both cases).

- [ ] **Step 9: Write the failing StatusBar test**

Create `frontend/src/shell/StatusBar.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { StatusBar } from "./StatusBar";

it("renders the status bar landmark with the app label", () => {
  render(<StatusBar />);
  const bar = screen.getByLabelText("Status bar");
  expect(bar).toBeInTheDocument();
  expect(bar).toHaveTextContent("Call Manager");
});
```

- [ ] **Step 10: Run it to verify it fails**

Run: `pnpm vitest run src/shell/StatusBar.test.tsx`
Expected: FAIL — `./StatusBar` does not exist.

- [ ] **Step 11: Create StatusBar + its CSS**

Create `frontend/src/shell/StatusBar.tsx`:
```tsx
import styles from "./StatusBar.module.css";

// Phase 1: a minimal global status bar. Cockpit-specific content (branch,
// deadline, spec-lock, scorecard band, Ln/Col, language mode) is wired in
// Phase 3 when the cockpit reconciles into the shell regions.
export function StatusBar() {
  return (
    <footer className={styles.statusbar} aria-label="Status bar">
      <span className={styles.spacer} />
      <span className={styles.item}>Call Manager</span>
    </footer>
  );
}
```

Create `frontend/src/shell/StatusBar.module.css`:
```css
.statusbar {
  display: flex;
  align-items: center;
  gap: 16px;
  height: 22px;
  padding: 0 12px;
  background: var(--app-statusbar);
  color: #fff; /* white on #0f766e ~ 4.7:1, clears AA */
  font-size: 12px;
}
.item {
  display: flex;
  align-items: center;
  gap: 5px;
}
.spacer {
  flex: 1;
}
```

- [ ] **Step 12: Run it to verify it passes**

Run: `pnpm vitest run src/shell/StatusBar.test.tsx`
Expected: PASS.

- [ ] **Step 13: Install the research-crystal favicon**

The finalized teal favicon asset is committed at `docs/superpowers/specs/assets/research-crystal-favicon.svg`. Copy it over the old prototype that `index.html` links:
```bash
cp docs/superpowers/specs/assets/research-crystal-favicon.svg \
   src/call_manager/web/static/favicon.svg
```
Verify it landed and is the crystal (a hexagon path + a teal center node):
```bash
grep -c 'M128 22 218 74' src/call_manager/web/static/favicon.svg   # expect 1
grep -c '#2dd4bf' src/call_manager/web/static/favicon.svg           # expect >=1
```
Expected: both `grep -c` print a non-zero count. (No automated test — `index.html` already references `/static/favicon.svg`; this is a static-asset swap.)

- [ ] **Step 14: Run the full frontend gate**

Run: `pnpm exec tsc --noEmit && pnpm vitest run`
Expected: tsc clean; all tests pass.

- [ ] **Step 15: Commit**

```bash
git -c commit.gpgsign=false add -A
git -c commit.gpgsign=false commit -m "feat(spa): research-crystal brand mark + title/status bars + favicon

BrandMark renders the simplified research-crystal (themeable via --app-* vars,
teal center node). TitleBar (brand + per-route title) and StatusBar (minimal
teal global bar) are the first IDE-shell chrome leaves. Replace the static
favicon.svg with the finalized teal crystal.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Activity bar — nav config + the vertical rail

**Files:**
- Create: `frontend/src/shell/nav-items.ts`
- Create: `frontend/src/shell/ActivityBar.tsx`
- Create: `frontend/src/shell/ActivityBar.module.css`
- Create: `frontend/src/shell/ActivityBar.test.tsx`

**Interfaces:**
- Consumes: `renderWithProviders` (Task 1).
- Produces: `ActivityBar` (no props; uses `useLocation`) from `frontend/src/shell/ActivityBar.tsx`; `ACTIVITY_TOP`, `ACTIVITY_BOTTOM`, and `type ActivityItem` from `frontend/src/shell/nav-items.ts`. Task 4's `AppShell` composes `ActivityBar`.

- [ ] **Step 1: Write the failing ActivityBar test**

Create `frontend/src/shell/ActivityBar.test.tsx`:
```tsx
import { screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { renderWithProviders } from "../test-utils";
import { ActivityBar } from "./ActivityBar";

it("Calls is an SPA link to / and is active on the dashboard route", () => {
  renderWithProviders(<ActivityBar />, { route: "/" });
  const calls = screen.getByRole("link", { name: "Calls" });
  expect(calls).toHaveAttribute("href", "/");
  // NavLink sets aria-current="page" when active (robust; no reliance on
  // hashed CSS-module class names).
  expect(calls).toHaveAttribute("aria-current", "page");
});

it("Workspaces, Profiles and Settings deep-link to their Jinja pages", () => {
  renderWithProviders(<ActivityBar />, { route: "/" });
  expect(screen.getByRole("link", { name: "Workspaces" })).toHaveAttribute(
    "href",
    "/workspaces",
  );
  expect(screen.getByRole("link", { name: "Profiles" })).toHaveAttribute(
    "href",
    "/profiles",
  );
  expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute(
    "href",
    "/me",
  );
});

it("hides the current-workspace item outside a cockpit", () => {
  renderWithProviders(<ActivityBar />, { route: "/" });
  expect(screen.queryByRole("link", { name: "Current workspace" })).toBeNull();
});

// Separate test: a second render in the same test would leave two ActivityBars
// mounted (RTL only cleans up between tests), so `getByRole("link", { name:
// "Calls" })` would throw on the duplicate. Each test does a single render.
it("shows the current-workspace item inside a cockpit and deactivates Calls", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit/abc" });
  const ws = screen.getByRole("link", { name: "Current workspace" });
  expect(ws).toHaveAttribute("href", "/cockpit/abc");
  // Calls must not be active while in a cockpit.
  expect(screen.getByRole("link", { name: "Calls" })).not.toHaveAttribute(
    "aria-current",
    "page",
  );
});

it("has no References destination", () => {
  renderWithProviders(<ActivityBar />, { route: "/" });
  expect(screen.queryByRole("link", { name: /references/i })).toBeNull();
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pnpm vitest run src/shell/ActivityBar.test.tsx`
Expected: FAIL — `./ActivityBar` / `./nav-items` do not exist.

- [ ] **Step 3: Create the nav-items config**

Create `frontend/src/shell/nav-items.ts`:
```ts
export type NavKind = "spa" | "external";

export interface ActivityItem {
  id: string;
  label: string;
  icon: string; // Codicon name without the "codicon-" prefix
  kind: NavKind; // "spa" -> react-router NavLink; "external" -> Jinja page <a>
  to: string;
}

// Primary group. The active Workspace/cockpit item is NOT here — ActivityBar
// renders it dynamically only while a cockpit is open. References is a
// per-workspace concern (cockpit sidebar), never a top-level destination.
export const ACTIVITY_TOP: ActivityItem[] = [
  { id: "calls", label: "Calls", icon: "search", kind: "spa", to: "/" },
  {
    id: "workspaces",
    label: "Workspaces",
    icon: "folder",
    kind: "external",
    to: "/workspaces",
  },
];

// Bottom-pinned group. These mirror the existing Jinja nav exactly
// (base.html: /profiles, /me) and deep-link out until those pages migrate.
export const ACTIVITY_BOTTOM: ActivityItem[] = [
  {
    id: "profiles",
    label: "Profiles",
    icon: "account",
    kind: "external",
    to: "/profiles",
  },
  { id: "settings", label: "Settings", icon: "gear", kind: "external", to: "/me" },
];
```

- [ ] **Step 4: Create ActivityBar + its CSS**

Create `frontend/src/shell/ActivityBar.tsx`:
```tsx
import { Tooltip } from "@mantine/core";
import { NavLink, useLocation } from "react-router-dom";

import styles from "./ActivityBar.module.css";
import { ACTIVITY_BOTTOM, ACTIVITY_TOP, type ActivityItem } from "./nav-items";

function Glyph({ icon }: { icon: string }) {
  return (
    <span className={styles.icon} aria-hidden="true">
      <span className={`codicon codicon-${icon}`} />
    </span>
  );
}

function Item({ item }: { item: ActivityItem }) {
  const body =
    item.kind === "spa" ? (
      <NavLink
        to={item.to}
        end={item.to === "/"}
        aria-label={item.label}
        className={({ isActive }) =>
          isActive ? `${styles.item} ${styles.active}` : styles.item
        }
      >
        <Glyph icon={item.icon} />
      </NavLink>
    ) : (
      <a href={item.to} aria-label={item.label} className={styles.item}>
        <Glyph icon={item.icon} />
      </a>
    );
  return (
    <Tooltip label={item.label} position="right" withArrow>
      {body}
    </Tooltip>
  );
}

export function ActivityBar() {
  const { pathname } = useLocation();
  const inCockpit = pathname.startsWith("/cockpit");
  return (
    <nav className={styles.activitybar} aria-label="Primary">
      <div className={styles.group}>
        {ACTIVITY_TOP.map((it) => (
          <Item key={it.id} item={it} />
        ))}
        {inCockpit && (
          <Item
            item={{
              id: "active-ws",
              label: "Current workspace",
              icon: "edit",
              kind: "spa",
              to: pathname,
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

Create `frontend/src/shell/ActivityBar.module.css`:
```css
.activitybar {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: 48px;
  background: var(--app-activitybar);
  padding-top: 6px;
}
.group {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
}
.spacer {
  flex: 1;
}
.item {
  width: 48px;
  height: 48px;
  display: grid;
  place-items: center;
  color: var(--app-muted);
  border-left: 2px solid transparent;
}
.item:hover {
  color: var(--app-text);
}
.active {
  color: #fff;
  border-left-color: #fff;
}
.icon .codicon {
  font-size: 22px;
}
```

- [ ] **Step 5: Run it to verify it passes**

Run: `pnpm vitest run src/shell/ActivityBar.test.tsx`
Expected: PASS (all four cases). (The `name` query matches the `aria-label` on each link; Mantine `Tooltip` renders its child link directly.)

- [ ] **Step 6: Run the full frontend gate**

Run: `pnpm exec tsc --noEmit && pnpm vitest run`
Expected: tsc clean; all tests pass.

- [ ] **Step 7: Commit**

```bash
git -c commit.gpgsign=false add -A
git -c commit.gpgsign=false commit -m "feat(spa): IDE activity bar with SPA-native vs Jinja deep-link nav

Vertical activity bar (Codicons). SPA-native NavLinks: Calls (/) + the active
Workspace/cockpit (shown only inside /cockpit/:id). External Jinja deep-links
mirroring base.html: Workspaces (/workspaces), Profiles (/profiles), Settings
(/me). References is intentionally not a top-level destination.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: AppShell composition + router layout-route integration

**Files:**
- Create: `frontend/src/shell/AppShell.tsx`
- Create: `frontend/src/shell/AppShell.module.css`
- Create: `frontend/src/shell/AppShell.test.tsx`
- Modify: `frontend/src/router.tsx`
- Modify: `frontend/src/router.test.tsx`

**Interfaces:**
- Consumes: `TitleBar`, `StatusBar` (Task 2), `ActivityBar` (Task 3); `Outlet` from react-router; the existing `Dashboard` / `Cockpit` route components.
- Produces: `AppShell` (no props; renders chrome + `<Outlet/>`) from `frontend/src/shell/AppShell.tsx`. Wires the persistent shell as the router's layout route.

- [ ] **Step 1: Write the failing AppShell test**

Create `frontend/src/shell/AppShell.test.tsx`:
```tsx
import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { expect, it } from "vitest";

import { theme } from "../theme/theme";
import { AppShell } from "./AppShell";

function renderShellAt(route: string) {
  const router = createMemoryRouter(
    [
      {
        element: <AppShell />,
        children: [{ index: true, element: <p>ROUTED BODY</p> }],
      },
    ],
    { initialEntries: [route] },
  );
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <RouterProvider router={router} />
    </MantineProvider>,
  );
}

it("renders the chrome regions and the routed outlet content", () => {
  renderShellAt("/");
  // "Funding calls" is the per-route title (unique). NB: "Call Manager" appears
  // in BOTH the title bar brand and the status bar, so don't getByText it here.
  expect(screen.getByText("Funding calls")).toBeInTheDocument(); // title bar
  expect(screen.getByLabelText("Primary")).toBeInTheDocument(); // activity bar
  expect(screen.getByLabelText("Status bar")).toBeInTheDocument(); // status bar
  expect(screen.getByText("ROUTED BODY")).toBeInTheDocument(); // <Outlet/>
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pnpm vitest run src/shell/AppShell.test.tsx`
Expected: FAIL — `./AppShell` does not exist.

- [ ] **Step 3: Create AppShell + its CSS**

Create `frontend/src/shell/AppShell.tsx`:
```tsx
import { Outlet } from "react-router-dom";

import { ActivityBar } from "./ActivityBar";
import styles from "./AppShell.module.css";
import { StatusBar } from "./StatusBar";
import { TitleBar } from "./TitleBar";

// The persistent IDE shell. Phase 1 ships title / activity / main / status.
// The contextual sidebar column is added in Phase 2 (Calls filters) / Phase 3
// (cockpit outline). Route bodies render unchanged inside <Outlet/>.
export function AppShell() {
  return (
    <div className={styles.shell}>
      <TitleBar />
      <div className={styles.body}>
        <ActivityBar />
        <div className={styles.main}>
          <Outlet />
        </div>
      </div>
      <StatusBar />
    </div>
  );
}
```

Create `frontend/src/shell/AppShell.module.css`:
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
.main {
  flex: 1;
  min-width: 0;
  overflow: auto;
}
```

- [ ] **Step 4: Run it to verify it passes**

Run: `pnpm vitest run src/shell/AppShell.test.tsx`
Expected: PASS.

- [ ] **Step 5: Update the router test drift guard (write the new expectation first)**

The router will move from flat routes to a pathless layout route, so the guard
must recurse into children and build absolute leaf paths. Replace the entire
contents of `frontend/src/router.test.tsx` with:
```tsx
import type { RouteObject } from "react-router-dom";
import { expect, it } from "vitest";

import { router } from "./router";
import { SPA_ROUTES } from "./routes.gen";

// Collect every concrete LEAF page path the router can match, as an absolute
// path. A pathless layout route (no `path`) contributes nothing itself; its
// children inherit the parent base. An index route resolves to the parent base.
function leafPaths(routes: RouteObject[], base = ""): string[] {
  const out: string[] = [];
  for (const r of routes) {
    const here =
      r.path == null
        ? base
        : r.path.startsWith("/")
          ? r.path
          : `${base.replace(/\/$/, "")}/${r.path}`;
    if (r.children?.length) out.push(...leafPaths(r.children, here));
    else out.push(r.index ? base || "/" : here);
  }
  return out;
}

// router.tsx is the only HAND-maintained consumer of the route-ownership
// manifest. Pin its concrete page paths (excluding the in-app "*" soft-404) to
// SPA_ROUTES, reducing each ":param" path to its static ownership prefix
// ("/cockpit/:id" -> "/cockpit") the way is_spa_route matches subtrees.
it("router concrete paths match the SPA_ROUTES manifest (drift guard)", () => {
  const concrete = leafPaths(router.routes as RouteObject[])
    .filter((p) => !p.includes("*"))
    .map((p) => {
      const i = p.indexOf("/:");
      return i === -1 ? p : p.slice(0, i) || "/";
    });
  expect(new Set(concrete)).toEqual(new Set(SPA_ROUTES));
});
```

- [ ] **Step 6: Run the router test to verify it fails**

Run: `pnpm vitest run src/router.test.tsx`
Expected: FAIL — the router is still flat (no layout route yet), so the guard
either still passes against the OLD shape or the new `leafPaths` reads the flat
routes. To make the failure meaningful, this step's expectation is paired with
Step 7; if it happens to pass against the flat router, proceed to Step 7 and
re-run in Step 8 (the guard must hold for the NEW nested shape).

- [ ] **Step 7: Wrap the routes in the AppShell layout route**

Replace the entire contents of `frontend/src/router.tsx` with:
```tsx
import { createBrowserRouter } from "react-router-dom";

import { Cockpit } from "./routes/Cockpit";
import { Dashboard } from "./routes/Dashboard";
import { AppShell } from "./shell/AppShell";

export const router = createBrowserRouter([
  {
    // Persistent IDE shell wraps every SPA route via <Outlet/>. This pathless
    // layout route keeps the shell mounted across navigations. Child page paths
    // are relative to "/" and MUST stay in sync with routes.json `spa_routes`
    // (the router.test drift guard enforces it).
    element: <AppShell />,
    children: [
      { index: true, element: <Dashboard /> },
      { path: "cockpit/:id", element: <Cockpit /> },
      // In-app soft 404 ONLY (client-side reached). NOT the route authority —
      // the server returns index.html only for manifest SPA routes, so an
      // unknown typed URL hits FastAPI's real 404, never this. Must not turn
      // unknown paths into 200s.
      {
        path: "*",
        element: (
          <main role="alert">
            <h1>Not found</h1>
          </main>
        ),
      },
    ],
  },
]);
```

- [ ] **Step 8: Run the router test to verify it passes against the nested shape**

Run: `pnpm vitest run src/router.test.tsx`
Expected: PASS — `leafPaths` yields `["/", "/cockpit/:id", "/*"]`; the `*` is filtered, `"/cockpit/:id"` reduces to `"/cockpit"`, giving `{"/", "/cockpit"}` which equals `SPA_ROUTES`.

- [ ] **Step 9: Run the full frontend gate + a build smoke**

Run: `pnpm exec tsc --noEmit && pnpm vitest run && pnpm build`
Expected: tsc clean; all tests pass; `pnpm build` (`tsc && vite build`) produces `frontend/dist` with no errors (confirms the Mantine + Codicons imports bundle).

- [ ] **Step 10: Commit**

```bash
git -c commit.gpgsign=false add -A
git -c commit.gpgsign=false commit -m "feat(spa): persistent IDE AppShell as the router layout route

AppShell composes the title bar / activity bar / main(Outlet) / status bar in a
full-height grid. router.tsx wraps the dashboard and cockpit routes in this
pathless layout route so the shell stays mounted across navigations; the bodies
render unchanged inside <Outlet/>. The hand-maintained route drift guard now
recurses into the layout-route children.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Manual verification (after Task 4, before merge)

Not a task — a reviewer/controller check. Run the dev server and confirm the
shell renders dark with the chrome around the existing pages:
```bash
cd frontend && pnpm dev   # then load http://localhost:5173/ (needs the backend on :8000 for data)
```
Confirm: dark editor background; teal status bar; activity bar with Calls active
on `/`; the crystal favicon in the browser tab; the dashboard list renders
inside the shell (dark, via the --cm-* remap); navigating to a `/cockpit/:id`
shows the "Current workspace" activity item and the cockpit body inside the
shell. (The cockpit/dashboard bodies are intentionally not yet reskinned.)

## Post-merge cleanup (not a branch task)

The four exploration SVGs in the repo root (`research-crystal-*.svg`) are
untracked scratch in the main checkout — delete them manually after merge:
```bash
rm -f research-crystal-icon-currentColor.svg research-crystal-icon-dark.svg \
      research-crystal-icon-light.svg research-crystal-mark-currentColor.svg
```

---

## Self-Review

**1. Spec coverage (spec §6 Phase 1 + §3/§4/§5/§8):**
- MantineProvider + dark teal theme object → Task 1 ✅
- Install Codicons + the CM VS Code theme → Task 1 deps ✅ (CM theme installed; wiring is P3, per spec phasing)
- AppShell (title/activity/main/status) with activity-bar nav → Tasks 2–4 ✅ (sidebar region deferred to P2 with its content — documented in Scope notes)
- Themed primitives used app-wide → the theme makes all Mantine components themed (Task 1); IDE-specific primitives (list/tree row) + Select migration land with consumers in P2/P3 — documented ✅
- Base typography/reset → `theme.css` body + Mantine's `styles.css` reset (Task 1) ✅
- Fold in the favicon → Task 2 (research-crystal) ✅
- Existing routes render inside the shell → Task 4 layout route ✅
- Per-surface muted / contrast (§8) → `--app-muted` vs `--app-muted-chrome` in `theme.css` (Task 1) ✅
- Activity-bar SPA-native vs Jinja deep-link; References excluded (§5, Codex fix) → Task 3 ✅
- SPA owns only `/`,`/cockpit`; drift guard green → Task 4 ✅

**2. Placeholder scan:** No TBD/TODO/"handle errors"/"similar to". Every code step has complete file contents. ✅

**3. Type consistency:** `theme` (Task 1) consumed by `test-utils`, `App`, `AppShell.test`. `renderWithProviders(ui, {route?})` signature consistent across Tasks 2–3 tests. `ActivityItem`/`ACTIVITY_TOP`/`ACTIVITY_BOTTOM` defined in Task 3 nav-items, consumed in the same task's ActivityBar. `BrandMark({size})`, `TitleBar`, `StatusBar`, `ActivityBar`, `AppShell` — all no-arg (except BrandMark) and used consistently. Codicon class pattern `codicon codicon-${icon}` consistent. ✅

**Note for the executor:** Step 6 of Task 4 is a soft fail-first checkpoint (the guard may already pass against the flat router); the binding assertion is Step 8 against the nested shape. Proceed through Step 7 regardless.
