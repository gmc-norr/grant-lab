# Resizable Panes — Slice 2 (Central Editor ↔ Source Split) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the cockpit editor ↔ citation-source split draggable: inside `ArtifactSectionEditor`, the editor column and the open source pane(s) become a horizontal `PanelGroup` so the boundary between them can be dragged to resize, with widths persisted.

**Architecture:** Continues Slice 1 (`react-resizable-panels@2.1.9` is already a dependency, merged at `01b77db`). The current `.editorGrid` flex row (editor + conditional peek + conditional pinned + conditional composer) becomes: a horizontal `<PanelGroup>` holding the **editor panel (always) + conditional peek + conditional pinned**, with `<PanelResizeHandle>`s between them, and the **composer left OUTSIDE the group** as a non-resizable sibling (a transient select-to-cite affordance). Source panes still *close* by unmounting (their existing ✕ button) — there is **no collapse** here, only resize.

**Tech Stack:** React 19 + Mantine v9 + react-resizable-panels 2.1.9 + Vite + Vitest. Spec: `docs/superpowers/specs/2026-06-28-resizable-panes-design.md` (§"Slice 2 — central editor ↔ source split").

## Global Constraints

Every step's requirements implicitly include this section.

- **No new dependency** — `react-resizable-panels@2.1.9` is already installed (Slice 1). Use the same 2.x API: `<PanelGroup direction="horizontal" autoSaveId="…" className>`, `<Panel id order minSize defaultSize className>`, `<PanelResizeHandle className aria-label>` (renders `role="separator"` + `data-resize-handle-state="inactive|hover|drag"`).
- **Resize only — NO collapse, NO imperative handles.** The central panels are NOT `collapsible`; there are no `ref`s, no `onCollapse`/`onExpand`, no toggles. Source panes appear/disappear by mount/unmount on `peekEv`/`pinnedEv` (unchanged). This means **no `isCollapsed()`/`collapse()` calls**, so the jsdom-throws-on-collapse problem from Slice 1 does NOT apply here.
- **Conditional panels carry stable `id` + `order`:** `editor`=1, `peek`=2, `pinned`=3 — the library persists/restores layouts by the set of panel ids when panels mount/unmount.
- **The composer is EXCLUDED from the resizable group** — it stays a sibling `<Box className={styles.composerPane}>` after the `PanelGroup`, rendered exactly as today (transient, fixed width-range).
- **Preserve ALL existing behaviour, ARIA, and test hooks — pure layout re-wrap:**
  - The `data-region="peek"` / `data-region="pinned"` attributes stay on their inner `<Box>`es (Cockpit.test queries `[data-region="pinned"] [data-testid="source-viewer"]`, `[data-region="peek"]`).
  - The peek `<Box>` keeps `component="section" aria-label="Citation source peek"` (role `region`); pinned keeps `component="aside" aria-label="Pinned source"`.
  - `SourceViewer` (with its `data-testid="source-viewer"` + `<mark>`), `CitationComposer`, all save/buffer/conflict/selection logic, and every visible string stay byte-identical. This task only changes the JSX *container* structure + CSS, never the inner content or any handler.
