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
- **Admin:** `AdminDashboard`, `AdminDrafting`, `AdminGoldenSet`, `AdminUsers`, `AdminPendingUsers` (+ `AdminLayout`, `AdminNav`).
- **Dashboard / Calls / Workspaces:** `Dashboard`, `DashboardHeader`, `Deadline`, `CallRow`, `CallDetail`, `WorkspaceList`, `WorkspaceCreate`. (`CallsFilters`/`WorkspacesFilters` already Mantine — leave.)
- **Shared components:**
  - `Button.tsx`, `Card.tsx`, `Select.tsx` — **DEAD CODE (zero consumers)** → delete the components, their `.module.css`, and `Button.test.tsx`.
  - `Pagination.tsx` — consumed only by `Dashboard` → convert to Mantine `Pagination` (or a `Group` of `Button`s), delete the module.

### Out of scope
- **IDE shell chrome** — `AppShell`, `ActivityBar`, `Sidebar`, `StatusBar`, `TitleBar`. Deliberate custom IDE layout (flex/grid), already at the bar (Mantine where applicable + scoped `--app-*` modules). Reworking into Mantine `AppShell` is churn for little gain. Untouched (also just gained the resizable-panes `PanelGroup`).
- **`MatchBadge`** — already a clean banded pill (`data-band`, `--app-*`), the idiomatic domain-badge pattern (cf. cockpit `BandBadge`). Keep.
- **Cockpit** — done.

## Decomposition

One independently-mergeable slice per cluster (mirrors the cockpit's 2a/2b/2c). Each slice: plan → Codex review → execute → independent verify (tsc + full vitest + build [+ visual harness where useful]) → local `--no-ff` merge → rebuild & restage the gitignored bundle → branch cleanup. Sequence is smallest-/simplest-first so the form/table conversion patterns established early are reused later.

1. **Slice 1 — Auth.** Login/Register forms → `TextInput`/`PasswordInput`/`Button` (+ Mantine form error/`Alert`); the static status pages → `Text`/`Alert`/`Card`. Self-contained (PreAuth layout, no shell). **Also deletes the dead `Button`/`Card`/`Select` components + CSS + `Button.test.tsx`** (trivial, gets them out of the way and proves the teardown-grep gate early). Establishes the Mantine form vocabulary for later slices.
2. **Slice 2 — Account/Settings.** The name/password/digest forms → Mantine.
3. **Slice 3 — Profiles.** `StructuredFields` (the large shared select/textarea/input form) → Mantine; `ProfilesList`/`ProfileDetail` → `Card`/`Stack`; `ProfileNew`/`ProfileEdit` wire the Mantine `StructuredFields`.
4. **Slice 4 — Admin.** Five raw `<table>`s → Mantine `Table`; admin forms/actions/banners → Mantine. The largest table cluster.
5. **Slice 5 — Dashboard / Calls / Workspaces.** Lists/cards/detail/buttons → Mantine; convert `Pagination` to Mantine and delete the wrapper. Closes the sweep.

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
