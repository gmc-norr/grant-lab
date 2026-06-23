# SPA Redesign — VS Code-style UI on Mantine — Design

**Status:** design approved (look signed off via mockup) 2026-06-23
**Visual reference:** `docs/superpowers/specs/2026-06-23-spa-vscode-redesign-mockup.html` (static HTML mockup of both surfaces, approved)

## 0. Summary

Redesign the **React SPA** (the funding-calls dashboard + the proposal cockpit) into a
cohesive **VS Code-style** application: one persistent IDE shell (activity bar, sidebar,
editor tabs, bottom panel, status bar), a **dark-first** VS Code theme, built on the
**Mantine v9** component library plus **Codicons** (VS Code's icon font) and a VS Code
**CodeMirror** theme. This is a deliberate new visual direction — we are **not** matching
the existing calm-clinical look; the old `--cm-*` teal system is superseded for the SPA.

The work is the whole SPA, executed in three independently-shippable phases:
**① Foundation → ② Dashboard → ③ Cockpit.** Light theme, a command palette, and restyling
the server-rendered **Jinja pages** (login/profile/admin) are explicit follow-ons.

## 1. Current state (why)

- The Jinja pages are polished (the `app.css` calm-clinical system), but the **React SPA is
  essentially unstyled** — default serif type, native form controls, bare links, no app
  chrome. (Baseline screenshots captured 2026-06-23.)
- The SPA already carries design **tokens** (`frontend/src/styles/tokens.css`, the `--cm-*`
  palette) and a partial `cockpit.css`, but lacks the layers that *use* them: base
  typography, an app shell, and styled components.
- Rather than finish the calm-clinical look, we are changing direction to a VS Code
  aesthetic — a natural fit, because the cockpit is **already architected like VS Code**
  (CodeMirror editor center, outline rail = sidebar, Inbox/Problems/Activity = panel,
  Draft/Spec/Scorecard = editor tabs).

## 2. Decisions (locked)

| Decision | Choice |
|---|---|
| Visual language | **VS Code** (new direction; calm-clinical superseded for the SPA) |
| Component kit | **Mantine v9** (CSS-modules, no CSS-in-JS runtime, 120+ components, CSS-variable theming) |
| Styling paradigm | Mantine theme + CSS modules + plain CSS. **No Tailwind**, no second UI kit |
| Icons | **`@vscode/codicons`** (VS Code's official icon font; CC-BY-4.0) |
| Editor theme | **`@uiw/codemirror-theme-vscode`** (ships dark + light; matches the cockpit's CodeMirror) |
| Color scheme | **Dark-first**; light is a fast-follow (Mantine + the CM theme + the palette already support both) |
| IDE chrome | Visual style everywhere **+ core chrome**: activity bar, sidebar, editor tabs, bottom panel, status bar, slim title bar. **Command palette deferred** |
| Shell model | **One unified shell** wrapping the whole SPA; the activity bar switches top-level views. v1 SPA-native views are **Calls** (`/`) and the **active Workspace / cockpit** (`/cockpit/:id`) — the only routes the SPA owns today; other activity-bar items deep-link to existing Jinja pages until migrated |
| Scope | Whole SPA, phased Foundation → Dashboard → Cockpit. **Jinja pages = separate follow-on** (restyle or migrate) |

## 3. Tooling & dependencies

New frontend deps (pnpm): `@mantine/core`, `@mantine/hooks`, `@vscode/codicons`,
`@uiw/codemirror-theme-vscode`. No backend changes.

**Bundle note:** the SPA JS is already ~818 kB. Mantine is tree-shakeable (import only used
components) and ships no CSS-in-JS runtime; Codicons is a font; the CM theme is small. We
will **measure bundle size before/after each phase** and prefer per-component imports.
Code-splitting the cockpit route from the dashboard route is a candidate optimization
(noted, not required).

## 4. Theme system

A single **Mantine theme object** is the source of truth, encoding the VS Code Dark palette
as CSS variables. The legacy `tokens.css` `--cm-*` variables are **retired for the SPA** (or
remapped onto the new theme where `cockpit.css` still references them, until those rules are
rewritten in Phase 3). Reference palette (VS Code Dark+):

| Role | Value |
|---|---|
| editor background | `#1e1e1e` |
| sidebar background | `#252526` |
| activity bar | `#333333` |
| tab bar / inactive tab | `#2d2d2d` |
| title bar | `#3c3c3c` |
| status bar | `#007acc` (accent) |
| primary accent | `#007acc` |
| link | `#4daafc` |
| text / muted (editor) / muted (chrome) | `#cccccc` / `#858585` / ~`#9d9d9d` (see §8) |
| list hover / selected | `#2a2d2e` / `#094771` |
| good / warn / bad | `#4ec9b0` / `#cca700` / `#f14c4c` |
| editor heading / string | `#569cd6` / `#ce9178` |

- **Type:** UI font `-apple-system, "Segoe UI", system-ui, …`; editor/monospace
  `"Cascadia Code", "Consolas", ui-monospace, …`. Dense IDE spacing; small radii (~4px).
- **Codicons** replace the mockup's placeholder glyphs (activity bar, tree rows, tabs, panel,
  status bar).
- **Favicon/identity:** a VS Code-dark favicon has already been prototyped
  (`src/call_manager/web/static/favicon.svg` + `frontend/index.html` link); fold it in.

## 5. The unified shell (information architecture)

One persistent layout wraps every SPA route. Regions (see mockup):

```
┌──┬─────────┬──────────────────────────────┐
│A │ SIDEBAR │ EDITOR TABS                   │   A = activity bar (48px)
│c │(context)│ ──────────────────────────── │   sidebar ~256px
│t │         │ editor / list (per view)     │
│ │          ├──────────────────────────────┤
│bar│        │ PANEL (cockpit only)         │   panel ~210px
├──┴─────────┴──────────────────────────────┤
│ STATUS BAR (#007acc)                       │   status bar 22px
└────────────────────────────────────────────┘   + slim title bar 30px on top
```

- **Title bar** (30px): app name + active-document/breadcrumb title.
- **Activity bar** (48px): top-level destinations. **SPA-native in v1** — **Calls**
  (dashboard `/`, Phase 2) and the **active Workspace / cockpit** (`/cockpit/:id`, Phase 3).
  **Deep-links to existing pages until migrated** — **Workspaces** (the server-rendered
  workspace list at `/workspaces`), plus **Profile** + **Settings** pinned at the bottom.
  **References is *not* a top-level destination** — it is a per-workspace concern and lives
  in the cockpit sidebar (below). Badges (e.g. workspace count) supported. Active item:
  white icon + left accent border. (Today the SPA owns only `/` and `/cockpit`
  — see [routes.json](../../../frontend/routes.json); the activity bar must visually
  distinguish SPA-native views from Jinja deep-links and must not imply SPA routes that
  don't exist yet.)
- **Sidebar** (contextual to the active activity-bar item):
  - *Calls view* → filters (Profile / Source / Sort + count chips + saved views).
  - *Workspace view* → the proposal **Outline** (section list, active section highlighted)
    + Workspace groups (References / Criteria / Versions / Members) with counts.
- **Editor area + tabs:**
  - *Calls view* → the funding-calls list (link-blue titles, match badges, color-coded
    deadlines), with a breadcrumb. **A call title keeps its current behavior in v1 — a plain
    link to the server-rendered call-detail page (`/calls/{id}`), where a workspace is
    started** ([Dashboard.tsx](../../../frontend/src/routes/Dashboard.tsx)). Opening a
    workspace directly from the list into the cockpit is a future enhancement, out of
    Phase 2 scope (this redesign makes no backend/routing change).
  - *Workspace view* → editor tabs **Draft · Spec · Scorecard · Panel review**; the body is
    the CodeMirror editor (VS Code theme) with a line gutter, or the Scorecard/Review views.
- **Bottom panel** (cockpit only): **Inbox · Problems · Activity** tabs (the SP2b-3b region,
  now VS Code-styled), with a count/severity badge on Problems and a panel toolbar.
- **Status bar** (22px, `#007acc`): workspace context — branch-like indicator, deadline,
  spec-lock state, scorecard band, save state, `Ln/Col`, language mode.

**React architecture:** a single `AppShell`-style layout component (Mantine `AppShell` or a
bespoke grid) renders the title bar / activity bar / sidebar / main / status bar and hosts
the routed view. The dashboard (`/`) and cockpit (`/cockpit/:id`) become **views inside the
shell**, selecting their sidebar + main content from the active route. Existing routing
(react-router) and data hooks (TanStack Query) are preserved; only presentation changes.

## 6. Phasing

**Phase 1 — Foundation** (shared, no visible surface "done" but everything downstream depends on it):
MantineProvider + the VS Code dark theme object; install Codicons + the CM VS Code theme;
the `AppShell` shell (title/activity/sidebar/main/status regions) with the activity-bar
nav; a small set of themed primitives/wrappers used app-wide (Button, Select, Tabs, Card,
Badge/pill, list row, tree row); base typography/reset; fold in the favicon. Existing routes
render *inside* the shell (even if their bodies are not yet reskinned).

**Phase 2 — Dashboard** onto the shell: filters → sidebar, the calls list reskinned
(link titles, match badges, deadline coloring), breadcrumb, counts. Replace native selects
with themed Mantine `Select`s. Call titles keep their existing `/calls/{id}` link (no
workspace-open behavior added). The **Workspaces** activity-bar item deep-links to the
existing `/workspaces` Jinja page in this phase.

**Phase 3 — Cockpit** onto the shell (the largest): editor tabs, the outline sidebar, the
CodeMirror VS Code theme wired into the existing editor, the bottom panel (Inbox/Problems/
Activity) reskinned, the Scorecard band pills mapped to the theme, the status bar populated.
Rewrite/retire the `cockpit.css` rules in favor of the theme + component styles.

Each phase ends green (tsc + vitest) and is independently mergeable.

## 7. Migration approach (re-skin, don't rewrite)

- Preserve **behavior and component contracts** — this is a presentation change. Existing
  vitest tests assert behavior/text/roles; keep them green. Where a test asserts a specific
  class name or DOM shape that the reskin changes, update the test deliberately (noted per
  task in the plan), never loosen an assertion to hide a regression.
- Absorb the existing `cockpit.css` / CSS-modules into the new theme/components incrementally
  (Phase 3 owns the cockpit ones); avoid a flag-day rewrite.
- No backend/API changes; no schema changes.

## 8. Accessibility

Mantine primitives are accessible by default (focus management, ARIA). Maintain: visible
focus rings (VS Code uses a clear focus outline); and **per-surface contrast**. Body text
`#cccccc` clears AA (≥ 4.5:1) on every surface, but the muted `#858585` clears 4.5:1 **only
on the editor `#1e1e1e`** (~4.5:1) and **falls below AA on the darker chrome** — `#252526`
(~4.2:1), `#333333` (~3.4:1), `#3c3c3c` (~3.0:1). Therefore the **Foundation phase defines a
per-surface muted token** and bumps muted text lighter (toward ~`#9d9d9d`) on the
sidebar / activity-bar / title surfaces, verifying each muted/background pair ≥ 4.5:1; reserve
`#858585` for the editor surface only. Also maintain keyboard navigation of the activity bar /
tabs / tree, and `role`/`aria-selected` on tabs (already present). Light theme (fast-follow)
will be the high-contrast daytime option.

## 9. Out of scope / deferred

- **Light theme** — immediate fast-follow after dark ships (theme + CM light variant exist).
- **Command palette (⌘K)** — a feature, not just a look; later.
- **Jinja pages** (login/profile/admin, and any legacy proposal-assist) — a separate
  initiative: either restyle `app.css` to a matching dark theme or migrate into the SPA.
- **Split editor views**, multi-workspace tabs.
- Bundle code-splitting beyond per-component imports (optimize if measurements demand).

## 10. Testing & validation

- **Gate:** `pnpm exec tsc --noEmit` (no TS linter — tsc is the gate) + `pnpm vitest run`,
  green after every task.
- **Visual QA:** the approved mockup is the target; re-run the headless-screenshot harness
  per phase (dashboard, cockpit) and compare against the mockup.
- **Behavior:** existing component tests stay green; new shell components get render/interaction
  tests (activity-bar switching, tab selection, status-bar content).
- No backend test impact (no backend changes).