- **Mantine + the existing scoped module only** — append the new classes to `frontend/src/cockpit/CockpitPanels.module.css`; use `--app-*` tokens. Module classes are unlayered → no `!important` needed.
- **Type gate is `tsc`** (no TS ESLint; `noUnusedLocals: true`). Per task: `pnpm exec tsc --noEmit` + the named test file(s). Slice end: full `pnpm test -- --run` + `pnpm build`. All commands from `frontend/`.
- **jsdom note:** `react-resizable-panels` renders panels at `defaultSize` under jsdom (no real layout) and may emit a benign warning; `ResizeObserver` is stubbed in `src/test-setup.ts`. Tests assert structure / roles / `data-*` — never pixel widths. (Slice 1 verified the tree mounts cleanly under this repo's jsdom.)

---

## File Structure

- `frontend/src/cockpit/CockpitPanels.module.css` — add `.centerPanes`, `.centerHandle`, `.sourcePanel`; trim `.sidePane`'s fixed widths.
- `frontend/src/editor/ArtifactSectionEditor.tsx` — wrap the editor + source panes in a `PanelGroup`; composer stays a sibling.
- `frontend/src/editor/ArtifactSectionEditor.test.tsx` — add two structural separator assertions.

No router/API/schema change; no OpenAPI regen. `react-resizable-panels` already in `package.json`.

---

### Task 1: Wrap the editor ↔ source split in a horizontal `PanelGroup`

**Files:**
- Modify: `frontend/src/cockpit/CockpitPanels.module.css`
- Modify: `frontend/src/editor/ArtifactSectionEditor.tsx`
- Test: `frontend/src/editor/ArtifactSectionEditor.test.tsx`

**Interfaces:**
- Consumes: the `Panel`/`PanelGroup`/`PanelResizeHandle` exports from `react-resizable-panels` (already installed); the existing `styles` import from `../cockpit/CockpitPanels.module.css`.
- Produces: nothing new for other tasks (terminal slice). The `data-region`/`data-testid` hooks are preserved for the existing Cockpit/editor tests.

- [ ] **Step 1: Append the central-split classes to `CockpitPanels.module.css`**

Find the existing block (around lines 32–51):
```css
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
Replace it with (keep `.editorGrid`, `.editorMain`, `.composerPane` as-is; trim `.sidePane`; add `.centerPanes`, `.sourcePanel`, `.centerHandle`):
```css
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
/* The editor↔source PanelGroup fills the row beside the (non-resizable) composer.
   The composer stays a sibling, so the group is the flex:1 filler. */
.centerPanes {
  flex: 1;
  min-width: 0;
}
/* A source pane is content-height and top-aligned, so a tall editor does not
   force huge blank space under the 60vh-capped SourceViewer. The editor panel
   (no align-self) stretches; the resize handle (no align-self) stretches too,
   so it stays grabbable along the full height. */
.sourcePanel {
  align-self: flex-start;
  min-width: 0;
}
.sidePane {
  min-width: 0;
}
.composerPane {
  min-width: 20rem;
  max-width: 30rem;
}
/* Vertical resize rail between editor and source pane(s). No visible UI unless
   styled; lights up on hover/drag (data-resize-handle-state) + keyboard focus
   ring. Mirrors the shell handle from Slice 1. */
.centerHandle {
  flex: 0 0 6px;
  align-self: stretch;
  position: relative;
  background: transparent;
  cursor: col-resize;
  transition: background 120ms ease;
}
.centerHandle:hover,
.centerHandle[data-resize-handle-state="hover"],
.centerHandle[data-resize-handle-state="drag"] {
  background: var(--app-accent);
}
.centerHandle:focus-visible {
  outline: 2px solid var(--app-accent);
  outline-offset: -2px;
}
```

- [ ] **Step 2: Write the failing structural tests in `ArtifactSectionEditor.test.tsx`**

The file already has an `openPeek` test that renders the editor with a peek panel and mocks `useCitationSource`. Add two tests after it.

Both render the editor with `citations={true}` so the negative case actually exercises the citations-enabled path (a `citations={false}` render via the existing `renderEditor` helper can NEVER set `peekEv`, so it would not catch a regression that renders a separator whenever `citations` is on). Factor the citations-enabled render into a small local helper, and use the **current `CitationSourceOut` shape** (matching `sourcePayload` in `Cockpit.test.tsx`) so the `SourceViewer` actually mounts with real content — strengthening the "hooks preserved" coverage:

```tsx
// Current CitationSourceOut shape (mirrors sourcePayload in Cockpit.test.tsx).
const FAKE_SOURCE = {
  evidence_id: "ev-9",
  reference_id: "r1",
  source_line: "smith2021 · Smith et al. 2021",
  claim: null,
  stance: "supports",
  confidence: null,
  index: 1,
  total: 1,
  prev_ev: null,
  next_ev: null,
  tombstoned: false,
  no_text: false,
  note: null,
  text: "The cat sat on the mat.",
  highlight: [0, 23],
};

function renderDraftEditor(openPeek?: { ev: string; n: number }) {
  const qc = new QueryClient();
  const save = { mutateAsync: vi.fn(), isPending: false };
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter initialEntries={["/cockpit/ws-1"]}>
        <QueryClientProvider client={qc}>
          <Routes>
            <Route
              path="/cockpit/:id"
              element={
                <ArtifactSectionEditor
                  workspaceId="ws-1"
                  queryKey={["draft", "ws-1"]}
                  head={"a".repeat(40)}
                  canEdit={true}
                  sections={[{ index: 0, heading: "", text: "x\n", sha: "s0" }]}
                  save={save}
                  refetch={() => {}}
                  citations={true}
                  openPeek={openPeek}
                />
              }
            />
          </Routes>
        </QueryClientProvider>
      </MemoryRouter>
    </MantineProvider>,
  );
}

