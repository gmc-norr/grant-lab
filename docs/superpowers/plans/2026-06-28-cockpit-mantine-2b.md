# Cockpit Mantine Migration — Slice 2b Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-skin the cockpit bottom panel + its six sub-panels (Inbox, Problems, Activity, Library, Citations, Claims) from legacy global CSS / orphan classes to Mantine + the existing scoped `CockpitPanels.module.css`, removing the `bottom-panel*`/`problem(s)-*`/`activity-*`/`inbox-empty` rule groups from `styles/cockpit.css`.

**Architecture:** Continues Slice 2a (merged `12cc2e9`). Same bar: Mantine primitives for everything; the existing scoped module for the few irreducible visuals; stable `data-*`/role hooks for tests. The bottom-panel tab bar becomes Mantine `Tabs` (the collapse control stays a `Button`). Four sub-panels (Inbox/Problems/Activity) have global `cockpit.css` rules to delete; the rest (Library/Citations/Claims) use **orphan classes styled nowhere**, so they are Mantine-conversion-only (no teardown — the conversion finally gives them real styling).

**Tech Stack:** React 19 + Mantine v9 + TanStack Query + Vite + Vitest. Spec: `docs/superpowers/specs/2026-06-27-cockpit-mantine-migration-design.md`. Prior slice: `docs/superpowers/plans/2026-06-27-cockpit-mantine-2a.md`.

## Global Constraints

Every task's requirements implicitly include this section. (Identical bar to Slice 2a.)

- **Pure re-skin — NO behaviour, data-flow, or copy changes.** Every visible string stays verbatim (e.g. "Nothing pending.", "No problems detected.", "Check uncited claims", "Deep check", "No citation issues found.", "analyzing…", "Drop PDFs here", "Reprocess", the per-tab labels with their count suffix like "Inbox 3").
- **Preserve ARIA contracts** — tests assert them:
  - The bottom **tab bar** renders `role="tab"` + `aria-selected` with accessible names = the visible label INCLUDING the count (`getByRole("tab",{name:/inbox 3/i})`, `/problems 3/i`, `/citations 2/i`, `/^activity$/i`) → use Mantine **`Tabs`** (value=`activeTab`, onChange=`onTab`). The **collapse** control stays a plain `Button` (`getByRole("button",{name:/expand panel/i})` / "Collapse panel").
  - Keep `role="status"` (problems-error, citations cap/note, claims job spans), `role="alert"` (citations/claims load-error), `aria-busy="true"` (loading), `aria-label`s on sections + form controls (e.g. "Add reference file", "New reference kind", `Kind for ${label}`, "Fetch by DOI / PMID / arXiv / URL", "Add image file", `Delete ${filename}`, "Confirm dismiss"), and `aria-hidden` on the problem severity glyph.
- **Mantine + the EXISTING scoped module only** — no new global CSS. Append to `frontend/src/cockpit/CockpitPanels.module.css` (created in 2a). Module classes are unlayered → they override Mantine's `@layer mantine` without `!important`. No top-level bare element selectors (descendant under a hashed class is fine).
- **Stable test hooks:** `data-category="member"` on the Activity member-category row (replaces the `.category-member` class the test queries). Preserve existing `data-status`/`data-meta` on Library badges.
- **Layout/semantic parity** (this IS a pure re-skin — keep DOM semantics, not just looks):
  - Where the original used `<ul>/<li>`, keep list semantics with Mantine polymorphism: parent `<Stack component="ul" style={{ listStyle: "none", padding: 0, margin: 0 }}>` + each row `component="li"`. (`CitationsPanel.test.tsx:66` asserts `getAllByRole("listitem")` for severity ordering — non-negotiable there; apply the same pattern to the other lists for parity. This differs from Slice 2a, which dropped `<li>` on its presentational rows.)
  - Where the original right-aligned a trailing row action (`.problem-act` and `.activity-act` both had `margin-left: auto`), give that action `ml="auto"` — or wrap the trailing action(s) in a `<Group ml="auto">`.
  - Preserve `<h3>`/`<h4>` heading elements as `<Text component="h3"|"h4" …>` (keep the element; style it like the old caption), not a bare non-heading `<Text>`.
