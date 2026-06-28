# Cockpit Mantine Migration — Slice 2c Implementation Plan (FINAL cockpit slice)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-skin the last two cockpit widgets (SourceViewer, CitationComposer) + tidy the two already-Mantine modals to Mantine + the scoped `CockpitPanels.module.css`, then **delete `frontend/src/styles/cockpit.css` and its `main.tsx` import** — completing the cockpit's removal of legacy global CSS.

**Architecture:** Continues Slices 2a (`12cc2e9`) + 2b (`80c2d3b`). `cockpit.css` currently holds ONLY the `source-viewer*` and `cite-composer*` groups; Task 1 removes the former, Task 2 the latter, Task 4 deletes the now-empty file + its import. Same bar as 2a/2b: Mantine primitives + scoped module for the irreducible visuals (the `<mark>` highlight, pre-wrap bodies, stance pill, the selectable chunk `<pre>`), stable `data-*` hooks for tests.

**Tech Stack:** React 19 + Mantine v9 + TanStack Query + Vite + Vitest. Spec: `docs/superpowers/specs/2026-06-27-cockpit-mantine-migration-design.md`.

## Global Constraints

Every task's requirements implicitly include this section. (Same bar as 2a/2b.)

- **Pure re-skin — NO behaviour, data-flow, or copy changes.** Every visible string verbatim ("This reference has been removed.", "The source text isn’t available yet.", "Pin", "Cite:", "Pick a reference:", "Add citation", "‹ back to references", "This source isn’t indexed yet.", etc.).
- **Preserve ARIA + DOM contracts** — tests assert them:
  - SourceViewer keeps a real `<mark>` for the highlight (`SourceViewer.test.tsx` queries `querySelector("mark")`). Add `data-testid="source-viewer"` to the SourceViewer root (`Cockpit.test.tsx` nests selectors on it — see Task 1).
  - Keep `aria-label`s ("Cited source", "Previous citation", "Next citation", "Pin source", "Close source", "Add a citation", "Cancel"), `role="note"` (tombstone), `role="alert"` (composer error), `aria-busy="true"` (loading).
  - **CitationComposer's chunk `<pre>` MUST stay a single-text-node `<pre>`** rendering `{c.text}` directly with the `onMouseUp={captureSelection}` handler. Its offset math (`measureUtf16Offset` walks from the `<pre>` start) breaks if any sibling element is interpolated inside the `<pre>`. Only swap its `className` to the module class — do NOT wrap its text in Mantine components.
  - Preserve `<h?>`/heading + list semantics where present (per the 2b convention): `Stack component="ul"` + `component="li"` for any `<ul>/<li>`.
- **Mantine + the EXISTING scoped module only** — append to `frontend/src/cockpit/CockpitPanels.module.css`; classes use `--app-*` tokens (copy the exact `color-mix(...)`/token values from the `cockpit.css` rules being ported). Module classes are unlayered → override Mantine's `@layer mantine` without `!important`. No top-level bare element selectors (descendant under a hashed class — e.g. `.sourceBody mark` — is fine).
- **`cockpit.css` teardown:** Task 1 removes `source-viewer*`, Task 2 removes `cite-composer*`. After Task 2 the file is empty. Task 4 deletes the file AND the `import "./styles/cockpit.css";` line at `main.tsx:8`, AFTER a repo-wide grep confirms no `.tsx` references any of its (now-migrated) classnames.
- **Test provider fix:** `SourceViewer.test.tsx` and (where it renders the component) `CitationComposer.test.tsx` render WITHOUT MantineProvider → wrap their render tree root in `<MantineProvider theme={theme} defaultColorScheme="dark">` (import `theme` from the correct relative path). `WorkspaceSettingsModal.test.tsx` already uses `renderWithProviders`; MetadataModal has no test. The CitationComposer offset-helper test that builds a raw `<pre>` in the DOM directly needs NO change.
- **Type gate is `tsc`** (no TS ESLint; `noUnusedLocals: true` — import only what you use). Per-task: `pnpm exec tsc --noEmit` + the task's named test file(s). Slice end (Task 4): full `pnpm test -- --run` + `pnpm build` + the deletion-verify grep. All commands from `frontend/`.

---

### Task 1: SourceViewer → Mantine + module (`<mark>`/body/stance/tombstone) + `data-testid`

**Files:**
- Modify: `frontend/src/cockpit/SourceViewer.tsx`
- Modify: `frontend/src/cockpit/CockpitPanels.module.css` (append source classes)
- Modify: `frontend/src/styles/cockpit.css` (remove ALL `source-viewer*` rules)
- Test: `frontend/src/cockpit/SourceViewer.test.tsx` (add MantineProvider), `frontend/src/routes/Cockpit.test.tsx` (`.source-viewer` → `[data-testid="source-viewer"]`)