it("renders a resize handle and the source viewer when a citation peek is open", async () => {
  vi.spyOn(citationsApi, "useCitationSource").mockReturnValue({
    data: FAKE_SOURCE, isLoading: false, isError: false,
  } as unknown as ReturnType<typeof citationsApi.useCitationSource>);
  const { container } = renderDraftEditor({ ev: "ev-9", n: 1 });
  await waitFor(() =>
    expect(screen.getByRole("separator", { name: "Resize source peek" })).toBeInTheDocument(),
  );
  // The source pane mounted with real content through the new Panel wrapper.
  expect(container.querySelector('[data-region="peek"] [data-testid="source-viewer"]')).not.toBeNull();
});

it("renders no resize handle when citations are on but no source pane is open", () => {
  // citations={true} with NO openPeek → peekEv/pinnedEv stay null → no source pane,
  // so the editor is a single full-width panel with no handle. Catches a regression
  // that would render a separator unconditionally whenever citations is enabled.
  vi.spyOn(citationsApi, "useCitationSource").mockReturnValue({
    data: undefined, isLoading: false, isError: false,
  } as unknown as ReturnType<typeof citationsApi.useCitationSource>);
  renderDraftEditor();
  expect(screen.queryByRole("separator")).toBeNull();
});
```

- [ ] **Step 3: Run them to verify they fail**

Run: `pnpm test -- --run src/editor/ArtifactSectionEditor.test.tsx`
Expected: "renders a resize handle and the source viewer when a citation peek is open" FAILS (no separator yet — the editor still renders the old flex row). "renders no resize handle when citations are on but no source pane is open" passes trivially (no separator exists yet). All pre-existing tests still pass.

- [ ] **Step 4: Refactor the `ArtifactSectionEditor` return into a `PanelGroup`**

In `frontend/src/editor/ArtifactSectionEditor.tsx`:

1. Add the import (after the `CodeMirrorEditor` import line, line 12):
```tsx
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
```

2. Replace the entire `return ( … )` block (lines 197–325, the `<Box component="main" className={styles.editorGrid} p="md">` … its closing `</Box>`) with the version below. **The only changes are container structure**: the editor `<Box className={styles.editorMain}>` is wrapped in `<Panel id="editor">`; each source `<Box>` is wrapped in `<Panel … className={styles.sourcePanel}>` preceded by a `<PanelResizeHandle>`; all three live inside one `<PanelGroup>`; the composer `<Box>` stays a sibling after the group. **Every inner child (the Tabs, banners, CodeMirror, buttons, SourceViewer instances, CitationComposer) is copied verbatim — do not alter their props or handlers.**

```tsx
  return (
    <Box component="main" className={styles.editorGrid} p="md">
      <PanelGroup
        direction="horizontal"
        autoSaveId="cm-cockpit-center"
        className={styles.centerPanes}
      >
        <Panel id="editor" order={1} minSize={30}>
          <Box component="section" className={styles.editorMain}>
            <Tabs value={view} onChange={(v) => v && setView(v as "section" | "full")}>
              <Tabs.List aria-label="View">
                <Tabs.Tab value="section">Section</Tabs.Tab>
                <Tabs.Tab value="full">{fullViewLabel}</Tabs.Tab>
              </Tabs.List>
            </Tabs>

            {staleSection !== null && (
              <Text role="alert" c="red" size="sm">
                This section changed underneath you. Your edits are kept.{" "}
                <Button size="xs" variant="subtle" onClick={() => reloadSection(staleSection)}>
                  Reload (discard my edits)
                </Button>{" "}
                <Button size="xs" variant="subtle" onClick={() => setStaleSection(null)}>
                  Keep editing
                </Button>
              </Text>
            )}

            {saveError !== null && (
              <Text role="alert" c="red" size="sm">
                {saveError}{" "}
                <Button size="xs" variant="subtle" onClick={() => setSaveError(null)}>
                  Dismiss
                </Button>
              </Text>
            )}

            <CodeMirrorEditor
              value={editorValue}
              docKey={view === "section" ? activeIndex : "full"}
              onChange={view === "full" ? () => {} : onEditorChange}
              requireHeading={view === "section" && activeIndex > 0}
              readOnly={view === "full" || !canEdit}
              citations={citations}
              onPillClick={citations ? onPillClick : undefined}
              onSelectionChange={citations && view === "section" ? setSelection : undefined}
              onCursorChange={setCursor}
            />

            {canEdit && view === "section" && (
              <Group gap="sm">
                <Button
                  size="xs"
                  onClick={onSave}
                  disabled={save.isPending || !isDirty(activeIndex)}
                >
                  Save section
                </Button>
                {citations && (
                  <Button
                    size="xs"
                    variant="default"
                    disabled={!selection || isDirty(activeIndex)}
                    title={isDirty(activeIndex) ? "Save this section first" : undefined}
                    onClick={() => setComposerOpen(true)}
                  >
                    Cite selection
                  </Button>
                )}
              </Group>
            )}
          </Box>
        </Panel>

        {citations && peekEv !== null && (
          <>
            <PanelResizeHandle className={styles.centerHandle} aria-label="Resize source peek" />
            <Panel id="peek" order={2} minSize={16} defaultSize={28} className={styles.sourcePanel}>
              <Box component="section" className={styles.sidePane} data-region="peek" aria-label="Citation source peek">
                {peek.isLoading && <Text aria-busy="true">Loading source…</Text>}
                {peek.isError && <Text role="alert" c="red">Could not load the cited source.</Text>}
                {peek.data && (
                  <SourceViewer
                    source={peek.data}
                    pinned={false}
                    onClose={() => setPeekEv(null)}
                    onPin={() => {
                      setPinnedEv(peekEv);
                      setPeekEv(null);
                    }}
                    onNavigate={(evId) => setPeekEv(evId)}
                  />
                )}
              </Box>
            </Panel>
          </>
        )}

        {citations && pinnedEv !== null && (
          <>
            <PanelResizeHandle className={styles.centerHandle} aria-label="Resize pinned source" />
            <Panel id="pinned" order={3} minSize={16} defaultSize={28} className={styles.sourcePanel}>
              <Box component="aside" className={styles.sidePane} data-region="pinned" aria-label="Pinned source">
                {pinned.isLoading && <Text aria-busy="true">Loading source…</Text>}
                {pinned.isError && <Text role="alert" c="red">Could not load the cited source.</Text>}
                {pinned.data && (
                  <SourceViewer
                    source={pinned.data}
                    pinned
                    onClose={() => setPinnedEv(null)}
                    onPin={() => {}}
                    onNavigate={(evId) => setPinnedEv(evId)}
                  />
                )}
              </Box>
            </Panel>
          </>
        )}
      </PanelGroup>

      {citations && composerOpen && selection && activeSection && (
        <Box component="section" className={styles.composerPane}>
          <CitationComposer
            workspaceId={workspaceId}
            claim={{
              section_index: activeSection.index,
              base_commit_sha: head,
              base_section_sha: activeSection.sha,
              anchor: selection.text,
            }}
            onDone={() => {
              setComposerOpen(false);
              setSelection(null);
              void refetch();
            }}
            onCancel={() => setComposerOpen(false)}
          />
        </Box>
      )}
    </Box>
  );
