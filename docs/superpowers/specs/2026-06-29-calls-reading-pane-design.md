# Funding Calls — Outlook-style Reading Pane

**Date:** 2026-06-29
**Status:** Design approved; ready for implementation plan.

## Goal

Give the Funding calls page an Outlook-style layout: the calls list, header, and
filters stay in the center pane, and clicking a call opens its full detail in a
**right reading pane** — without leaving the list. The standalone full-page call
view (`/calls/:id`) is kept for deep links (digest emails, shared URLs) and a
roomier read, rendered from the *same* detail UI so the two never drift.

## Context (current state)

- The calls list is the index route — `Dashboard` ([frontend/src/routes/Dashboard.tsx](../../../frontend/src/routes/Dashboard.tsx))
  — with `CallsFilters` as its left sidebar (via the route `handle.Sidebar`
  mechanism). Rows are `CallRow` ([frontend/src/routes/CallRow.tsx](../../../frontend/src/routes/CallRow.tsx)),
  whose title currently links to `/calls/:id`.
- The full call detail already exists as `CallDetail` ([frontend/src/routes/CallDetail.tsx](../../../frontend/src/routes/CallDetail.tsx))
  at `/calls/:id`, fed by `useCallDetail(callId, profile)` which returns
  `{ call, ranking, criteria, best_profile_id }`.
- The persistent IDE shell ([frontend/src/shell/AppShell.tsx](../../../frontend/src/shell/AppShell.tsx))
  already renders a **collapsible, draggable right pane**: any route opts in by
  declaring `handle: { RightPane: <Component> }`. The cockpit uses it today for
  the chat pane. Sidebar / main / right-pane live in a horizontal
  `react-resizable-panels` group; widths persist via `autoSaveId="cm-shell"`.
- `react-resizable-panels@2.1.9` is already a dependency. No new layout
  primitives are required.

This design therefore reuses existing machinery; the new work is (a) extracting
CallDetail's body into a shared presentational component, (b) a reading-pane
component that renders it for the URL-selected call, and (c) making a row click
set the selection instead of navigating away.

## Approach

**Selection lives in the URL** as a `selected` search param on the index route
(`/?selected=123`), and the reading pane is mounted through the shell's existing
`RightPane` handle. This matches the codebase's established "panes communicate
through the URL" pattern (the cockpit's sidebar/main/chat coordinate via the URL
+ TanStack Query, not a shared store), so no cross-shell prop-drilling or new
global store is needed. Selection is addressable: refresh, browser back/forward,
and link-sharing all restore the same call in the pane.

Two alternatives were rejected: a **nested child route** rendering into the pane
(the shell drives the right pane through `handle`, not a nested `Outlet`, and the
list is an index route — index routes cannot take children — so this is a routing
refactor that fights the current design); and **local React state** (the pane is
rendered by the shell, a sibling of the list, so bridging state across that
boundary needs a shared store, and selection would not survive refresh/share).

## Components

### New — `CallDetailBody.tsx` (presentational)

Everything `CallDetail` currently renders *between* its page chrome: the header
(title, `source_name · programme · external_id`, "Start drafting workspace"
button), the ranking card, analyst notes, the details grid, summary, and the
criterion breakdown.

- **Props:** `{ call, ranking, criteria, best_profile_id }` — exactly the
  non-error payload of `useCallDetail`.
- **No hooks, no routing, no mutations.** Pure render of its props. This is the
  single source of truth shared by the pane and the full page.

### New — `CallReadingPane.tsx` (the `RightPane` component)

- Reads `selected` and `profile` from the URL (`useSearchParams`).
- No `selected` → a quiet placeholder ("Select a call to preview").
- Otherwise `useCallDetail(selectedId, profile)` and render one of:
  loading · error · `not_found` ("Call not found.", list unaffected) ·
  `<CallDetailBody/>`.
- Renders its **own landmark** element `<aside aria-label="Call preview">` (the
  shell mounts RightPane components as-is; they own their landmark), with its own
  vertical scroll region.
- Pane header strip: an **"Open full page ↗"** link to
  `/calls/:selected?profile=<profile>` and a **close (×)** button that removes
  `selected` from the URL (deselect).

### Modify — `CallDetail.tsx` (the `/calls/:id` page)

- Keeps its page chrome: "← All calls", the centered `<main>` width wrapper, and
  the **hide / unhide** controls (with their confirm + undo + navigation).
