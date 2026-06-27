# Cockpit Mantine Migration — Design Spec

**Date:** 2026-06-27
**Status:** Draft (awaiting user review)

## Problem

The cockpit (the proposal-assist workspace surface) is the last large holdout
of **legacy global CSS**. Every cockpit panel renders raw `<h1>/<p>/<button>/
<ul>/<table>` with bare global classnames styled from a single 410-line global
stylesheet, `frontend/src/styles/cockpit.css`. The rest of the app uses Mantine
components + scoped CSS Modules.

This inconsistency is the visible cause of the **uneven margins** the user
reported: e.g. `.scorecard-shell` has zero horizontal padding, so the
"Scorecard" heading sits flush at the editor's left edge while the artifact tab
strip above it is inset only by each button's `0.9rem` padding — heading and
tabs don't line up. The bottom-panel body (`padding: 0.5rem`) and its tab bar
(`padding: 0.25rem 0.5rem`) disagree similarly. There is no single content
padding, so every panel improvises its own spacing.

## Goal

Bring the cockpit to the same consistency bar as the rest of the app:

1. **Every UI primitive becomes a Mantine component** — `Title`, `Text`,
   `Button`, `Badge`, `Tabs`, `Table`, `ScrollArea`, inputs — with spacing from
   Mantine's scale (`Stack`/`Group` `gap`, `Box` padding). This is what makes
   the margins uniform.
2. **The global `cockpit.css` slab is deleted.** Its irreducible visuals (things
   Mantine has no prop for) move to a **scoped CSS Module**,
   `frontend/src/cockpit/CockpitPanels.module.css`, using `--app-*` tokens —
   exactly like the rest of the repo.
3. **No behaviour or copy changes.** Pure re-skin. Same text, same roles, same
   data flow, same API calls.

Confirmed bar (user, 2026-06-27): *Mantine for primitives + scoped modules for
the irreducible visuals.* NOT "purge all CSS" — pre-wrap bodies, `<mark>`
highlights, diff colours, and CodeMirror theming stay as scoped CSS because
forcing them through Mantine's inline `styles` API would be less readable and
break the repo's CSS-Module convention.

## Scope

### In scope (global-CSS legacy consumers)

Editor shell + artifact panels:
- `routes/Cockpit.tsx` — the shell + the `[Draft | Spec | Scorecard | Panel
  review]` artifact tab strip
- `cockpit/ScorecardPanel.tsx`, `cockpit/SpecPanel.tsx`,
  `cockpit/DraftPanel.tsx`, `cockpit/ReviewPanel.tsx`, `cockpit/ProposalReview.tsx`
- `editor/ArtifactSectionEditor.tsx` — the core editor layout (the `.cockpit`
  grid: `editor | peek | pinned`, plus `.cockpit-section-actions` /
  `.cite-selection-btn` / `.cockpit-composer`). Driven by Draft/Spec panels.

Bottom panel + sub-panels:
- `cockpit/BottomPanel.tsx` (region + tab bar), `cockpit/InboxPanel.tsx`,
  `cockpit/ProblemsPanel.tsx`, `cockpit/ActivityPanel.tsx`,
  `cockpit/LibraryPanel.tsx`, `cockpit/CitationsPanel.tsx`,
  `cockpit/ClaimsPanel.tsx`

Side widgets + modals:
- `cockpit/SourceViewer.tsx`, `cockpit/CitationComposer.tsx`,
  `cockpit/MetadataModal.tsx`, `cockpit/WorkspaceSettingsModal.tsx`

Deleted at the end: `frontend/src/styles/cockpit.css`.

### Out of scope

- `cockpit/CockpitSidebar.tsx` + `cockpit/SidebarGroup.tsx` — already on a CSS
  Module (`CockpitSidebar.module.css`, zero global classnames); redone in SPA
  Phase 3a. Leave as-is.
- `editor/CodeMirrorEditor.tsx` + CodeMirror theming (`.cm-editor-host` and the
  `cm-*` styles) — CodeMirror has its own CSS layer; it stays as scoped CSS, not
  migrated to Mantine (consistent with the bar). ArtifactSectionEditor *embeds*
  CodeMirrorEditor but the editor's own theming is untouched.
- `cockpit/RubricEditor.tsx` + `cockpit/WorkspaceSetup.tsx` — already Mantine
  (Slice 1, merged `cc019a2`).
- The resizable left/right panes (separate slice, next).
- The rest of the whole-app sweep (forms, admin tables) — later slices.
- Any backend / API / behaviour change.

## Architecture

### The scoped module: `CockpitPanels.module.css`

Holds only what Mantine can't express as a prop, ported from `cockpit.css` with
`--app-*` tokens preserved. Selectors are scoped under a hashed class (descendant
bare-element selectors are fine, e.g. `.sourceBody mark`):

- **Band pills** — `strong / adequate / at-risk / gap` colour variants
  (`color-mix` gradients over `--app-good/-warn/-bad/-hover`). Used by
  ScorecardPanel. *(Alternative considered: Mantine `Badge` with `color` prop —
  rejected because the four bands need specific `color-mix` backgrounds + border
  tints that don't map onto Mantine's palette cleanly. A `Badge` wrapper with a
  module `className` per band is the chosen middle path: Mantine `Badge` for
  shape/size, module class for the band colour.)*
- **Source-viewer** — `white-space: pre-wrap` body, `<mark>` highlight
  background, stance/tombstone pills, monospace.
- **Diff rendering** — `.diff-add` / `.diff-del` line backgrounds (ProposalReview
  diffs, CitationComposer chunk).
- **Pre-wrap blocks** — proposal diff, cite-composer chunk, scorecard note.