```

Notes for the implementer:
- The outer `<Box className={styles.editorGrid} p="md">` is unchanged — it is still the flex row, now holding `[PanelGroup][composer?]`.
- The composer block is byte-identical to the original (only its *position* — now a sibling after the `PanelGroup` instead of a flex child among the panes — is "changed", and visually it stays rightmost as before).
- Do NOT add `collapsible`/`ref`/`onCollapse` to any Panel. `minSize`/`defaultSize` are numbers (percent).

- [ ] **Step 5: Run the editor tests (all green)**

Run: `pnpm test -- --run src/editor/ArtifactSectionEditor.test.tsx`
Expected: PASS — the two new tests + all pre-existing editor tests (`resolves the active section…`, `citations={false} renders no Cite control`, `openPeek prop opens the citation source peek panel`).

- [ ] **Step 6: Run the cockpit integration tests (data-region hooks intact)**

Run: `pnpm test -- --run src/routes/Cockpit.test.tsx`
Expected: PASS — especially `clicking a citation pill opens the source peek`, `pinning moves the source to the pinned pane and closes the center peek`, `closing the peek removes the source pane`, `Next citation refetches…` (all query `[data-region=…] [data-testid="source-viewer"]`, which still resolve through the new Panel wrappers).

- [ ] **Step 7: Type gate**

Run: `pnpm exec tsc --noEmit`
Expected: clean. (Watch `noUnusedLocals`: the only new import is `Panel, PanelGroup, PanelResizeHandle` — all three are used.)

- [ ] **Step 8: Commit**

```bash
git add frontend/src/cockpit/CockpitPanels.module.css frontend/src/editor/ArtifactSectionEditor.tsx frontend/src/editor/ArtifactSectionEditor.test.tsx
git commit -m "feat(spa): draggable editor↔source split in the cockpit center