- **cockpit.css teardown:** in each task, delete ONLY that component's rule block(s) from `frontend/src/styles/cockpit.css`. Do NOT delete the file or its `main.tsx:8` import (that is Slice 2c). After 2b, the ONLY remaining groups must be `source-viewer*` and `cite-composer*` (Slice 2c).
- **Test provider fix:** every 2b panel test currently renders WITHOUT `MantineProvider` (bare `render(...)` or `render(<QueryClientProvider>…)`). Wrap each test's render tree root in `<MantineProvider theme={theme} defaultColorScheme="dark">` (import `theme` from the correct relative path to `theme/theme`), preserving any existing QueryClient/router/mocks. The `document.fonts`/`matchMedia`/`ResizeObserver` stubs already live in `test-setup.ts`.
- **Type gate is `tsc`** (no TS ESLint; `noUnusedLocals: true` — import only what you use). Per-task: `pnpm exec tsc --noEmit` + the task's named test file(s). Slice end (Task 8): full `pnpm test -- --run` + `pnpm build` + teardown grep. All commands from `frontend/`.

---

### Task 1: BottomPanel → Mantine `Tabs` (tab bar) + `Button` (collapse)

**Files:**
- Modify: `frontend/src/cockpit/BottomPanel.tsx`
- Modify: `frontend/src/styles/cockpit.css` (remove `.bottom-panel`, `.bottom-panel-bar`, `.bottom-panel-tabs`, `.bottom-panel-tabs [role="tab"]` variants, `.bottom-panel-toggle`, `.bottom-panel-body`)
- Test: `frontend/src/cockpit/BottomPanel.test.tsx` (add MantineProvider)

**Current structure:** `<section className="bottom-panel" aria-label="Workspace panel">` → `.bottom-panel-bar` (a `role="tablist"` `.bottom-panel-tabs` div of 6 `<button role="tab" aria-selected>` with count-suffixed labels, plus the `.bottom-panel-toggle` collapse `<button>`), then `.bottom-panel-body` rendering the active sub-panel.

- [ ] **Step 1: Add MantineProvider to `BottomPanel.test.tsx`**

The two `render(<BottomPanel … />)` calls (lines ~25, ~46, and the others) render bare. Add `import { MantineProvider } from "@mantine/core";` + `import { theme } from "../theme/theme";` and wrap each render's element in `<MantineProvider theme={theme} defaultColorScheme="dark">…</MantineProvider>`. (A shared `renderBP` helper is fine if you prefer — keep the existing prop sets + mocks.)

- [ ] **Step 2: Re-skin `BottomPanel.tsx`**

```tsx
import { Box, Button, Group, ScrollArea, Tabs } from "@mantine/core";
```
- `<section className="bottom-panel" aria-label="Workspace panel">` → `<Box component="section" aria-label="Workspace panel" style={{ borderTop: "1px solid var(--app-border)", background: "var(--app-editor)" }}>`.
- `.bottom-panel-bar` → `<Group justify="space-between" gap="sm" px="sm" py={4} style={{ background: "var(--app-tabbar)" }}>`.
- The `role="tablist"` `.bottom-panel-tabs` + its 6 buttons → a Mantine `<Tabs value={activeTab} onChange={(v) => v && onTab(v as BottomTab)}>` with `<Tabs.List aria-label="Panel">` of `<Tabs.Tab value="inbox">Inbox {inboxCount}</Tabs.Tab>` … (keep the exact count-suffixed labels: `Inbox {inboxCount}`, `Problems {problemsCount}`, `Activity`, `Library`, `Citations {auditCount}`, `Claims {claimsCount}`). Put the `Tabs` inside the `Group` (left side); the collapse Button is the right side. NOTE: Mantine `Tabs.List` has a bottom border by default — acceptable; do not add `Tabs.Panel` (bodies render below, as today).
- `.bottom-panel-toggle` collapse `<button>` → `<Button variant="subtle" size="xs" aria-expanded={!collapsed} onClick={onToggleCollapsed}>{collapsed ? "Expand panel" : "Collapse panel"}</Button>`.
- `.bottom-panel-body` (was `max-height:14rem; overflow-y:auto; padding`) → `{!collapsed && <ScrollArea.Autosize mah="14rem" p="sm">{…active panel…}</ScrollArea.Autosize>}`. Keep the exact `activeTab === …` conditional bodies unchanged.
- Keep all the count hooks (`useInboxCount`, etc.) and the `onNavigate` plumbing identical.