**Current structure:** `<aside className="source-viewer" aria-label="Cited source">` → `.source-viewer-head` (title span, pos span, `.source-viewer-actions` div of prev/next/pin/close `<button>`s), optional `.source-viewer-tombstoned` `<p role="note">`, `.source-viewer-meta` `<p>` with `.source-viewer-stance stance-${stance}` span, `.source-viewer-claim` `<p>`, then either `.source-viewer-empty` `<p>` or `.source-viewer-body` `<div>` containing the text with a `<mark>` highlight, then optional `.source-viewer-note` `<p>`.

- [ ] **Step 1: Append source classes to the module** (port from `cockpit.css` `source-viewer-body`/`-body mark`/`-tombstoned`/`-stance`)

```css
/* --- SourceViewer (was .source-viewer-*) --- */
.sourceBody {
  white-space: pre-wrap;
  max-height: 60vh;
  overflow: auto;
  font-size: 0.92em;
  line-height: 1.5;
  color: var(--app-text);
}
.sourceBody mark {
  background: color-mix(in srgb, var(--app-warn) 30%, transparent);
  color: var(--app-text);
  border-radius: 2px;
  padding: 0 1px;
}
.sourceTombstoned {
  color: var(--app-bad);
  background: color-mix(in srgb, var(--app-bad) 16%, transparent);
  border: 1px solid color-mix(in srgb, var(--app-bad) 40%, transparent);
  border-radius: 4px;
  padding: 0.25rem 0.5rem;
  font-size: 0.85em;
}
.sourceStance {
  display: inline-block;
  font-size: 0.75em;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  padding: 0 0.35rem;
  border-radius: 999px;
  background: color-mix(in srgb, var(--app-good) 16%, transparent);
  color: var(--app-good);
  border: 1px solid color-mix(in srgb, var(--app-good) 40%, transparent);
}
```

- [ ] **Step 2: Re-skin `SourceViewer.tsx`**
```tsx
import { Box, Button, Group, Text } from "@mantine/core";
import styles from "./CockpitPanels.module.css";
```
- Root: `<aside className="source-viewer" aria-label="Cited source">` → `<Box component="aside" data-testid="source-viewer" aria-label="Cited source" p="sm" style={{ border: "1px solid var(--app-border)", borderRadius: 8, background: "var(--app-sidebar)", boxShadow: "var(--app-shadow-sm)", display: "flex", flexDirection: "column", gap: "0.5rem" }}>` (ports the old `.source-viewer` card chrome — incl. its `box-shadow: var(--app-shadow-sm)`). **The `data-testid="source-viewer"` is required** (Cockpit.test selectors).
- `.source-viewer-head` `<header>` → `<Group gap="sm" align="baseline" wrap="wrap">`; title span → `<Text fw={600}>`; pos span → `<Text size="sm" c="dimmed">`; `.source-viewer-actions` div → `<Group gap={4} ml="auto">` (the old `.source-viewer-actions` had `margin-left:auto`) of `<Button size="xs" variant="default">` for prev/next/pin/close (KEEP each `aria-label` + disabled + onClick + the glyph text ‹ › "⇱ Pin" ✕).
- tombstone `<p className="source-viewer-tombstoned" role="note">` → `<Text role="note" className={styles.sourceTombstoned}>`.
- stance: `<p className="source-viewer-meta">` → drop the wrapper `<p>` (or `<Box>`); `<span className={`source-viewer-stance stance-${stance}`}>` → `<Text span className={styles.sourceStance}>{source.stance}</Text>` (the `stance-${x}` modifier was an orphan — drop it).
- claim `<p className="source-viewer-claim">` → `<Text size="sm" c="dimmed"><strong>Claim:</strong> {source.claim}</Text>` (keep the bold "Claim:").
- empty `<p className="source-viewer-empty">` → `<Text c="dimmed" size="sm">`.
- body `<div className="source-viewer-body">` → `<Box className={styles.sourceBody}>` containing the SAME `validHighlight ? <>…<mark>…</mark>…</> : text` (keep the `<mark>` element verbatim).
- note `<p className="source-viewer-note">` → `<Text c="dimmed" size="sm">`.
- Keep the `validHighlight` computation unchanged.

