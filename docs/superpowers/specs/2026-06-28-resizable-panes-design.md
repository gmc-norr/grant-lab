# Resizable Panes — Design Spec

**Date:** 2026-06-28
**Status:** Draft (awaiting user review)

## Problem

The cockpit IDE shell has fixed-width side regions — the left `Sidebar`
(`flex 0 0 240px`, `shell/Sidebar.module.css`) and the right `CockpitChat`
(`width 320px`, `cockpit/chat/CockpitChat.module.css`). VS Code lets you drag
those edges to resize and collapse them; ours can't. The user also wants the
**central pane's** internal split (the editor vs the citation source viewer)
draggable the same way.

## Goal

VS-Code-style draggable + collapsible panes via the **`react-resizable-panels`**
library (decided; not hand-rolled), with widths/collapse state **persisted**
across reloads. Two regions of work:

1. **Shell panes** (Slice 1): the left `Sidebar` and the right `CockpitChat`
   become resizable AND collapsible (drag to resize; collapse to hidden — left
   reopens via the ActivityBar, right via a header toggle).
2. **Central split** (Slice 2): inside `editor/ArtifactSectionEditor.tsx`, the
   editor ↔ citation source-viewer split becomes draggable.

Confirmed with user (2026-06-28): library = `react-resizable-panels`; sequence =
**shell first, central next**; behaviour = **resize + collapse** (both).

This spec covers the whole feature; the **Slice 1 plan is written first**, Slice
2 gets its own plan after Slice 1 merges.

## Scope

### In scope
- Add `react-resizable-panels` as a frontend dependency.
- **Slice 1:** `shell/AppShell.tsx` (wrap the body's resizable regions in a
  `PanelGroup`), `shell/Sidebar.module.css` + `cockpit/chat/CockpitChat.module.css`
  (drop the fixed widths — the panel owns width now), `shell/ActivityBar.tsx`
  (active-item click toggles the sidebar collapse), `shell/TitleBar.tsx` (a
  persistent toggle for the right chat).
- **Slice 2:** `editor/ArtifactSectionEditor.tsx` (the `.cockpit`-grid `Flex`
  from cockpit Slice 2a becomes a nested `PanelGroup` for editor ↔ source).

### Out of scope
- The bottom panel (it already has its own collapse toggle; vertical resize of
  it could be a later nicety, not now).
- The ActivityBar width (fixed 48px, like VS Code — never resizable).
- Mobile/responsive behaviour (separate concern on the review backlog).
- Any backend/API change.

## Architecture

### Library

`react-resizable-panels` (v2). Primitives: `<PanelGroup direction="horizontal"
autoSaveId="…">`, `<Panel id minSize defaultSize collapsible collapsedSize>`,
`<PanelResizeHandle>`. Sizes are **percentages of the group** (the library's
model) — not pixels; a `~18%` default reads close to the old 240px on a typical
window and scales sensibly. Persistence is automatic via `autoSaveId`
(localStorage), and the library keys saved layouts by the **set of panel ids**,
so a route showing `{sidebar, main, rightpane}` and one showing `{sidebar, main}`
persist independently — no cross-route bleed.

### Slice 1 — shell `PanelGroup`

Today `AppShell` renders `<div class=body>` = `ActivityBar` + (conditional)
`Sidebar` + `<div class=main><Outlet/></div>` + (conditional) `RightPane`. New
structure: keep `ActivityBar` fixed (48px) OUTSIDE the group; wrap the rest in a
horizontal `PanelGroup`:

```
<div class=body>
  <ActivityBar/>                      {/* fixed 48px, not in the group */}
  <PanelGroup direction="horizontal" autoSaveId="cm-shell">
    {SidebarContent && <>
      <Panel id="sidebar" order={1} collapsible collapsedSize={0} minSize={12} defaultSize={18} ref={sidebarRef} onCollapse/onExpand→state>
        <Sidebar><SidebarContent/></Sidebar>
      </Panel>
      <PanelResizeHandle aria-label="Resize sidebar"/>
    </>}
    <Panel id="main" order={2} minSize={30}>
      <div class=main><Outlet/></div>
    </Panel>
    {RightPaneContent && <>
      <PanelResizeHandle aria-label="Resize chat"/>
      <Panel id="rightpane" order={3} collapsible collapsedSize={0} minSize={15} defaultSize={22} ref={rightRef} onCollapse/onExpand→state>
        <RightPaneContent/>
      </Panel>
    </>}
  </PanelGroup>
</div>
```

- `shell/Sidebar.module.css`: drop `flex: 0 0 240px`; keep `min-height:0;
  overflow:auto; background; border-right`. The `Panel` owns width; `.sidebar`
  fills it (`height:100%`).
