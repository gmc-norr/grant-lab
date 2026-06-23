# SPA Redesign — Phase 3 (Cockpit) Design Spec

**Goal:** Reconcile the proposal **Cockpit** (`/cockpit/:id`) into the VS Code-style IDE shell: relocate the proposal **Outline** into the shell's left sidebar region (Phase-2 mechanism) alongside a full set of **Workspace groups** (References · Criteria · Versions · Members), wire the **CodeMirror VS Code theme**, reskin the **editor tabs** and **bottom panel**, **populate the status bar** with workspace context, and **retire `cockpit.css`'s `--cm-*` rules** onto the `--app-*` theme — adding the small read-only backend endpoints the new sidebar groups + status bar need.

**Builds on:** the merged Phase-1 foundation (`AppShell` shell + Mantine v9 dark teal theme) and Phase-2 (`docs/superpowers/specs/2026-06-23-spa-vscode-redesign-phase2-dashboard-design.md` — the route-`handle`-driven sidebar region this phase reuses). Realizes §5–§6 of the master spec `docs/superpowers/specs/2026-06-23-spa-vscode-redesign-design.md` for the cockpit surface.

**Scope note — backend changes ARE in scope this phase.** Earlier phases were deliberately frontend-only; that was a per-phase choice, not a project rule. This phase adds small, **additive, read-only** endpoints where the SPA needs data it can't read today. No migrations (all tables already exist). No behavior changes to existing write paths.

---

## 1. Current state

The Cockpit (`frontend/src/routes/Cockpit.tsx`) renders **full-width** (no sidebar `handle`). It owns two-axis tab state as **local `useState`**: `topTab` (`draft|spec|scorecard|review`), `bottomTab` (`inbox|problems|activity`), `collapsed`. The four top tabs render `DraftPanel` / `SpecPanel` / `ScorecardPanel` / `ReviewPanel`; a `BottomPanel` sits below.

The **Outline is welded inside the editor**: `frontend/src/editor/ArtifactSectionEditor.tsx` renders `<nav aria-label="Outline">` (a `<ul>` of section buttons with `•` dirty markers) in column 1 of its `.cockpit` CSS grid, tightly coupled to that component's local state — per-section dirty `buffers`, the active-section `tab`, `selectSection`, the heading-delete guard, the citation selection bridge, and the 409 conflict/`saveError` banners. Draft and Spec each mount their **own** `ArtifactSectionEditor` (own sections, own buffers); switching the top tab unmounts one and mounts the other (so unsaved buffers already do not survive a Draft↔Spec switch — existing behavior).

The editor (`frontend/src/editor/CodeMirrorEditor.tsx`) is **CodeMirror 6** with a hand-rolled `EditorView.theme` referencing `--cm-*` tokens. `@uiw/codemirror-theme-vscode@4.25.10` is installed in `package.json` but **unused**.

Styling: `frontend/src/styles/cockpit.css` (367 lines, plain global classes, all `--cm-*` tokens — Source viewer, Citation composer, artifact tab strip, Spec lock banner, Proposal review, Scorecard band pills, Bottom panel, Problems, Activity). `frontend/src/theme/theme.css` carries a **dark-remap block** (lines 46–75) mapping `--cm-*` → dark values so the not-yet-reskinned cockpit reads dark-on-dark; the master spec says Phase 3 rewrites those rules onto `--app-*` and the remap "goes away."

The `StatusBar` (`frontend/src/shell/StatusBar.tsx`) is a **placeholder** ("Call Manager") — its own comment defers cockpit content (branch, deadline, spec-lock, scorecard band, Ln/Col, language) to Phase 3.

Backend cockpit JSON surface (`src/call_manager/web/api/workspaces.py`, mounted under `/api`): `GET …/draft`, `GET …/spec` (+ lock fields), save/lock/init/citation routes, `GET …/references`, `GET …/references/{id}/chunks`, `GET …/citations/{ev}/source`. **No** endpoint for workspace meta (call title/deadline/phase/role), versions, or members. The `ArtifactVersion` table already logs every commit (`commit_sha`, `created_by`, `message`, `created_at`); spec-lock lives on the workspace (`spec_locked_at`/`spec_locked_by`).

## 2. Decisions (locked in brainstorm)