- [ ] **Step 3: Update tests**
- `SourceViewer.test.tsx`: it has **seven `render(...)` calls plus a `rerender`** (lines ~31, 41, 55, 64, 77, 91, 102+107). Add a single helper and route EVERY render through it (do NOT wrap just a couple — any unwrapped Mantine render crashes with "MantineProvider not found"):
  ```tsx
  import { MantineProvider } from "@mantine/core";
  import { render } from "@testing-library/react";
  import { theme } from "../theme/theme";
  const renderSV = (ui: React.ReactElement) =>
    render(ui, { wrapper: ({ children }) => (
      <MantineProvider theme={theme} defaultColorScheme="dark">{children}</MantineProvider>
    ) });
  ```
  Replace each `render(<SourceViewer …/>)` with `renderSV(<SourceViewer …/>)`; the `rerender` returned by `renderSV` keeps the wrapper, so the rerender path stays wrapped too. (Or use the shared `renderWithProviders` from `../test-utils` — it already wraps in MantineProvider+QueryClient+Router.) The `querySelector("mark")` + `getByText` assertions are unaffected.
- `Cockpit.test.tsx`: update the 3 source-viewer selectors:
  - line ~476: `.source-viewer mark` → `[data-testid="source-viewer"] mark`
  - line ~490: `[data-region="pinned"] .source-viewer` → `[data-region="pinned"] [data-testid="source-viewer"]`
  - line ~502: `[data-region="peek"] .source-viewer` → `[data-region="peek"] [data-testid="source-viewer"]`

- [ ] **Step 4: Remove `source-viewer*` from `styles/cockpit.css`.** Verify: `grep -n "source-viewer" frontend/src/styles/cockpit.css` → no matches. (After this, only `cite-composer*` remains.)

- [ ] **Step 5: Run tests + typecheck**

