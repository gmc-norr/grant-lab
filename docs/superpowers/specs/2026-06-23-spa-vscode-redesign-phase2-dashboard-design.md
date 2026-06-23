# SPA Redesign — Phase 2 (Dashboard) Design Spec

**Goal:** Re-skin the Calls dashboard (`/`) onto the Phase-1 IDE shell — move the filters into a real sidebar region, restyle the calls list with banded match pills and themed deadline coloring, add a breadcrumb + summary-counts header strip — without changing any backend, API, DB, or route behavior. Pure frontend presentation on top of the unchanged `/api/calls` read-model.

**Builds on:** the merged Phase-1 foundation (Mantine v9 + custom dark teal theme + `AppShell` shell), `docs/superpowers/specs/2026-06-23-spa-vscode-redesign-design.md` (overall redesign, §6 Phase 2). This spec details that one-paragraph Phase-2 sketch.

## 1. Current state

`frontend/src/routes/Dashboard.tsx` is a single `<main>`: a `<header>` with the sign-out form + four **native** controls (Profile/Source/Sort selects, Show-closed checkbox), a summary-counts `<section>`, ranking-status lines, a `<ul>` of call rows (title→`/calls/{id}`, source, color-coded `<Deadline>`, rounded score), and `<Pagination>`. The `AppShell` grid is `title (30px) / body (activity-bar + main) / status (22px)` — **no sidebar column**. Filter/data plumbing lives in `frontend/src/api/dashboard.ts` (`useDashboard`, `useSetDefaultProfile`).

The `/api/calls` read-model response (unchanged by this phase):
- `effective: { profile_id, source }` — server-resolved filter state (an unknown/inactive id degrades to null / "all").
- `facets: { profiles: [{id,name}], sources: [{slug,name}] }`
- `summary: { open_count, high_score, soon_deadline }`
- `ranking: { pending, failed }`
- `items: [{ id, title, source_name, deadline, aggregate_score }]`
- `pagination: { page, has_prev, has_next }`

## 2. Decisions (locked in brainstorm)

1. **Sidebar = a generic, reusable shell region** (not Dashboard-local), so Phase 3's cockpit outline reuses it.
2. **Breadcrumb + the 3 summary counts** live in a **main-area header strip**; the status bar stays minimal until Phase 3 populates it.
3. **Sign-out stays in the dashboard**, reskinned into the right edge of the header strip — no change to the locked Phase-1 shell.
4. **Match score → a banded pill** (`MatchBadge`): strong ≥ 80, good 60–79, < 60 dim.

## 3. Architecture — sidebar as a route-driven shell region

`AppShell` gains a **sidebar column** in its grid body (`activity-bar | sidebar | main`). The sidebar column is rendered **only when the active route declares sidebar content**, so the Cockpit (Phase 3) and the `*` 404 route render full-width with no empty rail.

**Mechanism: React Router route `handle`.** The Calls (index) route declares `handle: { Sidebar: CallsFilters }`. `AppShell` calls `useMatches()`, finds the deepest match whose `handle?.Sidebar` is set, and renders `<Sidebar><handle.Sidebar /></Sidebar>` in the sidebar region (and omits the column when none is found). `handle.Sidebar` is a **component reference**, not an element, so it reads its own hooks.

**Shared state, zero plumbing across the shell boundary.** `CallsFilters` (sidebar) and the Dashboard list both call the *same* `useDashboard(params)` query and `useSearchParams()`. React Query dedupes by query key and the URL is the single source of filter truth, so the sidebar and list stay in sync **without** threading props/context through `AppShell`.

*Alternative considered & rejected:* a `SidebarContext` where the route body registers content via an effect — more moving parts and effect-timing fragility for no benefit on the data router.

## 4. Components

| Component | Responsibility | Notes |
|---|---|---|
| `AppShell` (modify) | Add the sidebar grid column + a `useMatches`-driven `<Sidebar>` region; omit the column when no `handle.Sidebar`. | Grid: body `activity-bar | sidebar? | main`. |
| `Sidebar` (new) | Thin shell wrapper: sidebar chrome (bg `--app-sidebar`, right border, scroll) around arbitrary route-supplied content. | Presentational only. |
| `CallsFilters` (new) | The Profile/Source/Sort/Show-closed controls; sidebar-resident; reads `useDashboard`+`useSearchParams`; declared via the Calls route `handle`. | Owns the filter logic moved out of `Dashboard`. |
| `DashboardHeader` (new) | Header strip: breadcrumb ("Calls") · the 3 counts · Sign-out (right). | Counts from `summary`. |
| `CallRow` (new) | One call row: title link + muted meta (`source · deadline`) + `MatchBadge`. The list-row primitive (spec deferred it to its first consumer = here). | |
| `MatchBadge` (new) | Banded score pill from `aggregate_score`. | Thresholds in §5. |
| `Deadline` (modify) | Reskin to theme colors; keep the existing state logic. | Thresholds in §5. |
| `Dashboard.tsx` (modify) | Shrinks to: `<DashboardHeader>` + ranking-status lines + the `CallRow` list + `<Pagination>`. Filters move to `CallsFilters`. | Index route gains `handle: { Sidebar: CallsFilters }`. |