1. **Relocate the Outline into the shell sidebar now** (not a later follow-on), using the Phase-2 route-`handle.Sidebar` mechanism.
2. **Full Workspace-groups set:** Outline · References · Criteria · Versions · Members. References + Criteria reuse existing data; Versions + Members get small new read-only endpoints.
3. **Backend changes welcome** where they make the app better (additive, read-only, no migrations).
4. **Cross-boundary state travels through global channels**, never context/props across `AppShell` — matching Phase 2. Active **tab + section → URL**; sections → RQ cache; dirty/cursor/save → one tiny external store.
5. **Full status-bar set** (master spec): branch-like indicator · deadline · spec-lock · scorecard band · save state · Ln/Col · language mode.
6. **Re-skin, not rewrite the editing engine.** `ArtifactSectionEditor`'s buffer/conflict/guard/cite logic is preserved byte-for-byte; only *where the outline renders* and *who owns tab/section state* changes.
7. **Execute as two independently-mergeable plans** (3a, 3b) under the Phase-3 umbrella (§8).

## 3. Architecture — relocating cockpit state across the shell boundary

`AppShell` renders the sidebar as a **sibling** of the routed Cockpit (`<Sidebar>` and `<Outlet>` are both children of the shell `body`), so a context provider mounted inside Cockpit cannot reach it. As in Phase 2, the channels that cross the boundary are **global**: the URL, the React Query cache, and one small external store.

```
AppShell
 ├─ ActivityBar
 ├─ Sidebar                 reads: URL(?tab,?sec) · RQ(useDraft/useSpec/useReferences/
 │   └─ CockpitSidebar             useScorecard/useVersions/useMembers) · dirty store
 ├─ main / <Outlet>
 │   └─ Cockpit             writes: URL(?tab,?sec); editor publishes dirty/cursor/save → store
 └─ StatusBar               reads: URL · RQ(useScorecard) · useCockpitMeta · cursor/save store
```

### 3.1 Active tab + section → URL search params
A new `useCockpitNav()` hook wraps `useSearchParams`, exposing `{ tab, sec, setTab, setSec, navigate(target) }` over `?tab=draft|spec|scorecard|review` and `?sec=<int>`. It **replaces** Cockpit's local `topTab` and the active-section `tab` inside `ArtifactSectionEditor`.
- The two-axis `onNavigate(NavTarget)` (Problems/Scorecard "jump") now sets `?tab` (and still sets the local bottom-panel tab + expands it).
- **Tabs/sections become deep-linkable** and survive refresh — a deliberate improvement.
- **`?sec` is a single index applied to the active artifact**, clamped to a valid section on artifact switch (preamble `0` always exists). This simplifies the prior per-artifact "remember last section" nicety into one clamped index — an accepted, minor behavior change in exchange for sidebar↔editor↔deep-link sync.
- **Bottom-panel tab + `collapsed` stay Cockpit-local** (they never cross the sidebar boundary). The `Section ↔ Full` view toggle stays **local to the editor**; selecting a section (sidebar row or `?sec` change) forces Section view for that index.

### 3.2 Sections → React Query cache
`CockpitSidebar`'s Outline calls the **same** `useDraft(id)` / `useSpec(id)` queries the editor uses; React Query dedupes by key, so there is no extra fetch and no prop plumbing. The "active artifact" for the Outline is `draft|spec` from `?tab`; on `scorecard`/`review` (no artifact) the Outline falls back to the **Draft** outline, and clicking a row navigates to `?tab=draft&sec=N`.

### 3.3 Dirty · cursor · save → one external store
A singleton module `frontend/src/cockpit/editorStore.ts` built on **`useSyncExternalStore`** (zero new deps): `{ dirty: Set<number>, cursor: {line,col}|null, save: "idle"|"saving"|"saved"|"dirty"|"error" }` for the **currently-mounted** artifact editor (at most one is mounted). `ArtifactSectionEditor` keeps its local `buffers`/conflict logic and **mirrors** the derived dirty-section set + save state into the store via an effect; `CodeMirrorEditor`'s `updateListener` publishes cursor Ln/Col. The editor **clears** the store on unmount. The sidebar Outline reads `dirty` for `•` markers; the status bar reads `cursor` + `save`.

*Alternative considered & rejected:* a React context above `AppShell` — but the sidebar is rendered by the top-level shell, so a cockpit-scoped provider still wouldn't wrap it; global channels are the same discipline Phase 2 settled on, and they keep deep-linking for free.

## 4. Components