Wrap the editor column + open citation-source pane(s) in a horizontal
react-resizable-panels PanelGroup (autoSaveId=cm-cockpit-center) so the
editor↔source boundary is draggable; widths persist. Source panes still close
by unmounting (no collapse). The composer stays a non-resizable sibling.
data-region/data-testid hooks preserved.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Slice-end verification (after Task 1)

Run from `frontend/`:
1. `pnpm exec tsc --noEmit` — clean.
2. `pnpm test -- --run` — full suite green.
3. `pnpm build` — builds.
4. Visual (real-browser, the route-mock Playwright harness from `reference_spa_visual_check`): render the **active-phase** Draft with a citation source open (mock `cockpit-meta` `current_phase:"active"` + a `draft` with a section, and `useCitationSource` data via an `openPeek` or a pill click), then confirm: the editor↔source handle renders and lights teal on hover; dragging it resizes the boundary; the source pane is top-aligned (no large blank space under it for a tall editor); a second pinned pane gets its own handle; reload persists the widths (localStorage `react-resizable-panels:cm-cockpit-center`, keyed `editor,peek` / `editor,peek,pinned`). NOTE: the active-phase cockpit needs fuller mock data than setup phase (it `.map`s over draft sections) — supply a `draft` with a `sections`/`content` shape so the route does not hit its error boundary.
5. Manual on `:8000`: open a Draft, click a citation pill → drag the editor↔source edge; pin a second source → drag again; reload → widths persist.

---

## Self-Review (author checklist — completed)

- **Spec coverage:** editor↔source becomes a nested horizontal `PanelGroup` (Step 4) ✓; handle between editor and source (Step 4 + `.centerHandle`) ✓; full-width editor with no handle when no source open (single-panel group — Step 4, test in Step 2) ✓; composer excluded, stays transient sibling (Step 4) ✓; `data-region`/`data-testid` hooks preserved (Step 4 + Step 6) ✓; persistence via `autoSaveId="cm-cockpit-center"` (Step 4) ✓.
- **Placeholder scan:** none — full CSS, full JSX, full test code, exact commands.
- **Type consistency:** `Panel`/`PanelGroup`/`PanelResizeHandle` imported once and all used; `minSize`/`defaultSize` numeric; no `ref`/`collapsible` (resize-only) → no `ImperativePanelHandle` import, no unused locals.
- **No-collapse invariant:** confirmed there are zero imperative `isCollapsed()/collapse()/expand()` calls, so the jsdom-collapse crash from Slice 1 cannot occur here; the new tests assert `getByRole("separator")` presence/absence plus (positive case) the `[data-region="peek"] [data-testid="source-viewer"]` mount.
- **Test rigor (Codex review applied):** the negative separator test renders `citations={true}` with no `openPeek` (not the `citations={false}` helper), so it actually catches an unconditional-separator regression; the positive test uses the current `CitationSourceOut` shape so the `SourceViewer` truly mounts.
- **Scope:** only the three named files change; `SourceViewer`/`CitationComposer`/`CodeMirrorEditor` and all editor logic untouched.