- [ ] **Step 3: Remove `.bottom-panel*` rules** from `styles/cockpit.css`. Verify: `grep -n "bottom-panel" frontend/src/styles/cockpit.css` → no matches.

- [ ] **Step 4: Run test + typecheck**

Run: `pnpm test -- --run src/cockpit/BottomPanel.test.tsx` → PASS (tab roles + names with counts, collapse button, body visibility).
Run: `pnpm exec tsc --noEmit` → clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/cockpit/BottomPanel.tsx frontend/src/cockpit/BottomPanel.test.tsx frontend/src/styles/cockpit.css
git commit -m "feat(cockpit): BottomPanel -> Mantine Tabs + ScrollArea"
```

---

### Task 2: InboxPanel → Mantine

**Files:**
- Modify: `frontend/src/cockpit/InboxPanel.tsx`
- Modify: `frontend/src/styles/cockpit.css` (remove `.inbox-empty`)
- Test: `frontend/src/cockpit/InboxPanel.test.tsx` (add MantineProvider)

**Current structure:** empty state `<p className="inbox-empty">Nothing pending.</p>`; else `<div className="inbox-panel">` with two `<ProposalReview>` (already Mantine from 2a).

- [ ] **Step 1: Add MantineProvider to `InboxPanel.test.tsx`** (wrap the render tree root; preserve mocks).

- [ ] **Step 2: Re-skin `InboxPanel.tsx`**
- `<p className="inbox-empty">Nothing pending.</p>` → `<Text c="dimmed" p="sm">Nothing pending.</Text>`.
- `<div className="inbox-panel">` → `<Stack gap="md">`.
- Imports: `import { Stack, Text } from "@mantine/core";`. Keep the section logic + both `<ProposalReview>` unchanged.

- [ ] **Step 3: Remove `.inbox-empty`** from `styles/cockpit.css`. Verify: `grep -n "inbox-empty" frontend/src/styles/cockpit.css` → no matches. (`.inbox-panel` is an orphan — nothing to remove.)

- [ ] **Step 4: Run test + typecheck** → `pnpm test -- --run src/cockpit/InboxPanel.test.tsx` PASS; `pnpm exec tsc --noEmit` clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/cockpit/InboxPanel.tsx frontend/src/cockpit/InboxPanel.test.tsx frontend/src/styles/cockpit.css
git commit -m "feat(cockpit): InboxPanel -> Mantine"
```

---

### Task 3: ProblemsPanel → Mantine (severity colour via Mantine `c=`)

**Files:**
- Modify: `frontend/src/cockpit/ProblemsPanel.tsx`
- Modify: `frontend/src/styles/cockpit.css` (remove `.problems-group-title`, `.problems-stale`, `.problem-row`, `.problem-row.severity-error/.warning .problem-sev`, `.problem-detail`, `.problem-act`, `.problems-empty`, `.problems-error`)
- Test: `frontend/src/cockpit/ProblemsPanel.test.tsx` (add MantineProvider)

**Current structure:** `.problems-panel` div → `.problems-toolbar` (run `<button>`), error/loading/empty `<p>`, then per category a `<section className="problems-group" aria-label={label}>` with `<h3 className="problems-group-title">` (+ optional `.problems-stale` span) and a `<ul>` of `<li className="problem-row severity-${sev}">` (icon `.problem-sev` aria-hidden, `.problem-title`, `.problem-detail`, optional `.problem-act` + `.problem-dismiss` buttons).