## 5. Visual spec (exact values, from the Phase-1 palette §4)

```
+----+----------------+-------------------------------------+
| (title bar: brand          Funding calls )                |
+----+----------------+-------------------------------------+
| Q  | FILTERS        | Calls  12 open · 5 top · 3 soon  [Sign out]
| F  | Profile   [v]  | ----------------------------------- |
|    | Source    [v]  | Funding call title          [ 82 ] | strong
|    | Sort      [v]  |   Vinnova · closes 9d             |  (deadline: bad)
|    | [ ] Show closed| Another call title          [ 64 ] | good
| P  |                |   Formas · closes 24d            |  (deadline: warn)
| S  |                | Lower-fit call              [ 41 ] | dim
+----+----------------+-------------------------------------+
| Call Manager   (status bar minimal — Phase 3 populates)   |
+-----------------------------------------------------------+
```

- **Title link:** link-teal `#2dd4bf`.
- **`MatchBadge` bands** (frontend threshold map on `aggregate_score`, rounded): all bands share one pill surface (a subtle tinted chip, e.g. `--app-hover`) and differ by **foreground/accent color** — **strong ≥ 80** → good `#4ec9b0`; **good 60–79** → muted `#969ca6`; **< 60** → dim `#6e7681`. A null score renders no badge.
- **`Deadline` states → color** (keep current `deadlineView` logic): **closed** (past) → dim `#6e7681` ("Closed · date"); **soon ≤ 14d** → bad `#f14c4c`; **watch ≤ 30d** → warn `#cca700`; **open > 30d** → muted `#969ca6`.
- **Sidebar:** bg `--app-sidebar` `#202327`, right border `--app-border` `#2c2f35`.

## 6. Behavior contracts preserved exactly (re-skin, not rewrite)

- Filter state is **read-only in the URL**; only an explicit Profile selection persists a default (`POST /api/me/default-profile`), **awaited first**, then reflected via a **functional** `setSearchParams` updater (never clobbers a Source/Sort/Show-closed change made while the POST is in flight).
- Selects **bind to server-resolved** `effective.profile_id` / `effective.source`, not the raw URL params (an unknown/inactive value degrades server-side and the control must reflect that).
- `useDashboard` poll-while-`ranking.pending` + drain-refetch on pending→0, `<Pagination>`, and the `unwrap` 401→`/login` redirect are **unchanged**.
- Call titles remain plain `/calls/{id}` anchors (coexistence with the un-ported Jinja detail page) — **no** workspace-open behavior added.

## 7. Mantine `Select` migration caveat

Mantine `Select` is a div-based combobox (not a native `<select>`), rendering its options in a dropdown on open with its own ARIA. Show-closed becomes a Mantine `Switch`. Existing `Dashboard.test.tsx` queries the native controls and will be **rewritten** for the new structure + interaction (open → pick option). Preserve `value`/`onChange` semantics and the empty options ("— No profile —", "All sources").

## 8. Scope / non-goals

- **No backend, API, or DB changes.** `/api/calls` and `POST /api/me/default-profile` are consumed as-is.
- Call-detail `/calls/{id}` stays Jinja (not reskinned).
- **No sidebar collapse/show-hide** — ship the region, not the toggle (a later fast-follow).
- Status bar stays minimal (Phase 3 populates it).
- Activity bar unchanged; **Workspaces** still deep-links to `/workspaces` Jinja.
- No new SPA routes; the `router.test.tsx` drift guard stays green.
- Dark-first; theme tokens only; preserve component contracts.

## 9. Testing & validation

- **New:** `CallsFilters` (Select rendering + URL update + Profile persist-then-reflect + bind-to-`effective`), `CallRow`/`MatchBadge` (band thresholds 80/60, null→no badge), `Deadline` (cutoffs 14/30 → state class), `AppShell` (renders `handle.Sidebar` when present; omits the column when absent).
- **Update:** `Dashboard.test.tsx` for the new DOM + Mantine-Select interaction; `router.test.tsx` stays green (handle added, routes unchanged).
- **Gate every task:** `pnpm exec tsc --noEmit` + `pnpm vitest run` green; `pnpm build` succeeds.
- **Finish:** a dev-mode screenshot (now that `pnpm dev` works) confirming the reskinned dashboard renders inside the shell.