Everything else (layout, spacing, borders, headings, buttons, tab strips) is
expressed with Mantine components and is NOT in the module.

### Mantine mapping (applies to every panel)

| Legacy | Mantine |
|---|---|
| `<main className="*-shell">` / `<section>` | `<Stack gap="sm">` inside a padded `<Box p="md">` |
| `<h1>` / `.`*`-header h1` | `<Title order={2}>` (panels) |
| `<p className="*-meta/-empty/-note">` | `<Text size="sm" c="dimmed">` |
| `<button>` | `<Button>` / `<ActionIcon>` (size `xs`/`sm`, `variant` per role) |
| `role="tablist"` button strips | Mantine `<Tabs>` (controlled, `value`/`onChange`) |
| `<ul className="*-list">` rows | `<Stack gap="xs">` of rows, each a `<Group>` |
| band/stance/tombstone pills | `<Badge>` + module band class |
| scrollable panel body | `<ScrollArea>` |
| `.cockpit` grid (editor\|peek\|pinned) | `<Flex gap="md" align="flex-start">` with `min-width:0` on the editor column (peek/pinned keep their `min/max-width` via the scoped module) |

### Consistent content padding

A single padded content container per panel (`<Box p="md">` or `Stack` with the
panel's outer padding) so headings and tab strips share one left edge. The
artifact tab strip (`Tabs.List`) and the bottom-panel tab bar inherit Mantine's
own padding, and the panel bodies use the SAME horizontal padding token — this is
the concrete fix for the reported margin drift.

### Deletion strategy for `cockpit.css`

`cockpit.css` is one global file imported once at `main.tsx:8`
(`import "./styles/cockpit.css";`). As each component migrates, delete that
component's rules from `cockpit.css`. The file shrinks each task and is deleted
(with its `main.tsx` import line) in the final task, once `grep` confirms no
remaining references to any of its classnames. Current referencing files:
`routes/Cockpit.tsx`, all `cockpit/*.tsx` panels, and
`editor/ArtifactSectionEditor.tsx`.

## Test impact

Cockpit panel tests assert on **text and ARIA roles**, which survive the
conversion. Two specific couplings + one provider requirement:

1. **`SourceViewer.test.tsx`** asserts `container.querySelector("mark")` — keep a
   real `<mark>` element for the highlight (styled by the module). No test change.
2. **`ActivityPanel.test.tsx:84`** asserts `document.querySelector(".category-member")`
   — this global class becomes module-hashed, so the literal selector breaks.
   Update the test to assert via a stable hook: render the member-category row
   with `data-category="member"` and query that (`[data-category="member"]`).
   The `data-` attribute is the stable contract; the module class is styling.
3. **MantineProvider** — panels using Mantine must be rendered with
   `renderWithProviders` (already exists, used by `RubricEditor.test.tsx`). Panel
   tests currently using plain `render` switch to `renderWithProviders`. This is
   the same mechanical fix applied in Slice 1.

`document.fonts` polyfill (for Mantine autosize `Textarea`) and `matchMedia`/
`ResizeObserver` stubs already live in `test-setup.ts` from Slice 1 — no new
setup needed.

No API/schema changes → no OpenAPI regen.

## Decomposition (sub-slices, each independently mergeable)

The sweep is split into three mergeable sub-slices to keep reviews tractable.
Each ends green (tsc + vitest + build) and removes its components' rules from
`cockpit.css`:

- **Slice 2a — Editor shell + artifact panels:** Cockpit shell + artifact
  `Tabs`, ScorecardPanel (establishes `CockpitPanels.module.css` + band pills),
  SpecPanel, DraftPanel, ReviewPanel, ProposalReview, and
  `editor/ArtifactSectionEditor.tsx` (the `.cockpit` grid → `Flex`). *(This is
  the surface the user screenshotted.)*
- **Slice 2b — Bottom panel + sub-panels:** BottomPanel `Tabs`, InboxPanel,
  ProblemsPanel, ActivityPanel, LibraryPanel, CitationsPanel, ClaimsPanel.
- **Slice 2c — Side widgets + teardown:** SourceViewer, CitationComposer,
  MetadataModal, WorkspaceSettingsModal, then **delete `cockpit.css`** + its
  import once no references remain.

(The detailed task breakdown lives in the implementation plan. Each sub-slice
gets its own plan section / branch.)

## Verification (per sub-slice)

1. `pnpm exec tsc --noEmit` — clean.
2. `pnpm test -- --run` — full vitest green (the cockpit migration touches many
   shared test setups, so run the whole suite, not just touched files).
3. `pnpm build` — bundle builds.
4. Visual: re-render the affected surface via the backendless Playwright
   route-mock harness (`scratchpad/spa_server.py` + `shot.mjs`) and eyeball the
   screenshot.
5. `grep` confirms the migrated components no longer reference any `cockpit.css`
   classname; the final slice confirms the file is deletable.

## Non-goals / risks

- **No visual redesign** — same information, same layout intent, just consistent
  spacing + Mantine chrome. Band colours, diff colours, and highlight behaviour
  are preserved pixel-intent via the scoped module.
- **CodeMirror theming** (in DraftPanel/SpecPanel editors) is its own CSS and is
  NOT migrated to Mantine — left as scoped CSS, consistent with the bar.
- **Risk:** Mantine `Tabs` changes the artifact tab DOM. `Cockpit.test.tsx`
  asserts tab behaviour by role/name (`role="tab"`, accessible name) — Mantine
  `Tabs` renders `role="tab"` with accessible names, so these should survive;
  any that assert raw `<button>` structure get updated in the same task.
