# SPA Redesign — VS Code-style UI on Mantine — Design

**Status:** design approved (look signed off via mockup) 2026-06-23
**Visual reference:** `docs/superpowers/specs/2026-06-23-spa-vscode-redesign-mockup.html` (static HTML mockup of both surfaces, **teal/slate palette, approved**)

## 0. Summary

Redesign the **React SPA** (the funding-calls dashboard + the proposal cockpit) into a
cohesive **VS Code-style** application: one persistent IDE shell (activity bar, sidebar,
editor tabs, bottom panel, status bar), **dark-first**, built on the **Mantine v9**
component library plus **Codicons** (VS Code's icon font) and a VS Code **CodeMirror**
editor theme. We adopt VS Code's *structure and chrome* but **our own dark palette** — a
teal/cyan accent that carries the hospital's calm-clinical teal heritage forward, on cooler
slate surfaces — so the app reads as our own tool, **not a VS Code clone**. The old *light*
calm-clinical `--cm-*` UI system is superseded for the SPA (we are dark now); its teal hue
lives on as the accent.

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
- Rather than finish the light calm-clinical look, we change direction to a VS Code-style
  dark IDE — a natural fit, because the cockpit is **already architected like VS Code**
  (CodeMirror editor center, outline rail = sidebar, Inbox/Problems/Activity = panel,
  Draft/Spec/Scorecard = editor tabs). We keep VS Code's chrome but recolor to our own
  teal/slate palette, carrying the calm-clinical teal forward as the accent.

## 2. Decisions (locked)

| Decision | Choice |
|---|---|
| Visual language | **VS Code-style IDE shell + our own dark palette** (teal heritage accent on slate surfaces; *not* a VS Code clone). Old *light* calm-clinical UI superseded for the SPA |
| Component kit | **Mantine v9** (CSS-modules, no CSS-in-JS runtime, 120+ components, CSS-variable theming) |
| Styling paradigm | Mantine theme + CSS modules + plain CSS. **No Tailwind**, no second UI kit |
| Icons | **`@vscode/codicons`** (VS Code's official icon font; CC-BY-4.0) |
| Editor theme | **`@uiw/codemirror-theme-vscode`** as the dark base (ships dark + light), **with headings/accents recolored toward the teal palette** (a small theme extension) so the editor coheres with the chrome |
| Color scheme | **Dark-first**, **custom teal/slate palette** (see §4); light is a fast-follow (Mantine + the CM theme + the palette support both) |
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

A single **Mantine theme object** is the source of truth, encoding our **custom dark palette**
as CSS variables. The legacy `tokens.css` `--cm-*` variables are **retired for the SPA** (or
remapped onto the new theme where `cockpit.css` still references them, until those rules are
rewritten in Phase 3). Palette — VS Code-style structure, our teal/slate identity (the
approved teal mockup is the reference):

| Role | Value |
|---|---|
| editor background | `#1a1b1e` |
| sidebar background | `#202327` |
| activity bar | `#2a2d33` |
| tab bar / inactive tab | `#25282d` |
| title bar | `#30343b` |
| status bar | `#0f766e` (heritage teal) |
| primary accent | `#14b8a6` (teal); bright accent `#2dd4bf` |
| link | `#2dd4bf` |
| text / muted / dim | `#cdd2d8` / `#969ca6` / `#6e7681` (per-surface notes in §8) |
| list hover / selected | `#25282e` / `#0d3b38` (teal-tinted) |
| good / warn / bad | `#4ec9b0` / `#cca700` / `#f14c4c` |
| editor heading / string | `#4dc4d9` (cyan) / `#ce9178` |

- **Type:** UI font `-apple-system, "Segoe UI", system-ui, …`; editor/monospace
  `"Cascadia Code", "Consolas", ui-monospace, …`. Dense IDE spacing; small radii (~4px).
- **Codicons** replace the mockup's placeholder glyphs (activity bar, tree rows, tabs, panel,
  status bar).
- **Brand mark / favicon — "research crystal":** a hexagonal knowledge-network with a **teal
  center node** (`#2dd4bf`; metaphor: connected funding intelligence). **Two tiers**, both
  teal/slate on the dark tile: the **full lattice** as the large brand logo (login / sidebar
  header / app icon, ≥48px) and a **simplified radial** reduction as the favicon (≤32px — the
  full lattice muddies in a browser tab). A themeable `currentColor` variant (`--icon-accent` =
  the teal accent) serves inline use and adapts to light/dark. Reference assets:
  `docs/superpowers/specs/assets/research-crystal-{full,favicon}.svg`. **Foundation** finalizes
  them, installs the favicon at `src/call_manager/web/static/favicon.svg` (wired via the
  `frontend/index.html` link) and the brand mark as a static asset; the exploration SVGs in the
  repo root get relocated/removed.

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
focus rings (VS Code uses a clear focus outline); and **per-surface contrast** on the custom
dark palette. Body text `#cdd2d8` clears AA (≥ 4.5:1) on every surface. The muted `#969ca6`
clears AA on the editor `#1a1b1e` (~5.6:1), sidebar `#202327` (~5.0:1) and activity bar
`#2a2d33` (~4.5:1), but only ~4.1:1 on the lightest chrome (title bar `#30343b`); the
**Foundation phase bumps muted toward ~`#9da3ad` on the title-bar surface** and verifies each
muted/background pair ≥ 4.5:1. The dim `#6e7681` (timestamps / tertiary) is reserved for the
dark editor/panel surfaces and treated as incidental text. Also maintain keyboard navigation
of the activity bar / tabs / tree, and `role`/`aria-selected` on tabs (already present). Light
theme (fast-follow)
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