- [ ] **Step 1: Add MantineProvider to `ProblemsPanel.test.tsx`** (wrap the `render(<QueryClientProvider>…)` tree).

- [ ] **Step 2: Re-skin `ProblemsPanel.tsx`**
```tsx
import { Box, Button, Group, Stack, Text } from "@mantine/core";
```
- `runButton`: `<button className="problems-run-uncited">` → `<Button size="xs" variant="default" disabled={…} onClick={…}>{running ? "Checking…" : "Check uncited claims"}</Button>`.
- Wrappers `.problems-panel`/`.problems-toolbar` → `<Stack gap="sm">` / `<Group gap="sm">`.
- error/loading/empty `<p>` → `<Text role="status" c="red" size="sm">` (error) / `<Text c="dimmed" size="sm">` (loading/empty), preserving strings.
- Group: `<section aria-label={label}>` → `<Box component="section" aria-label={CATEGORY_LABEL[g.cat]}>`; `<h3 className="problems-group-title">` → `<Text component="h3" tt="uppercase" size="xs" fw={600} c="dimmed">` (KEEP the `<h3>` element; style it like the old uppercase caption); the `.problems-stale` span → `<Text span c="yellow" size="xs"> · may be outdated</Text>` (keep exact text).
- Rows: keep list semantics — `<ul>` → `<Stack component="ul" gap={4} style={{ listStyle: "none", padding: 0, margin: 0 }}>`. Each `<li className="problem-row severity-${sev}">` → `<Group component="li" gap="sm" align="flex-start" wrap="nowrap">`. The severity glyph keeps its meaning via Mantine colour: `<Text aria-hidden c={r.severity === "error" ? "red" : "yellow"}>{r.severity === "error" ? "⛔" : "⚠"}</Text>`. Title → `<Text size="sm" fw={600}>`, detail → `<Text size="sm" c="dimmed">`. Wrap the trailing actions to preserve the old right-alignment (`.problem-act` had `margin-left:auto`): `<Group ml="auto" gap="xs">` containing the `.problem-act` "Go to {title}" `<Button size="xs" variant="subtle">` and (for `uncited_claim`) the `.problem-dismiss` "Dismiss" `<Button size="xs" variant="subtle" disabled={dismiss.isPending}>` — keep exact text + onClick.

- [ ] **Step 3: Remove the `.problem(s)-*` rules** from `styles/cockpit.css`. Verify: `grep -nE "problem-|problems-" frontend/src/styles/cockpit.css` → no matches. (Orphans `problems-panel`/`problems-toolbar`/`problems-loading`/`problem-title`/`problem-sev`/`problem-dismiss`/`problems-run-uncited` were never in cockpit.css.)