| Component | Change | Responsibility |
|---|---|---|
| `useCockpitNav` (new) | — | URL `?tab`/`?sec` read/write + `navigate(NavTarget)`; single source of tab/section truth. |
| `editorStore` (new) | — | `useSyncExternalStore` singleton: dirty set / cursor / save state for the mounted editor. |
| `CockpitSidebar` (new) | — | The cockpit `handle.Sidebar`: collapsible groups Outline · References · Criteria · Versions · Members. Reads URL + RQ + store; no props across the shell. |
| `SidebarGroup` / tree-row (new) | — | Small presentational collapsible-group + tree-row primitives (VS Code style), reused by the five groups. |
| `Cockpit.tsx` | modify | Drop local `topTab` (→ `useCockpitNav`); reskinned IDE editor-tab strip; keep bottom-panel tab/collapse local; register `handle: { Sidebar: CockpitSidebar }` in `router.tsx`. |
| `ArtifactSectionEditor.tsx` | modify | **Remove** the in-editor `<nav Outline>`; read active section from `?sec` (clamped); publish dirty/save to the store; keep buffers/conflict/guard/cite logic intact; grid collapses to editor (+peek/pinned/composer). |
| `CodeMirrorEditor.tsx` | modify | Replace `--cm-*` `EditorView.theme` with `@uiw/codemirror-theme-vscode` (dark) **+ a teal-recolor extension** (headings `#4dc4d9`, links/pills `#2dd4bf`); add a line gutter; publish cursor Ln/Col. Heading guard / remote-push / selection bridge / read-only compartment unchanged. |
| `BottomPanel.tsx` (+ panels) | modify | Reskin as a true IDE panel (toolbar, Problems severity/count badge, collapse). Content/behavior unchanged. |
| `StatusBar.tsx` | modify | On `/cockpit`: branch indicator · deadline · spec-lock · scorecard band · save · `Ln/Col` · `Markdown`. Off-cockpit: minimal. Reads URL + store + `useCockpitMeta` + `useScorecard`. |
| `TitleBar.tsx` | modify (3b) | Breadcrumb shows the call title from `useCockpitMeta` (falls back to "Proposal workspace"). |

## 5. Backend additions (additive, read-only, no migration)

All under `src/call_manager/web/api/workspaces.py`, member-gated (`require_member`), mirroring the existing endpoints' auth/ownership-guard patterns. Frontend hooks added to `frontend/src/api/` and types regenerated into `schema.gen.ts` (see the OpenAPI-codegen note in §9).

1. **`GET /api/workspaces/{id}/cockpit-meta` → `CockpitMetaOut`**
   `{ call_title: str | null, call_deadline: date | null, current_phase: "setup"|"active", role: "owner"|"member", spec_locked: bool }`.
   Resolved from the `Workspace` (+ its `Call`) row. Feeds the status bar (deadline, spec-lock fallback), the title-bar breadcrumb, and the Members "your role" hint. Hook: `useCockpitMeta(id)`.

2. **`GET /api/workspaces/{id}/versions` → `list[VersionOut]`**
   `[{ commit_sha: str, message: str, created_by: str, created_at: datetime }]`, newest-first, capped (≈50). From the `ArtifactVersion` table. Hook: `useVersions(id)`. Powers the **Versions** group (count + recent-commit list).

3. **`GET /api/workspaces/{id}/members` → `list[MemberOut]`**
   `[{ email: str, display_name: str | null, role: "owner"|"member" }]`, owner first. From the workspace owner + membership rows (the same source `require_member`/`require_owner` authorize against). Hook: `useMembers(id)`. Powers the **Members** group.

**Criteria** needs **no** new endpoint: the **Scorecard** read-model (`useScorecard`) already returns per-criterion entries (name, weight, band, deep-link). The Criteria group renders those with their band pills and jumps to the Scorecard tab.

Backend tests (pytest, testcontainers Postgres) cover each endpoint: member-gating (403 for non-members; 404 for unknown/cross-tenant ids), the read-model shape, and empty/edge cases (no versions yet, solo owner). Per project memory, verify any flagged failure by re-running in isolation (whole-suite runs drop the test Postgres under load).

## 6. Visual spec (tokens from the Phase-1 palette §4)

- **Editor tabs:** active tab raised on `--app-editor`, inactive on `--app-tabbar` `#25282d`; bottom accent on the active tab in `--app-accent`.
- **Sidebar groups:** group headers uppercase muted (`--app-muted`) with a codicon chevron + count chip; tree rows hover `--app-hover`, active row `--app-selected` with a left accent border; `•` dirty marker in `--app-warn`.
- **CodeMirror:** `@uiw/codemirror-theme-vscode` dark base, recolored — headings `--app-heading` `#4dc4d9`, links/citation-pill `--app-link` `#2dd4bf` on `--app-selected`-tinted chip; gutter muted; focus ring `--app-accent`.
- **Bottom panel:** panel bar on `--app-tabbar`, active tab underline `--app-accent`, Problems badge `--app-bad`/`--app-warn` by max severity.
- **Scorecard band pills → theme:** `strong`→`--app-good`, `adequate`→`--app-muted`, `at_risk`→`--app-warn`, `gap`→`--app-bad` (tinted chip surfaces from the dark palette).
- **Status bar:** the heritage-teal `--app-statusbar` `#0f766e` bar; items separated by thin dividers; deadline turns `--app-bad` when ≤14d (reuse the Deadline cutoffs from Phase 2 where sensible).