Run: `pnpm test -- --run src/cockpit/SourceViewer.test.tsx src/routes/Cockpit.test.tsx` → PASS.
Run: `pnpm exec tsc --noEmit` → clean.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/cockpit/SourceViewer.tsx frontend/src/cockpit/CockpitPanels.module.css frontend/src/styles/cockpit.css frontend/src/cockpit/SourceViewer.test.tsx frontend/src/routes/Cockpit.test.tsx
git commit -m "feat(cockpit): SourceViewer -> Mantine + data-testid; drop source-viewer CSS"
```

---

### Task 2: CitationComposer → Mantine + module (preserve the selectable `<pre>`)

**Files:**
- Modify: `frontend/src/cockpit/CitationComposer.tsx`
- Modify: `frontend/src/cockpit/CockpitPanels.module.css` (append `.citeChunk` + `.citeConfirm`)
- Modify: `frontend/src/styles/cockpit.css` (remove ALL `cite-composer*` rules — leaving the file empty)
- Test: `frontend/src/cockpit/CitationComposer.test.tsx` (add MantineProvider where it renders the component)

**Current structure:** `<aside className="cite-composer" aria-label="Add a citation">` → `.cite-composer-head` (bold "Cite:", `.cite-composer-claim` span, Cancel `<button>`), then either the refs list (`.cite-composer-refs` div: "Pick a reference:" + `<ul>` of `<li><button>` per ref) or the source view (`.cite-composer-source` div: back `<button>`, "Select the supporting passage:", the chunk `<pre className="cite-composer-chunk" onMouseUp=…>{c.text}</pre>` list, optional `.cite-composer-empty` `<p>`, the `.cite-composer-confirm` `<p>` with quote + "Add citation", error `<p role="alert">`).

- [ ] **Step 1: Append the chunk/confirm classes to the module** (port from `cockpit.css`)

```css
/* --- CitationComposer (was .cite-composer-*) --- */
.citeChunk {
  white-space: pre-wrap;
  border: 1px solid var(--app-border);
  border-radius: 4px;
  padding: 0.4rem;
  font-size: 0.9em;
  cursor: text;
  max-height: 40vh;
  overflow: auto;
}
.citeConfirm {
  background: var(--app-selected);
  border: 1px solid var(--app-border-strong);
  border-radius: 4px;
  padding: 0.4rem;
}
```

- [ ] **Step 2: Re-skin `CitationComposer.tsx`** — keep ALL logic (`captureSelection`, `measureUtf16Offset`, `submit`, state) byte-identical:
```tsx
import { Box, Button, Group, Stack, Text } from "@mantine/core";
import styles from "./CockpitPanels.module.css";
```
- Root: `<aside className="cite-composer" aria-label="Add a citation">` → `<Box component="aside" aria-label="Add a citation" p="sm" style={{ border: "1px solid var(--app-border)", borderRadius: 8, background: "var(--app-sidebar)", boxShadow: "var(--app-shadow-sm)" }}><Stack gap="sm">…</Stack></Box>` (ports the old `.cite-composer` card chrome incl. its `box-shadow: var(--app-shadow-sm)`).
- `.cite-composer-head` `<header>` → `<Group gap="xs" align="baseline">`: keep `<strong>Cite:</strong>`, `.cite-composer-claim` span → `<Text span c="dimmed" fs="italic">{claim.anchor}</Text>`, Cancel `<button>` → `<Button size="xs" variant="subtle" aria-label="Cancel" ml="auto" onClick={onCancel}>✕</Button>` (old `.cite-composer-head button` had `margin-left:auto`).
- Refs branch `.cite-composer-refs` → `<Stack gap="xs">`: "Pick a reference:" `<p>` → `<Text size="sm">`; loading `<p aria-busy>` → `<Text aria-busy="true">`; `<ul>` → `<Stack component="ul" gap={4} style={{ listStyle: "none", padding: 0, margin: 0 }}>`; each `<li><button>` → `<Box component="li"><Button variant="subtle" size="xs" disabled={r.text_status !== "done"} onClick={() => setRefId(r.id)}>{r.title}{r.text_status !== "done" ? " (not indexed yet)" : ""}</Button></Box>`.
- Source branch `.cite-composer-source` → `<Stack gap="xs">`: back `<button>` → `<Button size="xs" variant="subtle">‹ back to references</Button>`; "Select the supporting passage:" → `<Text size="sm">`; loading `<p aria-busy>` → `<Text aria-busy="true">`.
- **The chunk `<pre>` — KEEP AS A RAW `<pre>`** (single text node), only swap the className: `<pre key={c.chunk_index} className={styles.citeChunk} onMouseUp={(e) => captureSelection(c.chunk_index, c.text, e.currentTarget)}>{c.text}</pre>`. Do NOT replace with a Mantine component and do NOT wrap `{c.text}` — captureSelection's offset measurement requires the single-text-node `<pre>`.
- empty `<p className="cite-composer-empty">` → `<Text c="dimmed" size="sm">`; confirm `<p className="cite-composer-confirm">` → `<Box className={styles.citeConfirm}>` (keep "Quote:", the `<q>{evidence_quote}</q>`, and the "Add citation" `<Button size="xs" disabled={add.isPending} onClick={submit}>`); error `<p role="alert">` → `<Text role="alert" c="red" size="sm">`.

- [ ] **Step 3: Add MantineProvider to `CitationComposer.test.tsx`** where it renders `<CitationComposer>` (wrap that render tree root). Leave the offset-helper test (which builds a raw `<pre>` directly) untouched.

- [ ] **Step 4: Remove `cite-composer*` from `styles/cockpit.css`.** Verify: `grep -nE "cite-composer" frontend/src/styles/cockpit.css` → no matches. The file should now contain only comments/blank lines (no rules) — confirm: `grep -cE "^\.[a-z]" frontend/src/styles/cockpit.css` → `0`.

- [ ] **Step 5: Run tests + typecheck**

Run: `pnpm test -- --run src/cockpit/CitationComposer.test.tsx` → PASS (incl. the offset-helper test + any select-to-cite interaction).
Run: `pnpm exec tsc --noEmit` → clean.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/cockpit/CitationComposer.tsx frontend/src/cockpit/CockpitPanels.module.css frontend/src/styles/cockpit.css frontend/src/cockpit/CitationComposer.test.tsx
git commit -m "feat(cockpit): CitationComposer -> Mantine; drop cite-composer CSS"
```

---

### Task 3: Modal error lines → Mantine `Text`

**Files:**
- Modify: `frontend/src/cockpit/MetadataModal.tsx`
- Modify: `frontend/src/cockpit/WorkspaceSettingsModal.tsx`
- Test: `frontend/src/cockpit/WorkspaceSettingsModal.test.tsx` (already uses `renderWithProviders` — expected green; MetadataModal has no test)

**Note:** both modals are already Mantine (`Modal`/`Button`/`TextInput`/`Select`/`Textarea`). The only non-Mantine bit is the error `<p className="library-error">` (an orphan class — NOT in cockpit.css, so no teardown).

- [ ] **Step 1: Re-skin the error lines**
- `MetadataModal.tsx`: `{approve.isError ? <p className="library-error">Could not save — check the title.</p> : null}` → `{approve.isError ? <Text c="red" size="sm">Could not save — check the title.</Text> : null}`. Add `Text` to the existing `@mantine/core` import.
- `WorkspaceSettingsModal.tsx`: both `<p className="library-error">…</p>` ("Could not save settings." and "Could not delete workspace.") → `<Text c="red" size="sm">…</Text>`. Add `Text` to the import. (Leave the inline-styled danger-zone `<div>`/`<p>` as-is — they use `var(--mantine-color-*)` tokens, not cockpit.css.)

