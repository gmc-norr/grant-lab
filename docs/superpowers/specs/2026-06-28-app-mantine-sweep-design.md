# App-Wide Mantine Sweep — Design Spec

**Date:** 2026-06-28
**Status:** Draft (awaiting user review)

## Problem

The cockpit is now 100% Mantine + one scoped module (the 2a/2b/2c sweep). The **non-cockpit** content surfaces are still hand-rolled: raw `<table>`/`<form>`/`<input>`/`<button>` markup styled by per-route `*.module.css` (with `--app-*` tokens). Only `CallsFilters` and `WorkspacesFilters` use Mantine today. This is the open part of UI workstream-1 ("Mantine consistency — no hand-rolled primitives").

## Goal & consistency bar

Same bar as the cockpit sweep ([[project_cockpit_mantine_2a_merged]], [[project_gui_design_system]]):

- **Mantine primitive for every UI element** — `Table`, `TextInput`, `PasswordInput`, `NativeSelect`/`Select`, `Textarea`, `Button`, `Card`, `Badge`, `Alert`, `Tabs`, `Group`/`Stack`, `Pagination`, etc.
- **Scoped CSS Modules with `--app-*` tokens ONLY for irreducible visuals** that Mantine props can't express (banded colours, special surfaces). No top-level bare element selectors — descendants under a hashed class are fine.
- **No hand-rolled element-wrapper components** (the thin `Button`/`Card`/`Select` wrappers go away).
- **Dark teal theme via the existing `theme`** — Mantine components inherit it; no per-component colour overrides unless irreducible.

This is a re-skin: **no behaviour, data-flow, API, or copy changes.** Pure frontend; no schema, no OpenAPI regen.

## Scope

### In scope (all non-cockpit content surfaces)
- **Auth:** `Login`, `Register`, `PreAuthLayout`, `AccountPending`, `AccountNoAccess`, `AuthError` (+ `PreAuth.module.css`).
- **Account:** `Settings`.
- **Profiles:** `ProfilesList`, `ProfileDetail`, `ProfileNew`, `ProfileEdit`, `StructuredFields` (the shared structured-profile form).
- **Admin:** the five content routes — `AdminDashboard`, `AdminDrafting`, `AdminGoldenSet`, `AdminUsers`, `AdminPendingUsers` (all seven tables + forms/actions/banners; five content modules retired). **`AdminLayout`/`AdminNav` are NOT in scope** — nav chrome handled like the IDE shell (see Out of scope); `AdminNav.module.css` is a sanctioned remaining scoped module.
- **Dashboard / Calls / Workspaces:** `Dashboard`, `DashboardHeader`, `Deadline`, `CallRow`, `CallDetail`, `WorkspaceList`, `WorkspaceCreate`.
- **Sidebar filters (partial):** `CallsFilters`, `WorkspacesFilters` — their *controls* are already Mantine (`Select`/`TextInput`/`Switch`), but each still has a hand-rolled `<div className={styles.filters}>` wrapper + `<h2 className={styles.heading}>` (and CallsFilters a raw `<p role="alert">` error) + a CSS module. Convert just the wrapper/heading/error to Mantine (`Stack`/`Box`, `Title`/`Text`, `Text`/`Alert`) and retire the modules. Folded into Slice 5.
- **Shared fallbacks:** `RouteSpinner` (`router.tsx`) and the soft-404 (`router.tsx`) are raw `<main>` markup rendered INSIDE the providers → convert to Mantine (`Center`/`Loader`, `Alert`/`Title`). Folded into Slice 5 to make the "app-wide non-cockpit" claim crisp.
- **Shared components:**
  - `Button.tsx`, `Card.tsx`, `Select.tsx` — **DEAD CODE (zero consumers)** → delete the components, their `.module.css`, and `Button.test.tsx`.
  - `Pagination.tsx` — consumed only by `Dashboard`. Its contract is **prev/next** (`hasPrev`/`hasNext`/`Page N`), and the calls API returns an item `total` + `limit` (not page count). **Default: convert to a `Group` of Mantine `Button`s** to preserve the exact prev/next contract; if a numbered Mantine `Pagination` is chosen at plan time, derive `total={Math.ceil(itemTotal / limit)}`. Delete the module.