- [ ] **Step 4: Run test + typecheck** → `pnpm test -- --run src/cockpit/ProblemsPanel.test.tsx` PASS; `pnpm exec tsc --noEmit` clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/cockpit/ProblemsPanel.tsx frontend/src/cockpit/ProblemsPanel.test.tsx frontend/src/styles/cockpit.css
git commit -m "feat(cockpit): ProblemsPanel -> Mantine"
```

---

### Task 4: ActivityPanel → Mantine + `data-category` hook

**Files:**
- Modify: `frontend/src/cockpit/ActivityPanel.tsx`
- Modify: `frontend/src/cockpit/CockpitPanels.module.css` (append `.activityRow` member accent)
- Modify: `frontend/src/styles/cockpit.css` (remove `.activity-list`, `.activity-row`, `.activity-row.category-member`, `.activity-actor`, `.activity-time`, `.activity-act`, `.activity-empty`, `.activity-loading`, `.activity-error`)
- Test: `frontend/src/cockpit/ActivityPanel.test.tsx` (add MantineProvider + `.category-member` → `[data-category="member"]`)

**Current structure:** `.activity-panel` div → error/loading/empty `<p>`, else `<ul className="activity-list">` of `<li className="activity-row category-${category}">` (actor/summary/time spans + optional `.activity-act` button). The `.activity-row.category-member` rule adds a left accent border.

- [ ] **Step 1: Update `ActivityPanel.test.tsx`**
- Add MantineProvider wrap (import theme + MantineProvider).
- Line ~84: `expect(document.querySelector(".category-member")).not.toBeNull();` → `expect(document.querySelector('[data-category="member"]')).not.toBeNull();`.

- [ ] **Step 2: Append the member accent to the module**
```css
/* --- ActivityPanel member-category accent (was .activity-row.category-member) --- */
.activityRow[data-category="member"] {
  border-left: 2px solid var(--app-accent);
  padding-left: 0.4rem;
}
```

- [ ] **Step 3: Re-skin `ActivityPanel.tsx`**
```tsx
import { Box, Button, Group, Stack, Text } from "@mantine/core";
import styles from "./CockpitPanels.module.css";
```
- error/loading/empty `<p>` → `<Text role="status" c="red" size="sm">` (error) / `<Text c="dimmed" size="sm">` (loading/empty); wrap each in `<Box component="div">` or return the `<Text>` directly (keep the strings).
- `<ul className="activity-list">` → `<Stack component="ul" gap={4} style={{ listStyle: "none", padding: 0, margin: 0 }}>`; each `<li className="activity-row category-${r.category}">` → `<Group component="li" gap={6} align="baseline" wrap="wrap" data-category={r.category} className={styles.activityRow}>` (the `data-category` is the test hook AND drives the module accent). actor → `<Text span fw={600} size="sm">`, summary → `<Text span size="sm">`, time → `<Text span c="dimmed" size="sm"> · {…}</Text>`, act button → `<Button size="xs" variant="subtle" ml="auto">Go to</Button>` (the `ml="auto"` preserves `.activity-act`'s old `margin-left:auto` right-alignment).
- Keep `formatRelativeTime` + `toNav` unchanged.

- [ ] **Step 4: Remove `.activity-*` rules** from `styles/cockpit.css`. Verify: `grep -n "activity-" frontend/src/styles/cockpit.css` → no matches. (`activity-panel`/`activity-summary` orphans.)

- [ ] **Step 5: Run test + typecheck** → `pnpm test -- --run src/cockpit/ActivityPanel.test.tsx` PASS; `pnpm exec tsc --noEmit` clean.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/cockpit/ActivityPanel.tsx frontend/src/cockpit/CockpitPanels.module.css frontend/src/cockpit/ActivityPanel.test.tsx frontend/src/styles/cockpit.css
git commit -m "feat(cockpit): ActivityPanel -> Mantine + data-category hook"
```

---

### Task 5: LibraryPanel → Mantine (orphan classes, NO teardown)

**Files:**
- Modify: `frontend/src/cockpit/LibraryPanel.tsx`
- Test: `frontend/src/cockpit/LibraryPanel.test.tsx` (add MantineProvider)

**Note:** every `library-*` class is an orphan (styled nowhere) — so NO `cockpit.css` change; the conversion gives the panel real styling. Preserve ALL `aria-label`s (tests select by them) and the `data-status`/`data-meta` on the status badges.

- [ ] **Step 1: Add MantineProvider to `LibraryPanel.test.tsx`** (wrap the `render(<QueryClientProvider>…)` tree).