- [ ] **Step 2: Run test + typecheck**

Run: `pnpm test -- --run src/cockpit/WorkspaceSettingsModal.test.tsx` → PASS.
Run: `pnpm exec tsc --noEmit` → clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/cockpit/MetadataModal.tsx frontend/src/cockpit/WorkspaceSettingsModal.tsx
git commit -m "feat(cockpit): modal error lines -> Mantine Text"
```

---

### Task 4: Delete `cockpit.css` + import; slice verification gate

**Files:**
- Delete: `frontend/src/styles/cockpit.css`
- Modify: `frontend/src/main.tsx` (remove the `import "./styles/cockpit.css";` at line 8)

- [ ] **Step 1: Confirm `cockpit.css` defines no rules (so deleting it removes no styling)**

The per-task teardown removed each group as its component migrated, so after Task 2 the file has zero rule blocks. Verify: `grep -cE "^\.[a-z]" frontend/src/styles/cockpit.css` → `0`. That is the real safety gate — deleting an empty stylesheet cannot remove active styling, and the `pnpm build` in Step 3 is the final proof (a still-needed CSS import would fail the build).

(Do NOT grep components for class-name substrings to gate this: 2b intentionally left harmless **orphan** wrappers — `activity-panel`, `citations-panel`, `problems-panel`, `problem-title`, etc. — that were never in `cockpit.css` (styled nowhere), and Task 1 deliberately adds `data-testid="source-viewer"`. A substring grep would false-positive on all of those. Removing those dead classNames is optional tidy, explicitly OUT of scope for this slice.)

- [ ] **Step 2: Delete the file + the import**
- Delete `frontend/src/styles/cockpit.css`.
- Remove line 8 (`import "./styles/cockpit.css";`) from `frontend/src/main.tsx`.

- [ ] **Step 3: Slice verification gate** (from `frontend/`)
- `pnpm exec tsc --noEmit` → clean.
- `pnpm test -- --run` → ALL green.
- `pnpm build` → bundle builds (a missing CSS import would fail the build — proves the deletion is safe).
- Confirm gone: `test ! -f frontend/src/styles/cockpit.css && echo "deleted"`; `grep -n "cockpit.css" frontend/src/main.tsx` → no matches.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/main.tsx frontend/src/styles/cockpit.css
git commit -m "chore(cockpit): delete styles/cockpit.css — cockpit fully on Mantine + scoped module"
```

- [ ] **Step 5: Hand off** to subagent-driven-development's whole-branch review before merge.

---

## Self-Review

**Spec coverage:** Slice 2c scope (SourceViewer, CitationComposer, MetadataModal, WorkspaceSettingsModal, + delete `cockpit.css`) all covered (T1–T4). The spec's "keep `<mark>`", "data-testid on SourceViewer", "preserve the selectable `<pre>`", and "delete the file last" are all explicit.

**Placeholder scan:** none — each task has concrete code/mapping, exact test edits, exact teardown + grep.

**Type/name consistency:** new module classes `.sourceBody`(+`mark`)/`.sourceTombstoned`/`.sourceStance`/`.citeChunk`/`.citeConfirm` are created in T1/T2 and referenced there. `data-testid="source-viewer"` added in T1 matches the three `Cockpit.test.tsx` selector updates in the same task. `cockpit.css` is emptied across T1 (source-viewer*) + T2 (cite-composer*) and only then deleted in T4 — ordering is safe.

**Risk note:** the single highest-risk item is CitationComposer's chunk `<pre>` — its select-to-cite offset math depends on it being a single-text-node `<pre>`. Task 2 swaps only its `className`; it must NOT become a Mantine component or wrap its text. The CitationComposer offset-helper test (builds a raw `<pre>` itself) guards the measurement logic and needs no change. SourceViewer's `data-testid` is the contract that lets Cockpit.test stop depending on the (now module-hashed) `.source-viewer` class.

**Deletion gate (Task 4):** the file-delete is gated on `cockpit.css` having zero rule blocks (`grep -cE "^\.[a-z]" → 0` after Task 2) + a green `pnpm build` — NOT a component-class-substring grep, which would false-positive on the intentional `data-testid="source-viewer"` and on 2b's harmless orphan wrappers (`activity-panel`/`citations-panel`/…). Those orphan classNames are dead (styled nowhere); removing them is out of scope here.

**Test-wrap completeness:** `SourceViewer.test.tsx` has 7 renders + a rerender — wrap ALL via a `renderSV` helper (Task 1 Step 3), not a subset, or unwrapped Mantine renders crash.
