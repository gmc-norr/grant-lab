# Cockpit Mantine Migration — Slice 2a Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-skin the cockpit editor shell + artifact panels (Scorecard, Spec, Draft, Panel review, ProposalReview, the ArtifactSectionEditor grid, and the artifact tab strip) from legacy global CSS to Mantine + a scoped `CockpitPanels.module.css`, fixing the uneven panel margins.

**Architecture:** Every UI primitive becomes a Mantine component (`Title`/`Text`/`Button`/`Tabs`/`Stack`/`Group`/`Box`/`Flex`/`ScrollArea`); the irreducible visuals (band pills, diff colours, pre-wrap doc blocks, the editor-grid side-pane widths) move to a scoped CSS Module with `--app-*` tokens. A shared `BandBadge` component owns the band pill so ScorecardPanel **and** CockpitSidebar share one styling owner. As each component migrates, its rules are deleted from `styles/cockpit.css` (the file itself is deleted in Slice 2c, not here).

**Tech Stack:** React 19 + Mantine v9 + TanStack Query + Vite + Vitest. Spec: `docs/superpowers/specs/2026-06-27-cockpit-mantine-migration-design.md`.

## Global Constraints

Every task's requirements implicitly include this section.

- **Pure re-skin — NO behaviour, data-flow, or copy changes.** Every visible string stays verbatim (e.g. "No evaluation yet — Re-evaluate to generate a scorecard.", "Run full mock-panel review", "Cite selection", "Save section", "Pending proposals (N)").
- **Preserve ARIA contracts** — tests assert them:
  - Tab strips render `role="tab"` with `aria-selected` and accessible names matching the existing text → use Mantine **`Tabs`** (NOT `SegmentedControl`). Accessible names that tests match: `^draft$`, `^spec$`, `scorecard`, `panel review`, `^section$`, `full draft`, `full spec` (case-insensitive).
  - Keep `role="alert"` (save/stale/error banners), `role="status"` (lock banner, coherence, notices), `role="region"` + `aria-label` on the peek/pinned/composer panes (`citation source peek`, `pinned source`), `aria-busy="true"` on loading states.
- **Mantine + scoped module only** — no new global CSS. The module is `frontend/src/cockpit/CockpitPanels.module.css`; classes use `--app-*` tokens (copy the exact `color-mix(...)`/token values from `styles/cockpit.css`). No top-level bare element selectors in the module (descendant under a hashed class is fine, e.g. `.proposalDiff div`).
- **Stable test hooks** (the contract that survives class hashing): `data-band` on BandBadge, `data-region="peek"|"pinned"` on those panes, `data-diff="add"|"del"|"same"` on proposal diff rows.
- **cockpit.css teardown:** in each task, delete ONLY that component's rule block(s) from `frontend/src/styles/cockpit.css`. Do NOT delete the file or its `main.tsx:8` import (that is Slice 2c). `.band-*` rules are removed in Task 3 (after BOTH consumers migrate), not Task 2.
- **Test provider fix:** components that gain Mantine need `MantineProvider` in their tests. Where a test uses a bare `render(...)`, wrap its render tree's root in `<MantineProvider theme={theme} defaultColorScheme="dark">` (import `theme` from the test file's correct relative path to `theme/theme`), mirroring `routes/Cockpit.test.tsx`. The `document.fonts`/`matchMedia`/`ResizeObserver` stubs already live in `test-setup.ts` — no new setup.
- **Type gate is `tsc`** (no TS ESLint). Per-task verification: `pnpm exec tsc --noEmit` + the task's named test file(s). Run the FULL `pnpm test -- --run` + `pnpm build` once at the end of the slice (the migration touches shared trees; whole-suite is the real gate). Run all commands from `frontend/`.

---

### Task 1: `BandBadge` component + `CockpitPanels.module.css` foundation

**Files:**
- Create: `frontend/src/cockpit/BandBadge.tsx`
- Create: `frontend/src/cockpit/CockpitPanels.module.css`
- Test: `frontend/src/cockpit/BandBadge.test.tsx`

**Interfaces:**
- Produces: `BandBadge` — `function BandBadge({ band }: { band: ScorecardCriterion["band"] }): JSX.Element`. `ScorecardCriterion["band"]` = `"strong" | "adequate" | "at_risk" | "gap"` (from `../api/evaluator`). Renders a `<span data-band={band}>` with the label (`Strong`/`Adequate`/`At-risk`/`Gap`). Consumed by Task 2 (ScorecardPanel) and Task 3 (CockpitSidebar).
- Produces: `CockpitPanels.module.css` with class `bandBadge` + band classes `strong`, `adequate`, `at_risk`, `gap`. Later tasks append diff/doc/layout classes to this same file.

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/src/cockpit/BandBadge.test.tsx
import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { BandBadge } from "./BandBadge";

it("renders the human label and a stable data-band hook", () => {
  render(<BandBadge band="at_risk" />);
  const el = screen.getByText("At-risk");
  expect(el).toHaveAttribute("data-band", "at_risk");
});