- `cockpit/chat/CockpitChat.module.css`: drop `width:320px`; keep the rest. The
  `Panel` owns width.
- The per-cockpit remount keys (`key={main:…}` / `key={pane:…}`) stay on the
  inner content, not the Panels, so a workspace switch still resets state without
  thrashing the PanelGroup.

### Collapse + reopen (the VS-Code part)

- **Left sidebar:** the `sidebar` Panel is `collapsible` with `collapsedSize={0}`.
  Clicking the **active ActivityBar item** toggles it (VS Code behaviour). Wire
  it via the Panel's imperative handle: AppShell holds a `sidebarRef`
  (`ImperativePanelHandle`) and a `collapsed` boolean (from `onCollapse`/
  `onExpand`); it passes `onToggle` + `collapsed` to `ActivityBar`. Collapsed
  state persists via `autoSaveId`.
- **Right chat:** the `rightpane` Panel is `collapsible`. A **single persistent
  toggle in the `TitleBar`** (top bar, always visible — so it works even when the
  panel is collapsed to 0) both collapses and expands it; the icon/label reflects
  state. (One control, always reachable — avoids a header-chevron-to-close +
  separate-reopen split. Exact TitleBar placement finalised in the plan after
  reading `TitleBar.tsx`.) Same imperative-handle + state pattern.
- Keyboard: `PanelResizeHandle` is focusable and arrow-resizable out of the box;
  add an `aria-label` to each.

### Slice 2 — central editor ↔ source split (sketch; own plan later)

In `ArtifactSectionEditor`, cockpit Slice 2a left a `Flex` of: editor column
(`editorMain`) + conditional `peek`/`pinned` source panes (`data-region`) +
conditional `composer`. Plan for Slice 2:
- When a source pane (peek OR pinned) is open, render editor + that pane inside a
  nested horizontal `PanelGroup` (`autoSaveId="cm-cockpit-center"`) with a handle
  between, so the editor↔source boundary is draggable. When no source pane is
  open, the editor is a single full-width panel (no handle).
- The **composer** stays a transient fixed-width pane (it's a short-lived
  select-to-cite affordance), NOT part of the resizable split — keeps the
  conditional-panel matrix simple.
- The `data-region`/`data-testid` hooks from Slice 2a are preserved so cockpit
  tests keep working.
This conditional-panel design is the reason Slice 2 is separated — it's finalised
in its own plan.

## Test impact

- **jsdom has no layout**, so `react-resizable-panels` can't measure real sizes;
  it renders panels at their `defaultSize` and may emit a benign warning.
  `ResizeObserver` is already stubbed in `test-setup.ts`. **Tests assert
  structure / landmarks / ARIA / collapse-state — never pixel widths.**
- `shell/AppShell.test.tsx`: wrapping the body in a `PanelGroup` adds wrapper
  divs; assert the sidebar/main/right regions + landmarks (`<nav aria-label>`,
  `aside aria-label="Sidebar"`, the chat landmark) still render. Update any
  structural assertion to the new tree without weakening it.
- `shell/ActivityBar.test.tsx`: add a test that the active-item click calls the
  collapse toggle.
- `shell/TitleBar.test.tsx`: the chat toggle calls collapse/expand and reflects
  state (label/aria).
- No API/schema change → no OpenAPI regen.

## Decomposition

- **Slice 1 (panes-shell):** dep + shell `PanelGroup` (left+right resize) +
  collapse/reopen wiring (ActivityBar + chat toggle) + persistence + test
  updates. Independently mergeable; delivers the headline VS-Code feel.
- **Slice 2 (panes-center):** the central editor↔source split (own plan after
  Slice 1).

## Verification (per slice)
1. `pnpm exec tsc --noEmit` clean.
2. `pnpm test -- --run` full green.
3. `pnpm build` builds.
4. Visual: the backendless Playwright route-mock harness — render the cockpit,
   confirm the handles appear and the panes resize/collapse (the harness can
   drag via mouse events; at minimum confirm the handles + collapsed state
   render).
5. Manually exercise on `:8000` (drag both edges, collapse + reopen, reload →
   widths persist).

## Risks / notes
- **% vs px sizing:** the library is percentage-based; the old fixed px become
  approximate %. Acceptable and arguably better (scales with window). If a hard
  px floor matters, `minSize` can be paired with a CSS `min-width` on the inner
  content as a backstop.
- **Conditional panels:** Slice 1's sidebar/right panels are route-conditional;
  the library keys persistence by panel-id-set, so this is safe — but the
  `PanelGroup` must always contain the `main` panel (it always does).
- **Imperative refs:** collapse/reopen needs `ImperativePanelHandle` refs lifted
  to AppShell and threaded to ActivityBar/chat — modest coupling, the cost of the
  collapse feature the user asked for.