- [ ] **Step 2: Re-skin `LibraryPanel.tsx`** — keep ALL logic (the `useUploadQueue` hook, handlers, `MetadataModal`) byte-identical; only swap presentation:
- Outer `.library-panel` → `<Stack gap="md">`. The three `<section aria-label=…>` keep their `aria-label`; wrap contents in `<Stack gap="sm">`.
- `.library-section-title` `<h3>` → `<Title order={4}>`.
- File `<input type="file">` — keep the native `<input>` with its `aria-label` (Mantine's `FileButton` would change the a11y contract; a native input is fine and tests rely on the label). The kind `<select>` → Mantine `NativeSelect` (keeps `<option>` semantics + `aria-label`) OR keep native `<select>`; prefer `NativeSelect` with `data`/`aria-label`.
- `.library-dropzone` → `<Box>` with a dashed border via inline tokens (`style={{ border: "1px dashed var(--app-border)", borderRadius: 6, padding: "0.75rem", textAlign: "center" }}`), keep the `onDragOver`/`onDrop`.
- `.library-fetch` → `<Group gap="sm">` with a Mantine `TextInput` (keep `aria-label`) + `<Button>Fetch</Button>`.
- Reference list `<ul className="library-list">` → `<Stack component="ul" gap="xs" style={{ listStyle: "none", padding: 0, margin: 0 }}>` of rows (`<Group component="li" gap="sm" wrap="wrap">`). Row label → `<Text size="sm" fw={600}>`; per-row kind `<select>` → `NativeSelect` (keep `aria-label={`Kind for ${…}`}`); status badges `<span className="library-badge" data-status=…>` / `data-meta=…` → `<Badge size="sm" variant="light" data-status={r.text_status}>{r.text_status}</Badge>` and the `data-meta` one likewise (KEEP the `data-status`/`data-meta` attributes); download `<a className="library-dl">` → `<Anchor href={…} target="_blank" rel="noreferrer" size="sm">download</Anchor>`; the Edit/Waiver/Reprocess/Delete `<button>`s → `<Button size="xs" variant="default/subtle">` (keep exact text + onClick).
- Files `<ul className="library-file-list">` → `<Stack component="ul" gap="xs" style={{ listStyle: "none", padding: 0, margin: 0 }}>`; each row → `<Group component="li" gap="sm">` with `<Image src=… alt=… w={40} h={40} />` (Mantine `Image`) or keep `<img>` (small) — keep the `alt`; filename `<Text size="sm">`; delete `<Button size="xs">` (keep `aria-label={`Delete ${filename}`}`).
- `.library-msg`/`.library-note` `<p>` → `<Text c="dimmed" size="sm">`.
- Imports: `import { Anchor, Badge, Box, Button, Group, Image, NativeSelect, Stack, Text, TextInput, Title } from "@mantine/core";` (import ONLY what you use — drop any unused).

- [ ] **Step 3: Run test + typecheck** → `pnpm test -- --run src/cockpit/LibraryPanel.test.tsx` PASS (selects by aria-label / text / data-status); `pnpm exec tsc --noEmit` clean.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/cockpit/LibraryPanel.tsx frontend/src/cockpit/LibraryPanel.test.tsx
git commit -m "feat(cockpit): LibraryPanel -> Mantine"
```

---

### Task 6: CitationsPanel → Mantine (orphan classes, NO teardown)

**Files:**
- Modify: `frontend/src/cockpit/CitationsPanel.tsx`
- Test: `frontend/src/cockpit/CitationsPanel.test.tsx` (add MantineProvider)

**Note:** `citations-*`/`issue-*`/`severity-*` classes are orphans → NO `cockpit.css` change.

- [ ] **Step 1: Add MantineProvider to `CitationsPanel.test.tsx`** (wrap the bare `render(<CitationsPanel … />)` calls; keep mocks).

- [ ] **Step 2: Re-skin `CitationsPanel.tsx`** — keep the `RANK` sort + deep/audit logic byte-identical:
- Loading `<p aria-busy>` → `<Text aria-busy="true">Checking citations…</Text>`; error `<p role="alert">` → `<Text role="alert" c="red">`; no-draft `.citations-empty` `<p>` → `<Text c="dimmed">`.
- `.citations-toolbar` → `<Group gap="sm">`; Re-run / Deep check `<button>`s → `<Button size="xs" variant="default">` (keep disabled/onClick + "Deep checking…" text); `.citations-summary` span → `<Text span size="sm" c="dimmed">`.
- `.citations-cap` `<p role="status">` → `<Text role="status" size="sm" c="yellow">` (keep text); `.citations-note` → `<Text size="sm" c="dimmed">`.
- `.citations-clean` `<p>` → `<Text c="dimmed">No citation issues found.</Text>`.
- Issues — **MUST keep list semantics** (`CitationsPanel.test.tsx:66` asserts `getAllByRole("listitem")` and the error-before-warning ordering): `<ul className="citations-issues">` → `<Stack component="ul" gap="xs" style={{ listStyle: "none", padding: 0, margin: 0 }}>`; each `<li className="severity-${sev}">` → `<Group component="li" gap="sm" align="flex-start" wrap="wrap">` (drop the orphan className; the row stays a `listitem`). The `RANK` sort that produces the ordering is unchanged. `.issue-type` → `<Text span size="sm" fw={600}>`; `<code>{citekey}</code>` → Mantine `<Code>{citekey}</Code>`; `.issue-loc`/`.issue-detail` → `<Text span size="sm" c="dimmed">`; View-source `<button>` → `<Button size="xs" variant="subtle">View source</Button>` (keep onNavigate).
- Imports: `import { Button, Code, Group, Stack, Text } from "@mantine/core";`.

- [ ] **Step 3: Run test + typecheck** → `pnpm test -- --run src/cockpit/CitationsPanel.test.tsx` PASS; `pnpm exec tsc --noEmit` clean.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/cockpit/CitationsPanel.tsx frontend/src/cockpit/CitationsPanel.test.tsx
git commit -m "feat(cockpit): CitationsPanel -> Mantine"
```

---

### Task 7: ClaimsPanel → Mantine (orphan classes, NO teardown)

**Files:**
- Modify: `frontend/src/cockpit/ClaimsPanel.tsx`
- Test: `frontend/src/cockpit/ClaimsPanel.test.tsx` (add MantineProvider)

**Note:** `claims-*`/`kind-*` classes are orphans → NO `cockpit.css` change. Preserve `role="status"` on the job spans and `aria-label="Confirm dismiss"`.

- [ ] **Step 1: Add MantineProvider to `ClaimsPanel.test.tsx`** (wrap the bare `render(<ClaimsPanel … />)` calls; keep mocks).

- [ ] **Step 2: Re-skin `ClaimsPanel.tsx`** (3 components — `ClaimsPanel`, `ClaimSectionView`, `FindingRow`) — keep all handlers/state byte-identical:
- loading `<p aria-busy>` / error `<p role="alert">` / empty `.claims-empty` `<p>` → `<Text aria-busy>` / `<Text role="alert" c="red">` / `<Text c="dimmed">` (keep strings).
- `.claims-panel` / `.claims-section` → `<Stack gap="md">` / `<Box component="section">` (or `<Stack component="section" gap="sm">`).
- `.claims-section-head` `<header>` → `<Group component="header" gap="sm" align="center">`; `<h4>` → `<Title order={4}>` (keep the h4 level); job spans `.claims-job`/`.claims-job-failed` `<span role="status">` → `<Text role="status" span size="sm" c="dimmed">` (failed: `c="red"`) — keep exact strings incl. the `— ${error}` tail; Reanalyze `<button>` → `<Button size="xs" variant="subtle">`.
- `.claims-none` `<p>` → `<Text c="dimmed" size="sm">No open findings</Text>`.
- findings — keep list semantics: `<ul>` → `<Stack component="ul" gap="xs" style={{ listStyle: "none", padding: 0, margin: 0 }}>`; each `<li className="claims-finding kind-${kind}">` → `<Box component="li">` (drop the orphan className); `.claims-kind` span → `<Badge size="sm" variant="light">{KIND_LABEL[…]}</Badge>`; `.claims-claim` `<p>` → `<Text size="sm">`; `.claims-quote` `<blockquote>` → Mantine `<Blockquote p="xs">` (or `<Text component="blockquote" size="sm" c="dimmed">`); `.claims-ref` span → `<Text span size="xs" c="dimmed">`.
- `.claims-actions` `<div>` → `<Group gap="sm">`; Insert/Dismiss `<button>`s → `<Button size="xs">`; the `.claims-dismiss-form` inline form → `<Group gap="xs">` with `<TextInput size="xs" placeholder="Reason (required)">` + `<Button size="xs" aria-label="Confirm dismiss" disabled={…}>` (keep the reason state + trim logic).
- Imports: `import { Badge, Blockquote, Box, Button, Group, Stack, Text, TextInput, Title } from "@mantine/core";` (drop any unused).

- [ ] **Step 3: Run test + typecheck** → `pnpm test -- --run src/cockpit/ClaimsPanel.test.tsx` PASS; `pnpm exec tsc --noEmit` clean.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/cockpit/ClaimsPanel.tsx frontend/src/cockpit/ClaimsPanel.test.tsx
git commit -m "feat(cockpit): ClaimsPanel -> Mantine"
```

---

### Task 8: Slice 2b verification gate

**Files:** none (verification only).

- [ ] **Step 1: Full type + test + build** (from `frontend/`):
- `pnpm exec tsc --noEmit` → clean.
- `pnpm test -- --run` → ALL green.
- `pnpm build` → bundle builds.

- [ ] **Step 2: Confirm teardown leaves ONLY 2c groups**

Run: `grep -nE "bottom-panel|problem-|problems-|activity-|inbox-empty" frontend/src/styles/cockpit.css` → no matches.
Run: `grep -oE "^\.[a-z][a-z0-9-]+" frontend/src/styles/cockpit.css | sort -u` → must list ONLY `source-viewer*` and `cite-composer*` classes (the Slice 2c groups). `cockpit.css` is NOT deleted here.

- [ ] **Step 3: Visual check (optional, recommended)** — re-render the cockpit with the backendless Playwright route-mock harness (the `scratchpad/spa_server.py` + `shot.mjs` recipe, with `cockpit-meta.current_phase: "active"`) and eyeball the bottom panel + each tab for consistent margins.

- [ ] **Step 4: Commit any verification-driven fixes**, then hand off to subagent-driven-development's whole-branch review before merge.

---

## Self-Review

**Spec coverage:** Slice 2b scope (BottomPanel + Inbox/Problems/Activity/Library/Citations/Claims) all covered (T1–T7) + verification (T8). The spec's `data-category="member"` hook is in T4. Teardown groups (`bottom-panel*`/`problem(s)-*`/`activity-*`/`inbox-empty`) removed in their tasks; orphan panels (Library/Citations/Claims) explicitly no-teardown.

**Placeholder scan:** none — each task has a concrete mapping against the quoted current structure, exact test edits, and exact teardown + grep.

**Type/name consistency:** Mantine `Tabs` `value` = `BottomTab` union (`inbox|problems|activity|library|citations|claims`) via `onTab`. New module class `.activityRow` (with `[data-category="member"]`) referenced in ActivityPanel. `data-status`/`data-meta` preserved on Library badges; `data-category` added to Activity rows. After 2b, `cockpit.css` = only `source-viewer*` + `cite-composer*` (Slice 2c deletes the file).

**Risk note:** BottomPanel.test selects bottom tabs by `getByRole("tab",{name:/inbox 3/i})` (count in the name) and collapse by `getByRole("button",{name:/expand panel/i})` — Mantine `Tabs` + a plain `Button` satisfy both; keep the count suffix in each tab label. **List semantics are preserved** via `Stack component="ul"` + `component="li"` (CitationsPanel.test asserts `getAllByRole("listitem")` + error-before-warning order; the `RANK` sort is untouched) — this is the one place the 2b convention differs from 2a. Right-aligned trailing actions keep `ml="auto"` (old `.problem-act`/`.activity-act` `margin-left:auto`). `<h3>`/`<h4>` stay heading elements (`Text component="h3"` / `Title order={4}`). If any other orphan-panel test asserts a dropped class, switch it to a role/text/`data-*` query — never weaken the assertion.