it("maps every band to its label", () => {
  const { rerender } = render(<BandBadge band="strong" />);
  expect(screen.getByText("Strong")).toBeInTheDocument();
  rerender(<BandBadge band="gap" />);
  expect(screen.getByText("Gap")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm test -- --run src/cockpit/BandBadge.test.tsx`
Expected: FAIL — cannot resolve `./BandBadge`.

- [ ] **Step 3: Create the module CSS** (port the exact band rules from `styles/cockpit.css` lines 274-286; `bandBadge` = old `.band-pill`)

```css
/* frontend/src/cockpit/CockpitPanels.module.css
   Scoped cockpit visuals Mantine has no prop for. Tokens from theme/theme.css. */

/* Band pill — shared by ScorecardPanel + CockpitSidebar via <BandBadge>. */
.bandBadge {
  display: inline-block;
  font-size: 0.75em;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  padding: 0 0.4rem;
  border-radius: 999px;
  border: 1px solid transparent;
}
.strong {
  background: color-mix(in srgb, var(--app-good) 16%, transparent);
  color: var(--app-good);
  border-color: color-mix(in srgb, var(--app-good) 40%, transparent);
}
.adequate {
  background: var(--app-hover);
  color: var(--app-muted);
  border-color: var(--app-border);
}
.at_risk {
  background: color-mix(in srgb, var(--app-warn) 16%, transparent);
  color: var(--app-warn);
  border-color: color-mix(in srgb, var(--app-warn) 40%, transparent);
}
.gap {
  background: color-mix(in srgb, var(--app-bad) 16%, transparent);
  color: var(--app-bad);
  border-color: color-mix(in srgb, var(--app-bad) 40%, transparent);
}
```

- [ ] **Step 4: Create the component** (model on `components/MatchBadge.tsx`)

```tsx
// frontend/src/cockpit/BandBadge.tsx
import type { ScorecardCriterion } from "../api/evaluator";
import styles from "./CockpitPanels.module.css";

const BAND_LABEL: Record<ScorecardCriterion["band"], string> = {
  strong: "Strong",
  adequate: "Adequate",
  at_risk: "At-risk",
  gap: "Gap",
};

// Banded readiness pill, shared by the Scorecard and the sidebar Criteria list so
// the band styling has a single owner. data-band is the stable test/contract hook.
export function BandBadge({ band }: { band: ScorecardCriterion["band"] }) {
  return (
    <span className={`${styles.bandBadge} ${styles[band]}`} data-band={band}>
      {BAND_LABEL[band]}
    </span>
  );
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pnpm test -- --run src/cockpit/BandBadge.test.tsx`
Expected: PASS (4 assertions).

- [ ] **Step 6: Typecheck + commit**

Run: `pnpm exec tsc --noEmit` → clean.
```bash
git add frontend/src/cockpit/BandBadge.tsx frontend/src/cockpit/CockpitPanels.module.css frontend/src/cockpit/BandBadge.test.tsx
git commit -m "feat(cockpit): shared BandBadge + scoped CockpitPanels.module.css"
```

---

### Task 2: ScorecardPanel → Mantine (consumes BandBadge)

**Files:**
- Modify: `frontend/src/cockpit/ScorecardPanel.tsx`
- Modify: `frontend/src/styles/cockpit.css` (remove `.scorecard-*` rules, lines ~256-290 EXCEPT the `.band-*`/`.band-pill` block, which Task 3 removes)
- Test: `frontend/src/cockpit/ScorecardPanel.test.tsx`

**Interfaces:**
- Consumes: `BandBadge` from Task 1.

**Current structure** (`ScorecardPanel.tsx`): `<main className="scorecard-shell">` → `.scorecard-header` (`<h1>Scorecard</h1>` + overall `band-pill` + Re-evaluate `<button>` + error `<span>`), `.scorecard-meta` `<p>`, `.scorecard-summary` `<p>`, `.scorecard-empty` `<p>`, and `<ul className="scorecard-list">` of `<li className="scorecard-row">` (each: `band-pill`, `.scorecard-name` span, `.scorecard-weight` span, `.scorecard-link` button, `.scorecard-note` p).

- [ ] **Step 1: Update the test to provide Mantine context**

`ScorecardPanel.test.tsx:39` currently: `return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);`. Wrap in MantineProvider:

```tsx
// add imports at top:
import { MantineProvider } from "@mantine/core";
import { theme } from "../theme/theme";
// change the render helper body to:
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
    </MantineProvider>,
  );
```

- [ ] **Step 2: Run the test to confirm it fails on Mantine components not yet present**

Run: `pnpm test -- --run src/cockpit/ScorecardPanel.test.tsx`
Expected: PASS still (provider added, component unchanged) — this step just verifies the provider wrap didn't break the existing green test before the re-skin.

- [ ] **Step 3: Re-skin `ScorecardPanel.tsx`** to Mantine. Replace the render body:

```tsx
import { ActionIcon, Anchor, Badge, Box, Button, Group, Stack, Text, Title } from "@mantine/core";
// ...keep the existing hooks/imports; ADD:
import { BandBadge } from "./BandBadge";
// REMOVE the local BAND_CLASS/BAND_LABEL maps (BandBadge owns them now).
```

Render mapping (preserve all text + the `onNavigate` deep-link button):
- `<main className="scorecard-shell">` → `<Box component="main" p="md"><Stack gap="sm"> … </Stack></Box>`
- `.scorecard-header` → `<Group gap="sm" align="center">` containing `<Title order={2}>Scorecard</Title>`, `{data && <BandBadge band={data.scorecard.overall_band} />}`, `<Button size="xs" variant="default" disabled={...} onClick={...}>{running ? "Evaluating…" : "Re-evaluate"}</Button>`, and the two error `<Text c="red" role="status" size="sm">`.
- `.scorecard-meta`/`.scorecard-summary`/`.scorecard-empty` `<p>` → `<Text size="sm" c="dimmed">` (keep the stale `<span>` as `<Text span c="yellow">`; preserve "⚠ changed since:" text).
- `<ul className="scorecard-list">` → `<Stack gap="xs" component="ul" style={{ listStyle: "none", padding: 0, margin: 0 }}>` — OR simpler: `<Stack gap="xs">` of plain rows (drop the `<ul>/<li>` semantics; the list is presentational). Each row → `<Group gap="sm" align="flex-start" wrap="nowrap">` with `<BandBadge band={c.band} />`, `<Text fw={600} size="sm">{c.name}</Text>`, `<Text size="xs" c="dimmed">w {c.weight}</Text>`, the deep-link `<Anchor component="button" size="xs" onClick={...}>Go to §{...}</Anchor>`, and the note as `<Text size="sm" style={{ whiteSpace: "pre-wrap" }}>{c.note}</Text>` (put the note on its own line under the group inside a per-row `<Stack gap={2}>`).

Keep the `criteria` sort and all conditional rendering identical.

- [ ] **Step 4: Remove `.scorecard-*` rules from `styles/cockpit.css`**

Delete the block under `/* Scorecard tab … */` — the `.scorecard-shell/-header/-header h1/-meta/-summary/-empty/-list/-name/-weight/-link/-row/-note/-stale/-error` rules. **Keep** `.band-pill` and `.band-strong/-adequate/-at-risk/-gap` (Task 3 removes those). Verify: `grep -n "scorecard-" frontend/src/styles/cockpit.css` → no matches.

- [ ] **Step 5: Run the test + typecheck**

Run: `pnpm test -- --run src/cockpit/ScorecardPanel.test.tsx` → PASS (text assertions: "Quality", "Impact", "Mostly there.", band labels survive via BandBadge).
Run: `pnpm exec tsc --noEmit` → clean.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/cockpit/ScorecardPanel.tsx frontend/src/cockpit/ScorecardPanel.test.tsx frontend/src/styles/cockpit.css
git commit -m "feat(cockpit): ScorecardPanel -> Mantine + BandBadge"
```

---

### Task 3: CockpitSidebar band pill → BandBadge (remove global `.band-*`)

**Files:**
- Modify: `frontend/src/cockpit/CockpitSidebar.tsx` (only the band span + its now-dead maps)
- Modify: `frontend/src/styles/cockpit.css` (remove `.band-pill` + `.band-*`)
- Test: `frontend/src/cockpit/CockpitSidebar.test.tsx` (already wraps in MantineProvider; expected to stay green)

**Interfaces:**
- Consumes: `BandBadge` from Task 1.

- [ ] **Step 1: Swap the band span** in `CockpitSidebar.tsx`

- Add `import { BandBadge } from "./BandBadge";`
- Delete the `BAND_CLASS` and `BAND_LABEL` consts (lines 18-29) — BandBadge owns them. Keep the `ScorecardCriterion` type import (still used elsewhere? if not, drop it to keep tsc clean).
- Replace line 134 `<span className={`band-pill ${BAND_CLASS[c.band]}`}>{BAND_LABEL[c.band]}</span>` with `<BandBadge band={c.band} />`.

- [ ] **Step 2: Remove the global band rules** from `styles/cockpit.css`

Delete `.band-pill` and `.band-strong/-adequate/-at-risk/-gap`. Verify both consumers are migrated: `grep -rn "band-pill\|band-strong\|band-adequate\|band-at-risk\|band-gap" frontend/src --include="*.tsx"` → no matches (Task 2 already removed ScorecardPanel's). Then `grep -n "band-" frontend/src/styles/cockpit.css` → no matches.

- [ ] **Step 3: Run the test + typecheck**

Run: `pnpm test -- --run src/cockpit/CockpitSidebar.test.tsx` → PASS (`getByText("Gap")` at line 144 survives — BandBadge renders the label).
Run: `pnpm exec tsc --noEmit` → clean.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/cockpit/CockpitSidebar.tsx frontend/src/styles/cockpit.css
git commit -m "feat(cockpit): sidebar criteria use BandBadge; drop global .band-* rules"
```

---

### Task 4: Cockpit shell + artifact tab strip → Mantine `Tabs`

**Files:**
- Modify: `frontend/src/routes/Cockpit.tsx`
- Modify: `frontend/src/styles/cockpit.css` (remove `.cockpit-shell` + its `[role=tablist]` rules, lines ~157-185)
- Test: `frontend/src/routes/Cockpit.test.tsx` (already wraps in MantineProvider; the tab assertions use `getByRole("tab", …)` + `aria-selected`, which Mantine `Tabs` satisfies)

**Current structure** (`Cockpit.tsx:54-84`): `<div className="cockpit-shell">` with `<div role="tablist" aria-label="Artifact">` of four `<button role="tab" aria-selected onClick={() => onTab(...)}>` (Draft/Spec/Scorecard/Panel review), then the conditional panels, then `<BottomPanel/>`.

- [ ] **Step 1: Re-skin to Mantine `Tabs`**

```tsx
import { Box, Tabs } from "@mantine/core";
// ...
  return (
    <Box className="cockpit-shell-fallback" style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
      <UnsavedChangesGuard />
      <Tabs
        value={tab}
        onChange={(v) => v && onTab(v as TopTab)}
        aria-label="Artifact"
        keepMounted={false}
      >
        <Tabs.List>
          <Tabs.Tab value="draft">Draft</Tabs.Tab>
          <Tabs.Tab value="spec">Spec</Tabs.Tab>
          <Tabs.Tab value="scorecard">Scorecard</Tabs.Tab>
          <Tabs.Tab value="review">Panel review</Tabs.Tab>
        </Tabs.List>
      </Tabs>
      {tab === "draft" && <DraftPanel id={id} openPeek={openPeek} />}
      {tab === "spec" && <SpecPanel id={id} />}
      {tab === "scorecard" && <ScorecardPanel id={id} onNavigate={onNavigate} />}
      {tab === "review" && <ReviewPanel id={id} />}
      <BottomPanel
        id={id}
        activeTab={bottomTab}
        onTab={setBottomTab}
        collapsed={collapsed}
        onToggleCollapsed={() => setCollapsed((c) => !c)}
        onNavigate={onNavigate}
      />
    </Box>
  );
```

Notes: `onChange` keeps the existing `onTab` (which runs `confirmDiscardIfDirty`). `Tabs.Tab value` strings (`draft/spec/scorecard/review`) match the existing `TopTab` union and the accessible names are the visible labels (so `getByRole("tab",{name:/^spec$/i})` etc. resolve). The panels render OUTSIDE `Tabs.Panel` (unchanged conditional pattern), so the dirty-confirm gate still controls switching — do NOT move them into `Tabs.Panel`. The loading/setup early-returns (`cockpit-loading`, `WorkspaceSetup`) are unchanged.

- [ ] **Step 2: Remove `.cockpit-shell` rules** from `styles/cockpit.css`

Delete `.cockpit-shell`, `.cockpit-shell > [role="tablist"]`, and the `… button[role="tab"]` (+ `:hover`, `[aria-selected="true"]`) rules. (Leave `.spec-empty` for Task 6.) Verify: `grep -n "cockpit-shell" frontend/src/styles/cockpit.css` → no matches.

- [ ] **Step 3: Run Cockpit.test + typecheck**

Run: `pnpm test -- --run src/routes/Cockpit.test.tsx` → the artifact-tab assertions (`/^spec$/i`, `/^draft$/i` + `aria-selected`) pass under Mantine Tabs. (If any assertion relied on the literal `<button>` wrapper rather than `role="tab"`, update it to `getByRole("tab", …)` — do not weaken the assertion.)
Run: `pnpm exec tsc --noEmit` → clean.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/routes/Cockpit.tsx frontend/src/styles/cockpit.css frontend/src/routes/Cockpit.test.tsx
git commit -m "feat(cockpit): artifact tab strip -> Mantine Tabs"
```

---

### Task 5: ArtifactSectionEditor → Mantine (grid → `Flex`, view toggle → `Tabs`, data-region hooks)

**Files:**
- Modify: `frontend/src/editor/ArtifactSectionEditor.tsx`
- Modify: `frontend/src/cockpit/CockpitPanels.module.css` (append editor-layout classes)
- Modify: `frontend/src/styles/cockpit.css` (remove `.cockpit`, `.cockpit-main`, `.cockpit-peek`, `.cockpit-pinned`, `.cockpit-composer`, `.cockpit-section-actions`, `.cite-selection-btn`, and the `.cockpit-main > [role=tablist]` rules)
- Test: `frontend/src/editor/ArtifactSectionEditor.test.tsx` (add MantineProvider), `frontend/src/routes/Cockpit.test.tsx` (update `.cockpit-pinned`/`.cockpit-peek` selectors to `data-region`)

**Current structure** (`ArtifactSectionEditor.tsx:195-334`): `<main className="cockpit">` (3-col grid) → `<section className="cockpit-main">` containing the Section/Full `<div role="tablist" aria-label="View">` toggle, the stale/save `<p role="alert">` banners, `<CodeMirrorEditor/>`, and the `.cockpit-section-actions` `<div>` (Save section + `.cite-selection-btn` Cite selection). Then `<section className="cockpit-peek" aria-label="Citation source peek">`, `<aside className="cockpit-pinned" aria-label="Pinned source">`, `<section className="cockpit-composer">`.

- [ ] **Step 1: Append editor-layout classes to the module** (port widths from `cockpit.css` 16-20, 114-117)

```css
/* --- ArtifactSectionEditor layout (the old .cockpit grid) --- */
.editorGrid {
  display: flex;
  gap: 1rem;
  align-items: flex-start;
}
.editorMain {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  flex: 1;
  min-width: 0;
}
.sidePane {
  min-width: 18rem;
  max-width: 28rem;
}
.composerPane {
  min-width: 20rem;
  max-width: 30rem;
}
```

- [ ] **Step 2: Re-skin `ArtifactSectionEditor.tsx`**

- Container: `<main className="cockpit">` → `<Box component="main" className={styles.editorGrid} p="md">` (import `styles` from `../cockpit/CockpitPanels.module.css`, and Mantine `Box`, `Button`, `Tabs`, `Text`).
- `<section className="cockpit-main">` → `<Box component="section" className={styles.editorMain}>`.
- View toggle `<div role="tablist" aria-label="View">` (Section / {fullViewLabel}) → Mantine `Tabs`:
  ```tsx
  <Tabs value={view} onChange={(v) => v && setView(v as "section" | "full")} aria-label="View">
    <Tabs.List>
      <Tabs.Tab value="section">Section</Tabs.Tab>
      <Tabs.Tab value="full">{fullViewLabel}</Tabs.Tab>
    </Tabs.List>
  </Tabs>
  ```
  (Accessible names `Section` / `Full draft` / `Full spec` satisfy the `getByRole("tab",…)` assertions in Cockpit.test:234/241.)
- Stale + save `<p role="alert">` banners → `<Text role="alert" c="red" size="sm">…</Text>` with the inline `<Button size="xs" variant="subtle">` for Reload/Keep/Dismiss. Preserve the exact strings.
- `.cockpit-section-actions` `<div>` → `<Group gap="sm">`; "Save section" `<button>` → `<Button size="xs" disabled={...}>`; "Cite selection" `<button className="cite-selection-btn">` → `<Button size="xs" variant="default" disabled={...} title={...}>` (drop the `cite-selection-btn` class — the `Group` gap replaces its `margin-left`).
- Peek pane: `<section className="cockpit-peek" aria-label="Citation source peek">` → `<Box component="section" className={styles.sidePane} data-region="peek" aria-label="Citation source peek">`. Keep `role="region"` semantics — `aria-label` on a `<section>` already yields `role="region"`; verify the Cockpit.test `getByRole("region",{name:/citation source peek/i})` still resolves (it will, `<section aria-label>` = region).
- Pinned pane: `<aside className="cockpit-pinned" aria-label="Pinned source">` → `<Box component="aside" className={styles.sidePane} data-region="pinned" aria-label="Pinned source">`.
- Composer pane: `<section className="cockpit-composer">` → `<Box component="section" className={styles.composerPane}>`.
- Inner loading/error `<p aria-busy>`/`<p role="alert">` → `<Text aria-busy>` / `<Text role="alert" c="red">` (preserve strings + `aria-busy`).

- [ ] **Step 3: Add MantineProvider to `ArtifactSectionEditor.test.tsx`**

Both render trees (lines ~21 and ~90) wrap `<MemoryRouter>…</MemoryRouter>` directly. Wrap each root in `<MantineProvider theme={theme} defaultColorScheme="dark">` (add `import { MantineProvider } from "@mantine/core";` and `import { theme } from "../theme/theme";`). The `.cm-content` assertion (CodeMirror) and the `region` "citation source peek" assertion are unaffected.

- [ ] **Step 4: Update the coupled selectors in `routes/Cockpit.test.tsx`**

- Line ~490: `container.querySelector(".cockpit-pinned .source-viewer")` → `container.querySelector('[data-region="pinned"] .source-viewer')` (SourceViewer keeps `.source-viewer` until Slice 2c, so the nested selector still resolves).
- Line ~491: `container.querySelector(".cockpit-peek")` → `container.querySelector('[data-region="peek"]')`.
- Leave `.source-viewer mark` (line ~476) and `.cm-citation-pill` (line ~472) unchanged.

- [ ] **Step 5: Remove the editor-grid rules from `styles/cockpit.css`**

Delete `.cockpit`, `.cockpit-main`, `.cockpit-peek`, `.cockpit-pinned`, `.cockpit-section-actions`, `.cite-selection-btn`, `.cockpit-composer`, and `.cockpit-main > [role="tablist"]` (+ its `button[role="tab"]` variants). Verify: `grep -nE "\.cockpit($|[ .{,>])|cockpit-main|cockpit-peek|cockpit-pinned|cockpit-composer|cockpit-section-actions|cite-selection-btn" frontend/src/styles/cockpit.css` → no matches.

- [ ] **Step 6: Run both tests + typecheck**

Run: `pnpm test -- --run src/editor/ArtifactSectionEditor.test.tsx src/routes/Cockpit.test.tsx` → PASS.
Run: `pnpm exec tsc --noEmit` → clean.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/editor/ArtifactSectionEditor.tsx frontend/src/cockpit/CockpitPanels.module.css frontend/src/styles/cockpit.css frontend/src/editor/ArtifactSectionEditor.test.tsx frontend/src/routes/Cockpit.test.tsx
git commit -m "feat(cockpit): ArtifactSectionEditor -> Mantine Flex/Tabs + data-region hooks"
```

---

### Task 6: SpecPanel → Mantine

**Files:**
- Modify: `frontend/src/cockpit/SpecPanel.tsx`
- Modify: `frontend/src/styles/cockpit.css` (remove `.spec-empty`, `.spec-shell`, `.spec-lock-banner`, `.spec-lock-bar`, `.coherence-bar`, `.coherence-status`, `.coherence-error`)
- Test: `frontend/src/cockpit/SpecPanel.test.tsx` (add MantineProvider)

**Current structure** (`SpecPanel.tsx`): loading `<main aria-busy>`, error `<main role="alert"><h1>`, empty `<main className="spec-empty"><h1>Spec</h1><p>…</p><button>`, else `<div className="spec-shell">` with `.spec-lock-banner` `<p role="status">`, `.spec-lock-bar` `<div>` (Lock/Unlock buttons), `.coherence-bar` `<div>` (Check-coherence button + `.coherence-status`/`.coherence-error` `<span role="status">`), then `<ArtifactSectionEditor … fullViewLabel="Full spec" />`.

- [ ] **Step 1: Add MantineProvider to `SpecPanel.test.tsx`**

The test renders the real SpecPanel (which renders the real ArtifactSectionEditor → now Mantine) and asserts the "Full spec" tab + "unlock spec" button. Wrap its `render(` tree root (line ~17) in `<MantineProvider theme={theme} defaultColorScheme="dark">` (add the imports). If the test finds the tab via `getByRole("tab", {name:/full spec/i})` it already aligns with Task 5's Tabs; if it used `getByText`, leave it (still resolves).

- [ ] **Step 2: Re-skin `SpecPanel.tsx`**

- Loading: `<main aria-busy="true">Loading spec…</main>` → `<Box component="main" p="md"><Text aria-busy="true">Loading spec…</Text></Box>`.
- Error: `<main role="alert"><h1>Could not load the spec</h1></main>` → `<Box component="main" p="md"><Title order={2} role="alert">Could not load the spec</Title></Box>`.
- Empty: `<main className="spec-empty">` → `<Box component="main" p="md"><Stack gap="sm" align="flex-start"><Title order={2}>Spec</Title><Text>This proposal has no spec yet.</Text><Button disabled={...} onClick={() => init.mutate()}>Start spec from skeleton</Button></Stack></Box>`.
- Main: `<div className="spec-shell">` → `<Stack gap="sm">`. Lock banner `<p className="spec-lock-banner" role="status">` → `<Alert role="status" color="teal" variant="light">🔒 Spec locked…</Alert>` (preserve the dynamic `by … on …` text). Lock bar `<div className="spec-lock-bar">` → `<Group gap="sm">` with `<Button size="xs" variant="default">` Lock/Unlock. Coherence bar `<div className="coherence-bar">` → `<Group gap="sm" align="center">` with the Check-coherence `<Button size="xs">` and `<Text role="status" size="sm" c="dimmed">` (error variant `c="red"`).
- Keep `<ArtifactSectionEditor … />` unchanged.
- Imports: `import { Alert, Box, Button, Group, Stack, Text, Title } from "@mantine/core";`

- [ ] **Step 3: Remove `.spec-*`/`.coherence-*` rules** from `styles/cockpit.css`. Verify: `grep -nE "spec-empty|spec-shell|spec-lock|coherence-" frontend/src/styles/cockpit.css` → no matches.

- [ ] **Step 4: Run test + typecheck**

Run: `pnpm test -- --run src/cockpit/SpecPanel.test.tsx` → PASS.
Run: `pnpm exec tsc --noEmit` → clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/cockpit/SpecPanel.tsx frontend/src/cockpit/SpecPanel.test.tsx frontend/src/styles/cockpit.css
git commit -m "feat(cockpit): SpecPanel -> Mantine"
```

---

### Task 7: DraftPanel → Mantine (wrapper + error states)

**Files:**
- Modify: `frontend/src/cockpit/DraftPanel.tsx`
- Modify: `frontend/src/styles/cockpit.css` (remove `.draft-shell`)
- Test: `frontend/src/cockpit/DraftPanel.test.tsx` (already uses `renderWithProviders` + mocks ArtifactSectionEditor — expected green, no change)

**Current structure:** DraftPanel already uses Mantine `Button`/`Menu`. Only the outer `<div className="draft-shell">`, the `.draft-toolbar` `<div>` (orphan class), the `.export-msg` `<span>` (orphan), and the two raw error/loading `<main>`/`<h1>` blocks remain.

- [ ] **Step 1: Re-skin the non-Mantine bits**

- Loading: `<main aria-busy="true">Loading cockpit…</main>` → `<Box component="main" p="md"><Text aria-busy="true">Loading cockpit…</Text></Box>`.
- Error: `<main role="alert"><h1>Could not load this workspace</h1></main>` → `<Box component="main" p="md"><Title order={2} role="alert">Could not load this workspace</Title></Box>`.
- `<div className="draft-shell">` → `<Stack gap="sm">`; `<div className="draft-toolbar">` → `<Group gap="sm">`; `<span role="status" className="export-msg">` → `<Text role="status" size="sm" c="dimmed">`.
- Keep the `Menu`/`Button` export control and `<ArtifactSectionEditor … />` unchanged.
- Imports: add `Box, Group, Stack, Text, Title` to the existing `@mantine/core` import.

- [ ] **Step 2: Remove `.draft-shell`** from `styles/cockpit.css`. Verify: `grep -n "draft-shell" frontend/src/styles/cockpit.css` → no matches. (`.draft-toolbar`/`.export-msg` are orphans — not in cockpit.css — nothing to remove.)

- [ ] **Step 3: Run test + typecheck**

Run: `pnpm test -- --run src/cockpit/DraftPanel.test.tsx` → PASS (mocks the editor; asserts "editor" + export).
Run: `pnpm exec tsc --noEmit` → clean.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/cockpit/DraftPanel.tsx frontend/src/styles/cockpit.css
git commit -m "feat(cockpit): DraftPanel wrapper + states -> Mantine"
```

---

### Task 8: ReviewPanel → Mantine (status Badges + `<pre>` doc in module)

**Files:**
- Modify: `frontend/src/cockpit/ReviewPanel.tsx`
- Modify: `frontend/src/cockpit/CockpitPanels.module.css` (append `.reviewDoc` pre block)
- Test: `frontend/src/cockpit/ReviewPanel.test.tsx` (add MantineProvider)

**Current structure:** ReviewPanel's ~19 classnames are ALL orphans (styled nowhere) — so this is a pure Mantine conversion with NO `cockpit.css` teardown. `StatusPill` (review-status-pill + STATUS_CLASS map), `ItemRow` `<li>`, `ReviewDoc` (`<pre className="review-doc">`), plus the header/controls/budget/banner/items/meta/empty blocks.

- [ ] **Step 1: Add MantineProvider to `ReviewPanel.test.tsx`** (line ~70 `render(<QueryClientProvider…>)`): wrap in `<MantineProvider theme={theme} defaultColorScheme="dark">` (add imports).

- [ ] **Step 2: Append `.reviewDoc` to the module**

```css
/* --- ReviewPanel preformatted markdown doc --- */
.reviewDoc {
  white-space: pre-wrap;
  font-size: 0.85rem;
  margin: 0;
  padding: 0.5rem;
  border: 1px solid var(--app-border);
  border-radius: 6px;
  background: var(--app-sidebar);
  overflow: auto;
}
```

- [ ] **Step 3: Re-skin `ReviewPanel.tsx`**

- Replace `STATUS_CLASS` + `StatusPill` with a Mantine `Badge` mapping: `const STATUS_COLOR: Record<string,string> = { done:"teal", running:"blue", queued:"gray", failed:"red", cancelled:"gray", partial:"yellow" };` and `function StatusPill({status}) { return <Badge size="sm" variant="light" color={STATUS_COLOR[status] ?? "gray"}>{status}</Badge>; }` (keep rendering the raw `status` text — tests/usages key off it).
- `ItemRow` `<li>` → `<Group gap="sm" component="li" style={{listStyle:"none"}}>` (or a `<Group>` row inside a `<Stack component="ul">`); keep `review-item-kind`/`error` text as `<Text size="sm">`/`<Text size="sm" c="red">`.
- `ReviewDoc`: `<pre className="review-doc">` → `<pre className={styles.reviewDoc}>`; `<h2 className="review-doc-title">` → `<Title order={3}>`; loading `<p>` → `<Text size="sm" c="dimmed">`. Wrap section in `<Stack gap="xs" component="section">`.
- Main `<main className="review-shell">` → `<Box component="main" p="md"><Stack gap="sm">…</Stack></Box>`. Header `<h1>Panel review</h1>` → `<Title order={2}>`; stale `<span>` → `<Badge color="yellow" variant="light">stale</Badge>`. Controls → `<Group gap="sm">` + `<Button>` "Run full mock-panel review"; disabled-reason/start-error → `<Text role="status"/role="alert" size="sm" c="dimmed"/"red">`. Budget/meta `<p>` → `<Text size="sm" c="dimmed">`. Banners `<div role="status">` → `<Alert role="status" variant="light">`. Items `<ul>` → `<Stack gap="xs">`. Empty `<p>` → `<Text size="sm" c="dimmed">`. Preserve ALL strings + the `<strong>{run.status}</strong>` emphasis (use `<Text span fw={700}>`).
- Imports: `import { Alert, Badge, Box, Button, Group, Stack, Text, Title } from "@mantine/core";` + `import styles from "./CockpitPanels.module.css";`

- [ ] **Step 4: Run test + typecheck**

Run: `pnpm test -- --run src/cockpit/ReviewPanel.test.tsx` → PASS.
Run: `pnpm exec tsc --noEmit` → clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/cockpit/ReviewPanel.tsx frontend/src/cockpit/CockpitPanels.module.css frontend/src/cockpit/ReviewPanel.test.tsx
git commit -m "feat(cockpit): ReviewPanel -> Mantine (status Badges + scoped doc)"
```

---

### Task 9: ProposalReview → Mantine + `data-diff` hooks

**Files:**
- Modify: `frontend/src/cockpit/ProposalReview.tsx`
- Modify: `frontend/src/cockpit/CockpitPanels.module.css` (append diff styles)
- Modify: `frontend/src/styles/cockpit.css` (remove `.proposal-*`)
- Test: `frontend/src/cockpit/ProposalReview.test.tsx` (add MantineProvider + switch `toHaveClass` → `toHaveAttribute("data-diff", …)`)

**Current structure** (`ProposalReview.tsx:88-126`): `<section className="proposal-review" aria-label="Pending proposals">` → `.proposal-review-title` `<h2>`, `.proposal-review-notice` `<p role="status">` (orphan class), `<ul>` of `.proposal-item` `<li>` (each: `.proposal-meta` `<p>`, `<pre className="proposal-diff">` of `<div className={`diff-${tag}`}>` rows, `.proposal-actions` `<div>` of Accept/Reject `<button>`).

- [ ] **Step 1: Update `ProposalReview.test.tsx`**

- Wrap the `render(` tree (line ~14) root in `<MantineProvider theme={theme} defaultColorScheme="dark">` (add imports).
- Lines 73-74: change
  ```tsx
  expect(del).toHaveClass("diff-del");
  expect(screen.getByText("+ better.")).toHaveClass("diff-add");
  ```
  to
  ```tsx
  expect(del).toHaveAttribute("data-diff", "del");
  expect(screen.getByText("+ better.")).toHaveAttribute("data-diff", "add");
  ```
  (`del` is `await screen.findByText("- orig.")` — the diff `<div>` carrying the attribute.)

- [ ] **Step 2: Append diff styles to the module**

```css
/* --- ProposalReview diff --- */
.proposalDiff {
  margin: 0;
  white-space: pre-wrap;
  font-size: 0.85rem;
}
.proposalDiff div[data-diff="add"] { background: rgba(46, 160, 67, 0.15); }
.proposalDiff div[data-diff="del"] { background: rgba(248, 81, 73, 0.15); }
```

- [ ] **Step 3: Re-skin `ProposalReview.tsx`**

- `<section className="proposal-review" aria-label="Pending proposals">` → `<Box component="section" aria-label="Pending proposals" p="sm" style={{border:"1px solid var(--app-border)", borderRadius:6}}><Stack gap="sm">…</Stack></Box>` (keep the bordered card look via inline tokens — minor).
- `<h2 className="proposal-review-title">` → `<Title order={3}>`. Notice `<p role="status">` → `<Text role="status" size="sm" c="dimmed">`.
- `<ul>` → `<Stack gap="sm">`; `.proposal-item` `<li>` → `<Box component="li" style={{listStyle:"none", borderTop:"1px solid var(--app-border)", paddingTop:"0.5rem"}}>` (or a `<Stack gap="xs">`).
- `.proposal-meta` `<p>` → `<Text size="xs" c="dimmed">`.
- The diff: `<pre className="proposal-diff">` → `<pre className={styles.proposalDiff}>`; each `<div className={`diff-${r.tag}`}>` → `<div data-diff={r.tag}>` (drop the className; the module styles by `[data-diff]`). Keep the `(r.tag === "add" ? "+ " : …)` text prefix logic.
- `.proposal-actions` `<div>` → `<Group gap="sm">`; Accept/Reject `<button>` → `<Button size="xs">` (Accept) / `<Button size="xs" variant="default">` (Reject), preserving `disabled`/`title`/`onClick`.
- Imports: `import { Box, Button, Group, Stack, Text, Title } from "@mantine/core";` + `import styles from "./CockpitPanels.module.css";`

- [ ] **Step 4: Remove `.proposal-*` rules** from `styles/cockpit.css` (including `.proposal-diff .diff-add/.diff-del`). Verify: `grep -nE "proposal-|diff-add|diff-del" frontend/src/styles/cockpit.css` → no matches.

- [ ] **Step 5: Run test + typecheck**

Run: `pnpm test -- --run src/cockpit/ProposalReview.test.tsx` → PASS (data-diff attributes).
Run: `pnpm exec tsc --noEmit` → clean.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/cockpit/ProposalReview.tsx frontend/src/cockpit/CockpitPanels.module.css frontend/src/cockpit/ProposalReview.test.tsx frontend/src/styles/cockpit.css
git commit -m "feat(cockpit): ProposalReview -> Mantine + data-diff hooks"
```

---

### Task 10: Slice 2a verification gate

**Files:** none (verification only).

- [ ] **Step 1: Full type + test + build**

Run (from `frontend/`):
- `pnpm exec tsc --noEmit` → clean.
- `pnpm test -- --run` → ALL green (the migration touched shared trees; whole-suite is the gate; per the project note, whole-suite vitest is reliable — only backend testcontainers flakes).
- `pnpm build` → bundle builds.

- [ ] **Step 2: Confirm partial teardown is consistent**

Run: `grep -nE "scorecard-|band-pill|band-strong|spec-shell|spec-lock|coherence-|draft-shell|proposal-|cockpit-shell|\.cockpit($|[ .{,>])|cockpit-main|cockpit-peek|cockpit-pinned|cockpit-composer|cockpit-section-actions|cite-selection-btn" frontend/src/styles/cockpit.css` → no matches (all 2a class groups removed). The remaining `cockpit.css` should contain only Slice-2b/2c rules (bottom-panel, problems, activity, inbox, source-viewer, cite-composer). `cockpit.css` is NOT deleted here.

- [ ] **Step 3: Visual check (optional but recommended)**

Re-render the cockpit setup→scorecard surface with the backendless Playwright route-mock harness (the `scratchpad/spa_server.py` + `shot.mjs` recipe) and eyeball that headings/tabs/panels share one left margin and the band pills/diffs render correctly.

- [ ] **Step 4: Commit any verification-driven fixes**, then hand off to subagent-driven-development's whole-branch review before merge.

---

## Self-Review

**Spec coverage:** Slice 2a scope from the spec = BandBadge + module foundation (T1), ScorecardPanel (T2), CockpitSidebar band pill (T3), Cockpit shell + artifact Tabs (T4), ArtifactSectionEditor (T5), SpecPanel (T6), DraftPanel (T7), ReviewPanel (T8), ProposalReview (T9), verification (T10). All covered. The spec's `data-region`/`data-diff`/`data-band` hooks are in T5/T9/T1. The cockpit.css teardown ordering (band-* after both consumers) is enforced (T2 keeps, T3 removes).

**Placeholder scan:** none — every code/test/CSS change has concrete content or an exact mapping against quoted current structure.

**Type/name consistency:** `BandBadge({band})` signature is identical in T1 (produce), T2/T3 (consume). `ScorecardCriterion["band"]` union (`strong|adequate|at_risk|gap`) matches the module class names (`.strong/.adequate/.at_risk/.gap`) accessed via `styles[band]`. Mantine `Tabs` `value` strings match the `TopTab` union (`draft|spec|scorecard|review`) and the `view` union (`section|full`). Module class names referenced in components (`editorGrid`, `editorMain`, `sidePane`, `composerPane`, `reviewDoc`, `proposalDiff`, `bandBadge`) all exist in the module after their creating tasks.

**Risk note:** `routes/Cockpit.test.tsx` is touched by T4 (artifact Tabs) and T5 (data-region) — both run it. If T4's tab assertions or T5's region/`source-viewer` nested selectors surface an unforeseen coupling, fix the assertion to the stable role/`data-*` (never weaken it) within that task's loop.