- Its body is replaced by `<CallDetailBody {...data} />`. Behavior for deep links
  is otherwise unchanged.

### Modify — `CallRow.tsx`

- The title becomes an `Anchor`/`Link` to the **selection URL** (see URL contract
  below) instead of `/calls/:id`. Keeping it a real anchor preserves keyboard
  focus and middle-click-opens-new-tab.
- The row container gets a mouse `onClick` that selects the call (convenience —
  the whole row is clickable, Outlook-style). The accessible interactive elements
  remain the title anchor and the "Start drafting" anchor; the row-level click is
  a progressive-enhancement mouse affordance only, so no nested-button a11y
  violation is introduced.
- "Start drafting" stays a distinct link to `/cockpit/new?…`; its handler calls
  `stopPropagation` so clicking it does not also select the row.
- When `selected === call.id`, the row is visually highlighted and carries
  `aria-current` (e.g. `aria-current="true"`).

### Modify — `router.tsx`

- Add `handle: { RightPane: CallReadingPane, rightPaneLabel: "reading pane" }` to
  the index (Dashboard) route.

### Modify — `shell/AppShell.tsx` + `shell/TitleBar.tsx`

- `AppShell` already finds the deepest matched route's `handle.RightPane`. Extend
  the handle read to also pick up an optional `rightPaneLabel` and pass it to
  `TitleBar`.
- `TitleBar`'s right-pane toggle currently hard-codes `aria-label="Toggle chat
  panel"`. Parameterize it from the route-supplied label so on Calls it reads
  **"Toggle reading pane"**. The label **defaults to "chat panel" when a route
  supplies none**, so the cockpit's existing label (and its tests) are unchanged.

## URL contract

- **Select:** merge `selected=<id>` into the current `URLSearchParams` (do not
  replace) so `profile`, search/score filters, and `page` survive. A helper
  builds the selection target from the current params.
- **Deselect:** remove `selected`.
- **Pane fetch:** `selectedId = Number(selected)`; `profile` is read from the same
  `profile` param the list already uses, so the pane ranks against the dashboard's
  effective profile.
- **Open full page:** `/calls/<selected>?profile=<profile>` (profile omitted when
  absent), i.e. the existing route — unchanged.
- Selection persists across pagination/filter changes (it stays in the URL); the
  selected call need not be on the current page. This is acceptable and simple.

## States & accessibility

- Pane states: placeholder (no selection) · loading · error · `not_found`.
- Selected row carries `aria-current`; the pane is a labeled complementary
  landmark (`<aside aria-label="Call preview">`); the toggle button has a correct
  accessible name per route.
- The pane is collapsible/resizable via the shell's existing controls (TitleBar
  toggle + drag handle); width persists via the shell's `autoSaveId`.

## Scope boundaries (v1)

1. **Hide / unhide stays on the full page only.** Keeps `CallDetailBody` purely
   presentational and avoids an "undo with no row to return to" flow in the pane.
   Additive later.
2. **Calls list only.** The Workspaces list may copy the pattern later; out of
   scope here.
3. **The reading pane shares the cockpit chat pane's remembered width** — both use
   the shell's single `rightpane` panel, giving a consistent right-pane width
   app-wide. Making them persist independently is a small future shell change.
4. **No keyboard list navigation (j/k / arrows) in v1.** An additive Outlook touch
   for later.

## Testing

- `CallDetailBody`: renders with and without `ranking`; with `criteria`; with
  analyst notes; "Start drafting" button present with correct href.
- `CallReadingPane`: placeholder when no `selected`; renders `CallDetailBody` for
  `?selected=<id>` (mocked `useCallDetail`); loading / error / `not_found`
  branches; "Open full page ↗" points at `/calls/<id>`; close removes `selected`.
- `CallRow`: clicking the title sets `selected` while preserving other params;
  highlights / `aria-current` when active; "Start drafting" still links to
  `/cockpit/new?…` and does not select.
- `CallDetail` page: regression — still renders via the shared body; hide/unhide
  intact; deep link to `/calls/:id` works.
- Shell/TitleBar: the toggle's accessible name reflects the route's
  `rightPaneLabel`; the cockpit's "Toggle chat panel" label is unchanged.

## Out of scope / future

Hide-in-pane, the same pattern on the Workspaces list, independent right-pane
width persistence, and keyboard list navigation are all explicitly deferred.