## 7. Behavior contracts preserved (re-skin, not rewrite)

- **Editing engine unchanged:** per-section dirty `buffers`, the heading-delete guard (`blockedByHeadingGuard`), the remote controlled-value push, the select-to-cite selection bridge, the readOnly compartment, and the 409 conflict / non-conflict `saveError` banners all keep their current logic and tests. Only the Outline's render location and the section-index source (`?sec`) change.
- **Save / cite / lock / coherence / evaluator / review / problems / activity flows** and all their endpoints are untouched.
- **Citation peek/pin/composer** stay in the editor column.
- The `router.test.tsx` drift guard stays green: adding `handle` to the cockpit route and the three new `/api/workspaces/{id}/…` endpoints introduce **no new SPA route**; `routes.gen.ts` (ownership prefixes, not param paths) is unchanged.
- Existing cockpit vitest is kept green and updated **deliberately** only where the DOM/role shape changes (the Outline `<nav>` moving out of `ArtifactSectionEditor` into `CockpitSidebar`; the tab strip restyle). No assertion is loosened to hide a regression.

## 8. Decomposition — two shippable plans

**Plan 3a — Relocation & reskin (frontend).** `useCockpitNav` + `editorStore`; relocate the **Outline** into `CockpitSidebar` (Outline group only); CodeMirror VS Code theme + teal recolor + gutter + cursor publish; editor-tab + bottom-panel reskin; rewrite `cockpit.css` onto `--app-*` and **retire the `theme.css` `--cm-*` remap block** (grep-verify nothing in the SPA still reads `--cm-*`, retiring the legacy `tokens.css` for the SPA); **minimal status bar** (Ln/Col · language · save). Ends green (tsc + vitest + build), independently mergeable.

**Plan 3b — Workspace groups & rich status bar (backend + frontend).** The three read-only endpoints (`cockpit-meta`, `versions`, `members`) + pytest + regenerated types + hooks; the **References · Criteria · Versions · Members** sidebar groups; the **full status bar** (deadline · spec-lock · scorecard band · branch indicator) + the title-bar breadcrumb. Ends green (tsc + vitest + build + backend pytest), independently mergeable.

## 9. Scope / non-goals

- **In scope:** the cockpit reskin, the Outline relocation, the five sidebar groups, the three read-only endpoints, the populated status bar, the `cockpit.css`/`--cm-*` retirement.
- **No write-path or schema changes;** no migrations (all tables exist). New endpoints are read-only + additive.
- **No new SPA routes;** the drift guard stays green.
- **No sidebar collapse/show-hide toggle** (region only, matching Phase 2) — a later fast-follow.
- **Light theme, command palette, Jinja-page restyle, split editors, multi-workspace tabs** remain deferred (master spec §9).
- **OpenAPI codegen:** regenerate `schema.gen.ts` with `EMBEDDING_MODEL=bge-m3` (the `.env` mxbai model logs a stdout WARNING that breaks `openapi-typescript`) — per project memory.

## 10. Testing & validation

- **Frontend new:** `useCockpitNav` (URL read/write + clamp), `editorStore` (subscribe/snapshot/clear-on-unmount), `CockpitSidebar` (each group renders; loading/empty/error guards; Outline reflects `?tab`/`?sec`/dirty and writes `?sec` on click), the reskinned `StatusBar` (cockpit vs off-cockpit; cursor/save/band/deadline), editor-tab + bottom-panel restyle.
- **Frontend updated:** `Cockpit.test.tsx` (URL-driven tabs; Outline no longer in the editor), `ArtifactSectionEditor` tests (section from `?sec`; outline removed), `router.test.tsx` stays green. Resolve the pre-existing `Cockpit.test` `act()` warnings as part of the editor refactor.
- **Backend new:** pytest for the three endpoints (auth-gating, shape, edge cases).
- **Gate every task:** `pnpm exec tsc --noEmit` + `pnpm vitest run` + `pnpm build` green; 3b also `uv run pytest` for the new endpoints (verify any flagged failure in isolation).
- **Visual QA:** headless screenshot of the reskinned cockpit (editor + sidebar groups + bottom panel + status bar) against the approved teal mockup, per the visual-check recipe.