### Out of scope
- **IDE shell chrome** — `AppShell`, `ActivityBar`, `Sidebar`, `StatusBar`, `TitleBar`. Deliberate custom IDE layout (flex/grid), already at the bar (Mantine where applicable + scoped `--app-*` modules). Reworking into Mantine `AppShell` is churn for little gain. Untouched (also just gained the resizable-panes `PanelGroup`).
- **`AdminNav` + `AdminLayout`** — the admin subnav is a router-`NavLink` active-state nav (the same active-`className`-function pattern as the IDE shell, which Mantine's controlled `className` fights, for no test or UX gain). `AdminLayout` has no styled UI (just `RequireAdmin` + `AdminNav` + `Outlet`). Kept as-is; **`AdminNav.module.css` deliberately remains** — the one scoped admin module that survives Slice 4. Decided at Slice 4 plan time (Codex-reviewed). A small Mantine-ification of `AdminNav` is a possible later follow-up.
- **`ErrorBoundary`** — **deliberate exception.** It WRAPS `MantineProvider` in `App.tsx` (so a catastrophic error, including one in the theme/provider itself, still renders a fallback). Its fallback therefore renders with NO Mantine context and CANNOT use Mantine components without throwing. Keep its fallback framework-free (raw `<main>`/`<h1>`/`<button>`). Moving it inside the provider to "Mantine-ify" it would defeat its last-resort purpose — explicitly not doing that.
- **`MatchBadge`** — already a clean banded pill (`data-band`, `--app-*`), the idiomatic domain-badge pattern (cf. cockpit `BandBadge`). Keep.
- **Cockpit** — done.

## Decomposition

One independently-mergeable slice per cluster (mirrors the cockpit's 2a/2b/2c). Each slice: plan → Codex review → execute → independent verify (tsc + full vitest + build [+ visual harness where useful]) → local `--no-ff` merge → rebuild & restage the gitignored bundle → branch cleanup. Sequence is smallest-/simplest-first so the form/table conversion patterns established early are reused later.

1. **Slice 1 — Auth.** Login/Register forms → `TextInput`/`PasswordInput`/`Button` (+ Mantine form error/`Alert`); the static status pages → `Text`/`Alert`/`Card`. Self-contained (PreAuth layout, no shell). **Also deletes the dead `Button`/`Card`/`Select` components + CSS + `Button.test.tsx`** (trivial, gets them out of the way and proves the teardown-grep gate early). Establishes the Mantine form vocabulary for later slices.
2. **Slice 2 — Account/Settings.** The name/password/digest forms → Mantine.
3. **Slice 3 — Profiles.** `StructuredFields` (the large shared select/textarea/input form) → Mantine; `ProfilesList`/`ProfileDetail` → `Card`/`Stack`; `ProfileNew`/`ProfileEdit` wire the Mantine `StructuredFields`.
4. **Slice 4 — Admin.** **All seven raw `<table>`s** → Mantine `Table` (AdminDashboard ×1, AdminDrafting ×3, AdminGoldenSet ×2, AdminUsers ×1); admin forms/actions/banners → Mantine. The largest table cluster.
5. **Slice 5 — Dashboard / Calls / Workspaces + shared closers.** Lists/cards/detail/buttons → Mantine; convert `Pagination` (Group-of-Buttons, see above) and delete the wrapper; convert the `CallsFilters`/`WorkspacesFilters` wrapper/heading/error; convert the shared `RouteSpinner` + soft-404 fallbacks. Closes the sweep (after which the only non-cockpit raw UI left is the deliberately-excepted `ErrorBoundary` plus the scoped nav-chrome modules — the IDE shell and `AdminNav` — so the final "no modules left" check expects exactly those, not zero).

## Conventions to reuse (from the cockpit sweep)

- **Stable `data-*` test hooks** survive class hashing (`data-band`, `data-region`, etc.) — prefer them over `toHaveClass`.
- **List semantics:** `Stack component="ul"` + `component="li"` where a `<ul>/<li>` contract is asserted.
- **Mantine `Tabs`** keeps `role="tab"` + `aria-selected`; `aria-label` goes on `Tabs.List`.
- **No top-level bare element selectors** in `.module.css` (scope under a hashed class).
- **`tsc` is the type gate** (no TS ESLint; `noUnusedLocals` — import only what you use). Whole-suite `vitest` is reliable here (only backend testcontainers flakes; this sweep changes no backend).
- **Per-slice teardown grep** confirms removed classes/components are unreferenced before deleting files (cf. cockpit 2c `cockpit.css` deletion).
- **Restage the bundle after each local merge** (`pnpm build` → copy `dist/*` into the gitignored `src/call_manager/web/spa/`).

## Risks / notes

- **Mantine `Select` is a custom combobox, NOT a native `<select>`.** Several profile forms use `<select>` and their tests likely drive it with `userEvent.selectOptions` / `getByRole("combobox")`. Use **Mantine `NativeSelect`** (renders a real `<select>`) wherever a test relies on native-select semantics, so the DOM contract and tests survive. Only use the richer `Select` where there's no such dependency and the UX warrants it. The plan for each slice must read the route's test first and choose accordingly.
- **Form label association:** Mantine `TextInput`/`PasswordInput`/`Textarea` render a real `<input>`/`<textarea>` with a `label` prop that associates correctly, so `getByLabelText` keeps working — but verify each converted form's existing label queries.
- **Mantine `Table`** renders a real `<table>`/`<thead>`/`<tbody>`/`<tr>`/`<th>`/`<td>`, so `getByRole("table"/"row"/"cell"/"columnheader")` and text queries survive; assert against roles/text, not hashed classes.
- **Test preservation is the gate** — every existing route test must still pass (adjust queries to the new tree without weakening them); add structural assertions only where they strengthen coverage.
- **No API/schema change** → no OpenAPI regen anywhere in this sweep.

## Verification (per slice)
1. `pnpm exec tsc --noEmit` clean.
2. `pnpm test -- --run` full green.
3. `pnpm build` builds.
4. Teardown grep: removed classes/components unreferenced.
5. Visual: the backendless Playwright route-mock harness ([[reference_spa_visual_check]]) for the converted surface where layout matters.
6. Independent diff-vs-parity review before merge.
