# SPA Redesign — Phase 3a (Cockpit relocation & reskin) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Relocate the proposal Outline out of the editor and into the IDE shell's left sidebar region, drive the cockpit's artifact-tab + active-section from the URL, wire the CodeMirror VS Code dark theme, reskin the editor tabs / bottom panel / scorecard onto the `--app-*` palette, and fully retire the legacy `--cm-*` token system from the SPA — all behavior-preserving.

**Architecture:** The AppShell renders the sidebar as a *sibling* of the routed Cockpit, so (as in Phase 2) cross-boundary state travels through **global channels**, never props/context: the active artifact tab + active section live in the URL (`?tab`/`?sec`, via a new `useCockpitNav` hook); the sections come from the shared React Query cache (`useDraft`/`useSpec`); and dirty markers / cursor / save-state flow through one tiny `useSyncExternalStore` store the mounted editor publishes to. The editing engine (per-section buffers, heading-delete guard, 409/save-error banners, citation peek/compose) is preserved verbatim — only *where the outline renders* and *who owns tab/section state* changes.

**Tech Stack:** React 19 + Vite 5 + react-router-dom 7 (data router) + TanStack Query 5 + CodeMirror 6 (`@uiw/codemirror-theme-vscode`, `@lezer/highlight`) + `@vscode/codicons` + Vitest 2 + Testing Library.

**Spec:** `docs/superpowers/specs/2026-06-23-spa-vscode-redesign-phase3-cockpit-design.md` — this plan implements **§8 Plan 3a** (relocation & reskin; the three backend endpoints + the four extra sidebar groups + the rich status bar are Plan 3b). Read it.

## Global Constraints

- **Package manager pnpm 10.16 (Node ≥ 20).** Run `pnpm install` after any `package.json` change. New dep this plan: `@lezer/highlight@1.2.3` (exact — `.npmrc` has `save-exact=true`; it is already in the lockfile transitively, so pnpm dedupes).
- **Gate after every task, all green, from `frontend/`:** `pnpm exec tsc --noEmit` AND `pnpm vitest run` AND `pnpm build`. There is no TS linter — `tsc` is the gate (`strict`, `noUnusedLocals`, `noUnusedParameters`); `pnpm build` (`tsc && vite build`) additionally catches bundle/import regressions.
- **Re-skin, not rewrite — preserve the editing engine exactly.** These behaviors keep their current logic + tests: per-section dirty `buffers` (edits to one section survive switching sections); the heading-delete guard (`blockedByHeadingGuard`); the controlled remote `value` push (`Transaction.remote`, never echoed back to `onChange`); the select-to-cite selection bridge (kept across remote echoes); the readOnly compartment; the 409 conflict prompt (buffer preserved; Reload discards) and the non-conflict `saveError` dismissible banner; the optimistic post-save cache reconcile. The Section↔Full view toggle stays **local to the editor**; only the active *section index* moves to the URL.
- **State channels (no props/context across the AppShell boundary):** active artifact tab → `?tab`; active section → `?sec`; sections → React Query cache (`["draft", id]` / `["spec", id]`); dirty/cursor/save → the `editorStore` external store. The URL is the single source of truth for tab/section.
- **`?sec` is one clamped index for the active artifact.** An out-of-range `?sec` (artifact switch / deep-link before sections load) self-corrects: the editor `history.replace`s the URL to a valid index once `useDraft`/`useSpec` resolves (no extra history entry).
- **`NavTarget` carries `section_index`.** Problems/Scorecard "Go to §" must land on the section, not just switch tabs — `navigate()` writes `?tab` + `?sec` from the target.
- **Theme tokens only, dark-first.** Component/global CSS uses the `--app-*` variables from `frontend/src/theme/theme.css`; no hardcoded hex in CSS modules. (Exception: the CodeMirror JS theme config — `vscodeDarkInit({ settings })` — needs literal hex because the theme factory parses colors; the values used equal the corresponding `--app-*` tokens.) Tinted pill/badge backgrounds use `color-mix(in srgb, var(--app-*) N%, transparent)`.
- **`--cm-*` → `--app-*` migration map** (applied in Tasks 7–8; every occurrence): `--cm-border`,`--cm-border-soft` → `--app-border`; `--cm-surface` → `--app-sidebar`; `--cm-surface-sunken` → `--app-editor`; `--cm-shadow-sm` → `--app-shadow-sm` (new token); `--cm-ink`,`--cm-ink-strong` → `--app-text`; `--cm-ink-muted`,`--cm-muted`,`--cm-neutral-text` → `--app-muted`; `--cm-primary` → `--app-accent-bright`; `--cm-primary-hover` → `--app-accent`; `--cm-primary-soft`,`--cm-neutral-bg` → `--app-selected`/`--app-hover`; `--cm-primary-border` → `--app-border-strong`; `--cm-accent` → `--app-accent`; `--cm-good-text` → `--app-good`; `--cm-warn-text`,`--cm-warn` → `--app-warn`; `--cm-bad-text`,`--cm-error` → `--app-bad`; `*-bg`/`*-border` tints → `color-mix(... var(--app-good|warn|bad) ...)`.
- **No new SPA route.** Adding `handle: { Sidebar: CockpitSidebar }` to the existing `cockpit/:id` route does NOT change `routes.json`/`routes.gen.ts`; the `router.test.tsx` drift guard (reads only `path`/`children`/`index`) stays green.
- **Module-global store hygiene in tests.** `editorStore` is a singleton; any test that mounts the editor or `CockpitSidebar` MUST `resetEditorStore()` in `afterEach` so state never leaks between tests.
- **OpenAPI codegen is NOT touched** in 3a (no API change). (3b regenerates `schema.gen.ts`.)
- **Git — every commit uses THIS exact form.** All work is in the worktree `WT=/home/parlar_ai/dev/call-monitor/.worktrees/spa-vscode-redesign-phase3a` on branch `feat/spa-vscode-redesign-phase3a` (off `master`). Dispatched subagents default git/cwd to the MAIN tree (the WRONG branch), so **before any commit** run `git -C "$WT" branch --show-current` and confirm it prints exactly `feat/spa-vscode-redesign-phase3a`. Then:
  ```bash
  git -C "$WT" add <paths>
  git -C "$WT" -c commit.gpgsign=false commit -m "<message>

  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
  ```

---

## File Structure

| File | Responsibility |
|---|---|
| `frontend/src/cockpit/nav.ts` (modify) | `NavTarget` gains `section_index?: number` |
| `frontend/src/cockpit/cockpitNav.ts` (new) + `.test.tsx` | `useCockpitNav` hook (URL `?tab`/`?sec`), `TOP_TABS`, `tabFromParams`/`secFromParams`, `navigate(NavTarget)` |
| `frontend/src/cockpit/editorStore.ts` (new) + `.test.ts` | `useSyncExternalStore` singleton: dirty section set / cursor Ln·Col / save-state, published by the mounted editor |
| `frontend/src/editor/CodeMirrorEditor.tsx` (modify) + `.test.tsx` (new) | VS Code dark theme + teal heading/link highlight + line gutter; `onCursorChange` prop |
| `frontend/src/routes/Cockpit.tsx` (modify) | Artifact tab from `?tab` (`useCockpitNav`); `onNavigate` writes URL + local bottom-tab |
| `frontend/src/cockpit/ScorecardPanel.tsx` + `ProblemsPanel.tsx` (modify) | Pass `section_index` through `onNavigate` |
| `frontend/src/editor/ArtifactSectionEditor.tsx` (modify) | Active section from `?sec` (clamped + replace); publish dirty/cursor/save; Section↔Full local; (Task 6) remove the in-editor Outline |
| `frontend/src/cockpit/CockpitSidebar.tsx` (new) + `.module.css` + `.test.tsx` | The cockpit `handle.Sidebar`: the Outline group (reads `?tab`/`?sec` + RQ + store) |
| `frontend/src/cockpit/SidebarGroup.tsx` (new) | Collapsible sidebar group primitive (codicon chevron + count) |
| `frontend/src/router.tsx` (modify) | `cockpit/:id` route gains `handle: { Sidebar: CockpitSidebar }` |
| `frontend/src/routes/Cockpit.test.tsx` (modify) | `renderCockpit` mounts `CockpitSidebar` + `Cockpit`; URL-driven; `resetEditorStore` |
| `frontend/src/styles/cockpit.css` (rewrite) | All rules → `--app-*`; IDE editor-tabs + bottom-panel + scorecard reskin |
| `frontend/src/components/Button.module.css`, `Select.module.css`, `Card.module.css` (modify) | → `--app-*` |
| `frontend/src/theme/theme.css` (modify) | Add `--app-shadow-sm`; delete the `--cm-*` dark-remap block |
| `frontend/src/main.tsx` (modify) | Drop the `styles/tokens.css` import |
| `frontend/src/shell/StatusBar.tsx` (modify) + `.module.css` + `.test.tsx` (new) | On `/cockpit`: save-state · `Markdown` · `Ln/Col` from `editorStore` |

---

### Task 1: `useCockpitNav` hook + `NavTarget.section_index`

**Files:**
- Modify: `frontend/src/cockpit/nav.ts`
- Create: `frontend/src/cockpit/cockpitNav.ts`, `frontend/src/cockpit/cockpitNav.test.tsx`

**Interfaces:**
- Produces: `TopTab` (unchanged), `NavTarget = { top_tab?; bottom_tab?; section_index? }`; `useCockpitNav(): { tab: TopTab; sec: number; setTab(t); setSec(i, opts?); navigate(target) }`; `tabFromParams(sp)`, `secFromParams(sp)`, `TOP_TABS`.
- Consumes: `react-router-dom` `useSearchParams`.

- [ ] **Step 1: Extend `NavTarget`** in `frontend/src/cockpit/nav.ts`:

```ts
export type TopTab = "draft" | "spec" | "scorecard" | "review";
export type BottomTab = "inbox" | "problems" | "activity";
export type NavTarget = { top_tab?: TopTab; bottom_tab?: BottomTab; section_index?: number };
export type OnNavigate = (target: NavTarget) => void;
```

- [ ] **Step 2: Write the failing test** `frontend/src/cockpit/cockpitNav.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { expect, it } from "vitest";

import { useCockpitNav } from "./cockpitNav";

function Probe() {
  const nav = useCockpitNav();
  const loc = useLocation();
  return (
    <div>
      <span data-testid="tab">{nav.tab}</span>
      <span data-testid="sec">{nav.sec}</span>
      <span data-testid="search">{loc.search}</span>
      <button onClick={() => nav.setTab("spec")}>setTab</button>
      <button onClick={() => nav.setSec(3)}>setSec</button>
      <button onClick={() => nav.navigate({ top_tab: "draft", section_index: 2 })}>nav</button>
    </div>
  );
}
function renderAt(url: string) {
  return render(<MemoryRouter initialEntries={[url]}><Probe /></MemoryRouter>);
}

it("defaults to draft / 0 with no params", () => {
  renderAt("/cockpit/x");
  expect(screen.getByTestId("tab").textContent).toBe("draft");
  expect(screen.getByTestId("sec").textContent).toBe("0");
});
it("reads ?tab and ?sec", () => {
  renderAt("/cockpit/x?tab=spec&sec=2");
  expect(screen.getByTestId("tab").textContent).toBe("spec");
  expect(screen.getByTestId("sec").textContent).toBe("2");
});
it("falls back on an invalid tab / non-numeric sec", () => {
  renderAt("/cockpit/x?tab=bogus&sec=abc");
  expect(screen.getByTestId("tab").textContent).toBe("draft");
  expect(screen.getByTestId("sec").textContent).toBe("0");
});
it("setTab writes ?tab", () => {
  renderAt("/cockpit/x");
  fireEvent.click(screen.getByText("setTab"));
  expect(screen.getByTestId("tab").textContent).toBe("spec");
  expect(screen.getByTestId("search").textContent).toContain("tab=spec");
});
it("setSec writes ?sec", () => {
  renderAt("/cockpit/x");
  fireEvent.click(screen.getByText("setSec"));
  expect(screen.getByTestId("sec").textContent).toBe("3");
});
it("navigate writes ?tab and ?sec from a target (section_index)", () => {
  renderAt("/cockpit/x?tab=scorecard");
  fireEvent.click(screen.getByText("nav"));
  expect(screen.getByTestId("tab").textContent).toBe("draft");
  expect(screen.getByTestId("sec").textContent).toBe("2");
});
```

- [ ] **Step 3: Run it — Expected: FAIL** (`Cannot find module './cockpitNav'`).

Run: `pnpm vitest run src/cockpit/cockpitNav.test.tsx`

- [ ] **Step 4: Implement** `frontend/src/cockpit/cockpitNav.ts`:

```ts
import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";

import type { NavTarget, TopTab } from "./nav";

export const TOP_TABS: TopTab[] = ["draft", "spec", "scorecard", "review"];

/** `?tab` parsed to a valid TopTab, defaulting to "draft". */
export function tabFromParams(sp: URLSearchParams): TopTab {
  const t = sp.get("tab");
  return (TOP_TABS as string[]).includes(t ?? "") ? (t as TopTab) : "draft";
}

/** `?sec` parsed to a non-negative int, defaulting to 0. */
export function secFromParams(sp: URLSearchParams): number {
  const s = sp.get("sec");
  return s != null && /^\d+$/.test(s) ? Number(s) : 0;
}

export type CockpitNav = {
  tab: TopTab;
  sec: number;
  setTab: (t: TopTab) => void;
  setSec: (i: number, opts?: { replace?: boolean }) => void;
  navigate: (target: NavTarget) => void;
};

// Single source of truth for the cockpit's active artifact tab (?tab) and active
// section (?sec). Both the editor (main) and CockpitSidebar (a shell sibling) read
// these — the URL is the shared channel across the AppShell boundary, exactly as
// Phase 2 used it for the dashboard filters. setSec({ replace: true }) backs the
// out-of-range clamp (an invalid deep-link self-corrects with no history entry).
// navigate() owns the URL half of a NavTarget (tab + section); the bottom-panel
// tab is local cockpit state (handled by Cockpit's onNavigate wrapper).
export function useCockpitNav(): CockpitNav {
  const [sp, setSp] = useSearchParams();
  const tab = tabFromParams(sp);
  const sec = secFromParams(sp);

  const setTab = useCallback(
    (t: TopTab) =>
      setSp((prev) => {
        const next = new URLSearchParams(prev);
        next.set("tab", t);
        return next;
      }),
    [setSp],
  );

  const setSec = useCallback(
    (i: number, opts?: { replace?: boolean }) =>
      setSp(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set("sec", String(i));
          return next;
        },
        { replace: opts?.replace ?? false },
      ),
    [setSp],
  );

  const navigate = useCallback(
    (target: NavTarget) =>
      setSp((prev) => {
        const next = new URLSearchParams(prev);
        if (target.top_tab) next.set("tab", target.top_tab);
        if (target.section_index != null) next.set("sec", String(target.section_index));
        return next;
      }),
    [setSp],
  );

  return { tab, sec, setTab, setSec, navigate };
}
```

- [ ] **Step 5: Run it — Expected: PASS** (6 tests).

Run: `pnpm vitest run src/cockpit/cockpitNav.test.tsx`

- [ ] **Step 6: Gate + commit**

```bash
pnpm exec tsc --noEmit && pnpm vitest run && pnpm build
git -C "$WT" branch --show-current   # MUST print feat/spa-vscode-redesign-phase3a
git -C "$WT" add frontend/src/cockpit/nav.ts frontend/src/cockpit/cockpitNav.ts frontend/src/cockpit/cockpitNav.test.tsx
git -C "$WT" -c commit.gpgsign=false commit -m "feat(cockpit): useCockpitNav URL state (?tab/?sec) + NavTarget.section_index

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `editorStore` external store

**Files:**
- Create: `frontend/src/cockpit/editorStore.ts`, `frontend/src/cockpit/editorStore.test.ts`

**Interfaces:**
- Produces: `useEditorStore(): EditorSnapshot` (`{ dirty: number[]; cursor: Cursor | null; save: SaveState }`); imperative publishers `setDirty(number[])`, `setCursor(Cursor | null)`, `setSave(SaveState)`, `resetEditorStore()`; raw `subscribe`/`getSnapshot`. `SaveState = "idle"|"saving"|"saved"|"dirty"|"error"`; `Cursor = { line: number; col: number }`.
- Consumes: React `useSyncExternalStore`.

- [ ] **Step 1: Write the failing test** `frontend/src/cockpit/editorStore.test.ts`:

```ts
import { afterEach, expect, it, vi } from "vitest";

import {
  getSnapshot,
  resetEditorStore,
  setCursor,
  setDirty,
  setSave,
  subscribe,
} from "./editorStore";

afterEach(() => resetEditorStore());

it("starts empty", () => {
  expect(getSnapshot()).toEqual({ dirty: [], cursor: null, save: "idle" });
});

it("setDirty sorts, dedupes, and emits only on change", () => {
  const cb = vi.fn();
  const unsub = subscribe(cb);
  setDirty([3, 1]);
  expect(getSnapshot().dirty).toEqual([1, 3]);
  expect(cb).toHaveBeenCalledTimes(1);
  setDirty([1, 3]); // same contents -> no emit
  expect(cb).toHaveBeenCalledTimes(1);
  unsub();
});

it("setCursor / setSave update and emit", () => {
  const cb = vi.fn();
  subscribe(cb);
  setCursor({ line: 2, col: 4 });
  setSave("saving");
  expect(getSnapshot().cursor).toEqual({ line: 2, col: 4 });
  expect(getSnapshot().save).toBe("saving");
  expect(cb).toHaveBeenCalledTimes(2);
});

it("getSnapshot is referentially stable until a change (no tearing)", () => {
  const a = getSnapshot();
  setSave("idle"); // already idle -> no change
  expect(getSnapshot()).toBe(a);
});

it("reset returns to empty and notifies", () => {
  const cb = vi.fn();
  subscribe(cb);
  setSave("error");
  resetEditorStore();
  expect(getSnapshot()).toEqual({ dirty: [], cursor: null, save: "idle" });
  expect(cb).toHaveBeenCalled();
});
```

- [ ] **Step 2: Run it — Expected: FAIL** (`Cannot find module './editorStore'`).

Run: `pnpm vitest run src/cockpit/editorStore.test.ts`

- [ ] **Step 3: Implement** `frontend/src/cockpit/editorStore.ts`:

```ts
import { useSyncExternalStore } from "react";

export type SaveState = "idle" | "saving" | "saved" | "dirty" | "error";
export type Cursor = { line: number; col: number };
export type EditorSnapshot = { dirty: number[]; cursor: Cursor | null; save: SaveState };

const EMPTY: EditorSnapshot = { dirty: [], cursor: null, save: "idle" };
let snapshot: EditorSnapshot = EMPTY;
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}
function sameNums(a: number[], b: number[]): boolean {
  return a.length === b.length && a.every((n, i) => n === b[i]);
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

// MUST return a stable reference while unchanged — useSyncExternalStore re-reads on
// every render and would loop on a fresh object each time.
export function getSnapshot(): EditorSnapshot {
  return snapshot;
}

/** Publish the dirty section indices for the mounted artifact editor. A
 *  same-contents set does NOT emit (the buffers effect fires on every keystroke). */
export function setDirty(indices: number[]): void {
  const sorted = [...indices].sort((a, b) => a - b);
  if (sameNums(sorted, snapshot.dirty)) return;
  snapshot = { ...snapshot, dirty: sorted };
  emit();
}

export function setCursor(cursor: Cursor | null): void {
  const c = snapshot.cursor;
  if (c === cursor) return;
  if (c && cursor && c.line === cursor.line && c.col === cursor.col) return;
  snapshot = { ...snapshot, cursor };
  emit();
}

export function setSave(save: SaveState): void {
  if (snapshot.save === save) return;
  snapshot = { ...snapshot, save };
  emit();
}

/** Reset to empty — call on editor unmount so a stale cursor/save/dirty never
 *  leaks to the status bar of a non-cockpit view or the next artifact. */
export function resetEditorStore(): void {
  if (snapshot === EMPTY) return;
  snapshot = EMPTY;
  emit();
}

export function useEditorStore(): EditorSnapshot {
  return useSyncExternalStore(subscribe, getSnapshot);
}
```

- [ ] **Step 4: Run it — Expected: PASS** (5 tests).

Run: `pnpm vitest run src/cockpit/editorStore.test.ts`

- [ ] **Step 5: Gate + commit**

```bash
pnpm exec tsc --noEmit && pnpm vitest run && pnpm build
git -C "$WT" branch --show-current
git -C "$WT" add frontend/src/cockpit/editorStore.ts frontend/src/cockpit/editorStore.test.ts
git -C "$WT" -c commit.gpgsign=false commit -m "feat(cockpit): editorStore (dirty/cursor/save) external store

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: CodeMirror VS Code theme + line gutter + `onCursorChange`

**Files:**
- Modify: `frontend/src/editor/CodeMirrorEditor.tsx`
- Create: `frontend/src/editor/CodeMirrorEditor.test.tsx`
- Modify: `frontend/package.json` (+ `@lezer/highlight@1.2.3`)

**Interfaces:**
- Consumes: `editorStore` not directly — the cursor is reported via a new prop so the editor stays store-agnostic and unit-testable.
- Produces: `CodeMirrorEditorProps` gains `onCursorChange?: (pos: { line: number; col: number } | null) => void` (default no-op). The editor now renders the `@uiw/codemirror-theme-vscode` dark theme + a teal heading/link `HighlightStyle` + a `lineNumbers()` gutter; the `.cm-citation-pill` class and all existing behavior are unchanged.

- [ ] **Step 1: Add the dependency**

Run: `pnpm add @lezer/highlight@1.2.3`
Expected: `package.json` gains `"@lezer/highlight": "1.2.3"` (exact); lockfile updated; install succeeds.

- [ ] **Step 2: Write the failing test** `frontend/src/editor/CodeMirrorEditor.test.tsx`:

```tsx
import type { EditorView } from "@codemirror/view";
import { render } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { CodeMirrorEditor } from "./CodeMirrorEditor";

it("publishes 1-based Ln/Col on selection change", () => {
  const onCursorChange = vi.fn();
  const { container } = render(
    <CodeMirrorEditor
      value={"line one\nline two"}
      onChange={() => {}}
      requireHeading={false}
      citations={false}
      onCursorChange={onCursorChange}
    />,
  );
  const view = (container.querySelector(".cm-editor") as HTMLElement & { __cmView?: EditorView })
    .__cmView!;
  // Caret at index 12 = start of "line two" (9) + 3 -> line 2, col 4.
  view.dispatch({ selection: { anchor: 12 } });
  expect(onCursorChange).toHaveBeenLastCalledWith({ line: 2, col: 4 });
});

it("renders a line-number gutter", () => {
  const { container } = render(
    <CodeMirrorEditor value={"a\nb"} onChange={() => {}} requireHeading={false} citations={false} />,
  );
  expect(container.querySelector(".cm-gutters")).not.toBeNull();
});
```

- [ ] **Step 3: Run it — Expected: FAIL** (`onCursorChange` ignored → not called; no `.cm-gutters`).

Run: `pnpm vitest run src/editor/CodeMirrorEditor.test.tsx`

- [ ] **Step 4: Implement** the changes in `frontend/src/editor/CodeMirrorEditor.tsx`.

(a) Replace the imports block at the top (lines 1–7) with (adds `lineNumbers`, `HighlightStyle`/`syntaxHighlighting`, `tags`, `vscodeDarkInit`):

```ts
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { markdown } from "@codemirror/lang-markdown";
import { Compartment, EditorState, Transaction } from "@codemirror/state";
import { EditorView, keymap, lineNumbers } from "@codemirror/view";
import { tags as t } from "@lezer/highlight";
import { vscodeDarkInit } from "@uiw/codemirror-theme-vscode";
import { useEffect, useRef } from "react";

import { citationPills } from "./citations";
```

(b) Replace the `cmTheme` constant (the `EditorView.theme({...})` block, current lines 50–76) with the VS Code base + teal highlight + chrome override:

```ts
// VS Code dark editor recolored to our identity. The vscode base + gutter use
// literal hex (the @uiw theme factory parses colors, so CSS vars can't be passed
// there); they equal our --app-* tokens (#1a1b1e=--app-editor, #2dd4bf=accent-bright,
// #0d3b38=selected). A teal HighlightStyle recolors markdown headings/links, and a
// thin chrome override (token-based) styles the focus ring + citation pill. jsdom
// never resolves these colors (zero rects), so the vitest suite is unaffected — the
// look is screenshot-verified.
const vscodeBase = vscodeDarkInit({
  settings: {
    background: "#1a1b1e",
    gutterBackground: "#1a1b1e",
    caret: "#2dd4bf",
    selection: "#0d3b38",
    selectionMatch: "#0d3b38",
    lineHighlight: "#ffffff0a",
  },
});
const tealHighlight = syntaxHighlighting(
  HighlightStyle.define([
    { tag: [t.heading, t.heading1, t.heading2, t.heading3], color: "#4dc4d9", fontWeight: "600" },
    { tag: [t.link, t.url], color: "#2dd4bf" },
  ]),
);
const cmChrome = EditorView.theme({
  "&": { borderRadius: "6px", fontSize: "0.95rem" },
  ".cm-content": { fontFamily: "ui-monospace, monospace" },
  "&.cm-focused": { outline: "2px solid var(--app-accent)" },
  ".cm-citation-pill": {
    backgroundColor: "var(--app-selected)",
    border: "1px solid var(--app-border-strong)",
    borderRadius: "4px",
    padding: "0 4px",
    color: "var(--app-link)",
    cursor: "pointer",
    whiteSpace: "nowrap",
  },
});
```

(c) Add `onCursorChange` to the props type (after `onSelectionChange?` in `CodeMirrorEditorProps`):

```ts
  /** Reports the 1-based caret Ln/Col (or null) for the status bar. Default no-op. */
  onCursorChange?: (pos: { line: number; col: number } | null) => void;
```

(d) In the function signature destructure, add `onCursorChange = () => {},` (next to `onSelectionChange = () => {}`), and add the ref alongside the others:

```ts
  const onCursorChangeRef = useRef(onCursorChange);
  onCursorChangeRef.current = onCursorChange;
```

(e) In the extensions array, replace the single `cmTheme,` entry with the four:

```ts
      vscodeBase,
      tealHighlight,
      cmChrome,
      lineNumbers(),
```

(f) In the `EditorView.updateListener.of((u) => {...})` body, add the cursor publish at the very top of the callback (before the citation block), so it fires for BOTH draft and spec editors:

```ts
        if (u.selectionSet || u.docChanged) {
          const head = u.state.selection.main.head;
          const line = u.state.doc.lineAt(head);
          onCursorChangeRef.current?.({ line: line.number, col: head - line.from + 1 });
        }
```

- [ ] **Step 5: Run it — Expected: PASS** (2 tests).

Run: `pnpm vitest run src/editor/CodeMirrorEditor.test.tsx`

- [ ] **Step 6: Gate + commit** (the full suite must stay green — the existing Cockpit pill/contenteditable/selection tests are unaffected by the theme swap):

```bash
pnpm exec tsc --noEmit && pnpm vitest run && pnpm build
git -C "$WT" branch --show-current
git -C "$WT" add frontend/src/editor/CodeMirrorEditor.tsx frontend/src/editor/CodeMirrorEditor.test.tsx frontend/package.json frontend/pnpm-lock.yaml
git -C "$WT" -c commit.gpgsign=false commit -m "feat(editor): VS Code dark CM theme + teal headings + gutter + onCursorChange

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Cockpit artifact tab from `?tab` + section-index nav wiring

**Files:**
- Modify: `frontend/src/routes/Cockpit.tsx`
- Modify: `frontend/src/cockpit/ScorecardPanel.tsx`, `frontend/src/cockpit/ProblemsPanel.tsx`
- Modify: `frontend/src/routes/Cockpit.test.tsx` (add one URL-tab test; existing tests unaffected)

**Interfaces:**
- Consumes: `useCockpitNav` (Task 1).
- Produces: the artifact top-tab is now URL-driven; `onNavigate` writes `?tab`+`?sec` (via `navigate`) and still drives the local bottom-panel tab. `ScorecardPanel`/`ProblemsPanel` include `section_index` in the target they emit.

- [ ] **Step 1: Replace** `frontend/src/routes/Cockpit.tsx` entirely:

```tsx
import { useState } from "react";
import { useParams } from "react-router-dom";

import { BottomPanel } from "../cockpit/BottomPanel";
import { useCockpitNav } from "../cockpit/cockpitNav";
import { DraftPanel } from "../cockpit/DraftPanel";
import type { BottomTab, NavTarget } from "../cockpit/nav";
import { ReviewPanel } from "../cockpit/ReviewPanel";
import { ScorecardPanel } from "../cockpit/ScorecardPanel";
import { SpecPanel } from "../cockpit/SpecPanel";

export function Cockpit() {
  const { id = "" } = useParams();
  // Artifact tab + active section live in the URL (?tab/?sec) so the shell sidebar
  // Outline and the editor stay in sync across the AppShell boundary. The bottom
  // region (tab + collapse) stays local — it never crosses that boundary.
  const { tab, setTab, navigate } = useCockpitNav();
  const [bottomTab, setBottomTab] = useState<BottomTab>("inbox");
  const [collapsed, setCollapsed] = useState(false);

  const onNavigate = (target: NavTarget) => {
    navigate(target); // ?tab + ?sec
    if (target.bottom_tab) {
      setBottomTab(target.bottom_tab);
      setCollapsed(false);
    }
  };

  return (
    <div className="cockpit-shell">
      <div role="tablist" aria-label="Artifact">
        <button type="button" role="tab" aria-selected={tab === "draft"} onClick={() => setTab("draft")}>
          Draft
        </button>
        <button type="button" role="tab" aria-selected={tab === "spec"} onClick={() => setTab("spec")}>
          Spec
        </button>
        <button type="button" role="tab" aria-selected={tab === "scorecard"} onClick={() => setTab("scorecard")}>
          Scorecard
        </button>
        <button type="button" role="tab" aria-selected={tab === "review"} onClick={() => setTab("review")}>
          Panel review
        </button>
      </div>
      {tab === "draft" && <DraftPanel id={id} />}
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
    </div>
  );
}
```

- [ ] **Step 2: Wire `section_index` in `ScorecardPanel.tsx`** — change the deep-link button's onClick (line 104) to:

```tsx
                onClick={() =>
                  onNavigate({
                    top_tab: c.deep_link!.artifact,
                    section_index: c.deep_link!.section_index,
                  })
                }
```

- [ ] **Step 3: Wire `section_index` in `ProblemsPanel.tsx`** — change `toNav` (lines 24–29) to:

```tsx
function toNav(target: ProblemRow["target"]): NavTarget {
  return {
    top_tab: target?.top_tab ?? undefined,
    bottom_tab: target?.bottom_tab ?? undefined,
    section_index: target?.section_index ?? undefined,
  };
}
```

- [ ] **Step 4: Add a URL-tab test** to `frontend/src/routes/Cockpit.test.tsx` (append; reuses the existing `mockDraftSequenceByPath` which already serves `/spec` an empty-section payload):

```tsx
it("clicking the Spec tab switches the artifact view and sets ?tab=spec", async () => {
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/spec")) {
      return Promise.resolve({
        data: { head: "a".repeat(40), can_edit: true, sections: [{ index: 0, heading: "", text: "", sha: "s0" }], locked: false },
        error: undefined,
      });
    }
    if (path.endsWith("/proposed-changes")) return Promise.resolve({ data: { items: [] }, error: undefined });
    if (path.endsWith("/problems")) return Promise.resolve({ data: { problems: [], counts: { error: 0, warning: 0 }, scorecard_stale: [] }, error: undefined });
    return Promise.resolve(draftPayload(true)); // /draft
  }) as never);
  renderCockpit();
  await screen.findByRole("button", { name: /Methods/ });
  fireEvent.click(screen.getByRole("tab", { name: /^spec$/i }));
  // The empty-spec CTA proves SpecPanel mounted from ?tab=spec.
  expect(await screen.findByRole("button", { name: /start spec from skeleton/i })).toBeInTheDocument();
});
```

> Note: the `/Methods/` button exists whether the Outline is still in the editor (Tasks 4–5) or in the sidebar (Task 6) — the reviewer runs the whole suite after each task, so ordering is safe.

- [ ] **Step 5: Assert the panel propagates `section_index`** (the Codex #2 fix). Add to `frontend/src/routes/Cockpit.test.tsx` (add `import { ScorecardPanel } from "../cockpit/ScorecardPanel";` to the imports):

```tsx
it("ScorecardPanel 'Go to §' passes section_index to onNavigate", async () => {
  const onNavigate = vi.fn();
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/scorecard")) {
      return Promise.resolve({
        data: {
          scorecard: {
            criteria: [
              { index: 0, name: "Impact", band: "gap", note: "", deep_link: { artifact: "draft", section_index: 2 }, weight: 1, effective_weight: 1 },
            ],
            overall_band: "gap",
            summary: "",
          },
          evaluated_draft_sha: null, evaluated_spec_sha: null, completed_at: null, stale: false, stale_reasons: [],
        },
        error: undefined,
      });
    }
    return Promise.resolve({ data: null, error: undefined }); // /evaluator-runs/latest
  }) as never);
  const qc = new QueryClient();
  render(
    <QueryClientProvider client={qc}>
      <ScorecardPanel id="ws-1" onNavigate={onNavigate} />
    </QueryClientProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: /go to §2/i }));
  expect(onNavigate).toHaveBeenCalledWith({ top_tab: "draft", section_index: 2 });
});
```

- [ ] **Step 6: Run + gate + commit**

```bash
pnpm vitest run && pnpm exec tsc --noEmit && pnpm build
git -C "$WT" branch --show-current
git -C "$WT" add frontend/src/routes/Cockpit.tsx frontend/src/cockpit/ScorecardPanel.tsx frontend/src/cockpit/ProblemsPanel.tsx frontend/src/routes/Cockpit.test.tsx
git -C "$WT" -c commit.gpgsign=false commit -m "feat(cockpit): URL-driven artifact tab; propagate section_index through nav

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: `ArtifactSectionEditor` — active section from `?sec` + publish to the store

**Files:**
- Modify: `frontend/src/editor/ArtifactSectionEditor.tsx`

**Interfaces:**
- Consumes: `useCockpitNav` (`sec`, `setSec`), `editorStore` (`setDirty`/`setCursor`/`setSave`/`resetEditorStore`), `CodeMirrorEditor` `onCursorChange` (Task 3).
- Produces: section selection now flows through `?sec`; the Section↔Full toggle is local (`view`); the editor publishes its dirty set / cursor / save-state to `editorStore` and clears it on unmount. The in-editor Outline `<nav>` is RETAINED here (its buttons call `setSec`); Task 6 moves it to the sidebar. All buffer/conflict/cite behavior is unchanged.

This task replaces the component's state model. Replace `frontend/src/editor/ArtifactSectionEditor.tsx` entirely:

```tsx
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { useCitationSource } from "../api/citations";
import { ApiError } from "../api/client";
import { useCockpitNav } from "../cockpit/cockpitNav";
import { CitationComposer } from "../cockpit/CitationComposer";
import { resetEditorStore, setCursor, setDirty, setSave } from "../cockpit/editorStore";
import { SourceViewer } from "../cockpit/SourceViewer";
import { CodeMirrorEditor } from "./CodeMirrorEditor";

type Section = { index: number; heading: string; text: string; sha: string };

type SaveVars = {
  index: number;
  content: string;
  base_commit_sha: string;
  base_section_sha: string;
};
type SaveResultLike = { head: string; section_sha: string };

export type ArtifactSectionEditorProps = {
  workspaceId: string;
  /** TanStack cache key for this artifact, e.g. ["draft", id] | ["spec", id]. */
  queryKey: [string, string];
  head: string;
  canEdit: boolean;
  sections: Section[];
  save: { mutateAsync: (v: SaveVars) => Promise<SaveResultLike>; isPending: boolean };
  refetch: () => void | Promise<unknown>;
  /** Draft passes true (pills + select-to-cite). Spec passes false. */
  citations?: boolean;
  /** Label for the whole-artifact read-only View tab. */
  fullViewLabel?: string;
};

// ApiError.message is "code: detail"; strip the code prefix, show the human detail.
function saveErrorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    const prefix = `${e.code}: `;
    return e.message.startsWith(prefix) ? e.message.slice(prefix.length) : e.message;
  }
  return "Save failed — please try again.";
}

export function ArtifactSectionEditor({
  workspaceId,
  queryKey,
  head,
  canEdit,
  sections,
  save,
  refetch,
  citations = false,
  fullViewLabel = "Full draft",
}: ArtifactSectionEditorProps) {
  const qc = useQueryClient();
  const { sec, setSec } = useCockpitNav();
  // The active SECTION index lives in the URL (?sec); the Section<->Full VIEW is a
  // local, editor-only toggle (it never crosses the sidebar boundary).
  const [view, setView] = useState<"section" | "full">("section");
  // PER-SECTION dirty buffers: edits to one section survive switching sections or
  // the Full tab and back. A section is dirty iff its buffer differs from the fetch.
  const [buffers, setBuffers] = useState<Record<number, string>>({});
  const [staleSection, setStaleSection] = useState<number | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [peekEv, setPeekEv] = useState<string | null>(null);
  const [pinnedEv, setPinnedEv] = useState<string | null>(null);
  const [selection, setSelection] = useState<{ from: number; to: number; text: string } | null>(
    null,
  );
  const [composerOpen, setComposerOpen] = useState(false);
  const peek = useCitationSource(workspaceId, peekEv);
  const pinned = useCitationSource(workspaceId, pinnedEv);

  const byIndex = useMemo(() => new Map(sections.map((s) => [s.index, s])), [sections]);
  // Clamp the URL section to one that exists in THIS artifact. An out-of-range ?sec
  // (artifact switch / deep-link before load) renders section 0 now and history-
  // replaces the URL once sections resolve — no spurious back-history entry.
  const validSec = byIndex.has(sec) ? sec : (sections[0]?.index ?? 0);
  useEffect(() => {
    if (sections.length > 0 && !byIndex.has(sec)) setSec(validSec, { replace: true });
  }, [sec, validSec, sections.length, byIndex, setSec]);

  // Side-effects of moving to a new section (mirrors the old selectSection): drop a
  // stale conflict/save banner, the claim selection, and the composer; force Section
  // view. Runs whenever ?sec changes. Does NOT touch buffers (edits must survive).
  useEffect(() => {
    setStaleSection(null);
    setSaveError(null);
    setSelection(null);
    setComposerOpen(false);
    setView("section");
  }, [sec]);

  const activeIndex = view === "full" ? -1 : validSec;
  const activeSection = activeIndex >= 0 ? byIndex.get(activeIndex) : undefined;
  const fullText = sections.map((s) => s.text).join("");
  const editorValue =
    view === "full" ? fullText : (buffers[activeIndex] ?? activeSection?.text ?? "");

  const isDirty = (idx: number) =>
    buffers[idx] !== undefined && buffers[idx] !== byIndex.get(idx)?.text;

  // Publish the dirty section set (for the sidebar Outline • markers). Recompute when
  // buffers or the fetched sections change.
  useEffect(() => {
    setDirty(
      sections.filter((s) => buffers[s.index] !== undefined && buffers[s.index] !== s.text).map((s) => s.index),
    );
  }, [buffers, sections]);

  // Publish save-state for the status bar.
  useEffect(() => {
    if (save.isPending) setSave("saving");
    else if (saveError) setSave("error");
    else if (activeIndex >= 0 && isDirty(activeIndex)) setSave("dirty");
    else setSave("saved");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [save.isPending, saveError, buffers, activeIndex]);

  // Clear the store on unmount so a stale cursor/save/dirty never leaks to the status
  // bar of a non-cockpit view or the next artifact.
  useEffect(() => () => resetEditorStore(), []);

  function onEditorChange(next: string) {
    if (activeIndex < 0) return;
    setSaveError(null);
    setBuffers((b) => ({ ...b, [activeIndex]: next }));
  }

  function reloadSection(index: number) {
    setBuffers((b) => {
      const next = { ...b };
      delete next[index];
      return next;
    });
    setStaleSection(null);
    void refetch();
  }

  async function onSave() {
    if (!activeSection) return;
    const index = activeSection.index;
    const content = buffers[index] ?? activeSection.text;
    setSaveError(null);
    try {
      const result = await save.mutateAsync({
        index,
        content,
        base_commit_sha: head,
        base_section_sha: activeSection.sha,
      });
      qc.setQueryData(queryKey, (prev: { head: string; sections: Section[] } | undefined) => {
        if (!prev) return prev;
        return {
          ...prev,
          head: result.head,
          sections: prev.sections.map((s) =>
            s.index === index ? { ...s, text: content, sha: result.section_sha } : s,
          ),
        };
      });
      setBuffers((b) => {
        const next = { ...b };
        delete next[index];
        return next;
      });
      setStaleSection(null);
    } catch (e) {
      if (e instanceof ApiError && e.code === "conflict") {
        setStaleSection(index);
        return;
      }
      setStaleSection(null);
      setSaveError(saveErrorMessage(e));
    }
  }

  const onPillClick = (_citekey: string, evId: string) => setPeekEv(evId);

  return (
    <main className="cockpit">
      <nav aria-label="Outline">
        <ul>
          {sections.map((s) => (
            <li key={s.index}>
              <button
                type="button"
                aria-current={view === "section" && validSec === s.index}
                onClick={() => setSec(s.index)}
              >
                {(s.heading || "preamble") + (isDirty(s.index) ? " •" : "")}
              </button>
            </li>
          ))}
        </ul>
      </nav>

      <section className="cockpit-main">
        <div role="tablist" aria-label="View">
          <button
            type="button"
            role="tab"
            aria-selected={view === "section"}
            onClick={() => setView("section")}
          >
            Section
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === "full"}
            onClick={() => setView("full")}
          >
            {fullViewLabel}
          </button>
        </div>

        {staleSection !== null && (
          <p role="alert">
            This section changed underneath you. Your edits are kept.{" "}
            <button type="button" onClick={() => reloadSection(staleSection)}>
              Reload (discard my edits)
            </button>{" "}
            <button type="button" onClick={() => setStaleSection(null)}>
              Keep editing
            </button>
          </p>
        )}

        {saveError !== null && (
          <p role="alert">
            {saveError}{" "}
            <button type="button" onClick={() => setSaveError(null)}>
              Dismiss
            </button>
          </p>
        )}

        <CodeMirrorEditor
          value={editorValue}
          onChange={view === "full" ? () => {} : onEditorChange}
          requireHeading={view === "section" && activeIndex > 0}
          readOnly={view === "full" || !canEdit}
          citations={citations}
          onPillClick={citations ? onPillClick : undefined}
          onSelectionChange={citations && view === "section" ? setSelection : undefined}
          onCursorChange={setCursor}
        />

        {canEdit && view === "section" && (
          <div className="cockpit-section-actions">
            <button
              type="button"
              onClick={onSave}
              disabled={save.isPending || !isDirty(activeIndex)}
            >
              Save section
            </button>
            {citations && (
              <button
                type="button"
                className="cite-selection-btn"
                disabled={!selection || isDirty(activeIndex)}
                title={isDirty(activeIndex) ? "Save this section first" : undefined}
                onClick={() => setComposerOpen(true)}
              >
                Cite selection
              </button>
            )}
          </div>
        )}
      </section>

      {citations && peekEv !== null && (
        <section className="cockpit-peek" aria-label="Citation source peek">
          {peek.isLoading && <p aria-busy="true">Loading source…</p>}
          {peek.isError && <p role="alert">Could not load the cited source.</p>}
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
        </section>
      )}

      {citations && pinnedEv !== null && (
        <aside className="cockpit-pinned" aria-label="Pinned source">
          {pinned.isLoading && <p aria-busy="true">Loading source…</p>}
          {pinned.isError && <p role="alert">Could not load the cited source.</p>}
          {pinned.data && (
            <SourceViewer
              source={pinned.data}
              pinned
              onClose={() => setPinnedEv(null)}
              onPin={() => {}}
              onNavigate={(evId) => setPinnedEv(evId)}
            />
          )}
        </aside>
      )}

      {citations && composerOpen && selection && activeSection && (
        <section className="cockpit-composer">
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
        </section>
      )}
    </main>
  );
}
```

- [ ] **Step 1: Add `resetEditorStore` to the Cockpit test teardown.** In `frontend/src/routes/Cockpit.test.tsx`, add the import and call so the singleton store never leaks across tests:

```tsx
import { resetEditorStore } from "../cockpit/editorStore";
// ...
afterEach(() => {
  vi.restoreAllMocks();
  resetEditorStore();
});
```

- [ ] **Step 2: Run the existing Cockpit suite — Expected: PASS.** The Outline buttons still exist (in-editor) and now drive `?sec`; the Section/Full toggle stays local; buffers/conflict/cite behavior is unchanged. The "lastSectionIndex restore" assertion holds because `?sec` persists across the Full toggle.

Run: `pnpm vitest run src/routes/Cockpit.test.tsx`

- [ ] **Step 3: Add the clamp test** to `frontend/src/routes/Cockpit.test.tsx` (add `useLocation` to the `react-router-dom` import; add the `LocationProbe` helper once):

```tsx
// add near the top, after the other helpers:
function LocationProbe() {
  return <span data-testid="loc">{useLocation().search}</span>;
}

it("an out-of-range ?sec clamps to a valid section and history-replaces the URL", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draftPayload(true) as never);
  const qc = new QueryClient();
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/cockpit/ws-1?sec=99"]}>
        <Routes>
          <Route path="/cockpit/:id" element={<><Cockpit /><LocationProbe /></>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  // The editor renders the preamble (section 0), not a blank doc...
  await waitFor(() =>
    expect(document.querySelector(".cm-content")?.textContent).toContain("preamble"),
  );
  // ...and the URL self-corrected to a valid section.
  await waitFor(() => expect(screen.getByTestId("loc").textContent).toContain("sec=0"));
});
```

- [ ] **Step 4: Gate + commit**

```bash
pnpm exec tsc --noEmit && pnpm vitest run && pnpm build
git -C "$WT" branch --show-current
git -C "$WT" add frontend/src/editor/ArtifactSectionEditor.tsx frontend/src/routes/Cockpit.test.tsx
git -C "$WT" -c commit.gpgsign=false commit -m "feat(editor): drive active section from ?sec; publish dirty/cursor/save

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Relocate the Outline into `CockpitSidebar` (the route `handle.Sidebar`)

**Files:**
- Create: `frontend/src/cockpit/SidebarGroup.tsx`, `frontend/src/cockpit/CockpitSidebar.tsx`, `frontend/src/cockpit/CockpitSidebar.module.css`, `frontend/src/cockpit/CockpitSidebar.test.tsx`
- Modify: `frontend/src/editor/ArtifactSectionEditor.tsx` (remove the in-editor `<nav>`), `frontend/src/styles/cockpit.css` (`.cockpit` grid drops the outline column), `frontend/src/router.tsx` (handle), `frontend/src/routes/Cockpit.test.tsx` (`renderCockpit` mounts the sidebar)

**Interfaces:**
- Consumes: `useDraft`/`useSpec` (`api/editor`), `useCockpitNav`, `useEditorStore`, `SidebarGroup`.
- Produces: `CockpitSidebar` (default-exported as a component reference for `handle.Sidebar`) rendering the Outline group; `SidebarGroup({ title, count?, defaultOpen?, children })`.

- [ ] **Step 1: Create `frontend/src/cockpit/SidebarGroup.tsx`:**

```tsx
import { type ReactNode, useState } from "react";

import styles from "./CockpitSidebar.module.css";

// A collapsible VS Code-style sidebar group: codicon chevron + title + count.
export function SidebarGroup({
  title,
  count,
  defaultOpen = true,
  children,
}: {
  title: string;
  count?: number;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className={styles.group}>
      <button
        type="button"
        className={styles.groupHeader}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className={`codicon codicon-chevron-${open ? "down" : "right"}`} aria-hidden="true" />
        <span className={styles.groupTitle}>{title}</span>
        {count != null && <span className={styles.groupCount}>{count}</span>}
      </button>
      {open && <div className={styles.groupBody}>{children}</div>}
    </section>
  );
}
```

- [ ] **Step 2: Create `frontend/src/cockpit/CockpitSidebar.tsx`:**

```tsx
import { useParams } from "react-router-dom";

import { useDraft, useSpec } from "../api/editor";
import { useCockpitNav } from "./cockpitNav";
import styles from "./CockpitSidebar.module.css";
import { useEditorStore } from "./editorStore";
import { SidebarGroup } from "./SidebarGroup";

// The cockpit's left sidebar (the route's handle.Sidebar). Reads the SAME
// useDraft/useSpec queries + ?tab/?sec as the editor — React Query dedupes by key,
// the URL is the shared channel — so the Outline stays in sync with the editor across
// the AppShell boundary with no props. Dirty markers come from editorStore, which the
// mounted editor publishes. Both hooks are always called (stable hook order); on the
// scorecard/review tabs the Outline shows the Draft sections and a click navigates
// into Draft.
export function CockpitSidebar() {
  const { id = "" } = useParams();
  const { tab, sec, navigate } = useCockpitNav();
  const artifact: "draft" | "spec" = tab === "spec" ? "spec" : "draft";
  const draft = useDraft(id);
  const spec = useSpec(id);
  const q = artifact === "spec" ? spec : draft;
  const { dirty } = useEditorStore();
  const sections = q.data?.sections ?? [];

  return (
    <div className={styles.sidebar}>
      <SidebarGroup title="Outline" count={sections.length || undefined}>
        {q.isLoading && <p className={styles.note}>Loading…</p>}
        {!q.isLoading && sections.length === 0 && <p className={styles.note}>No sections.</p>}
        <ul className={styles.outline} aria-label="Outline">
          {sections.map((s) => (
            <li key={s.index}>
              <button
                type="button"
                className={styles.row}
                aria-current={sec === s.index}
                onClick={() => navigate({ top_tab: artifact, section_index: s.index })}
              >
                <span className={styles.rowLabel}>{s.heading || "preamble"}</span>
                {dirty.includes(s.index) && (
                  <span className={styles.dirty} aria-label="unsaved">
                    •
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      </SidebarGroup>
    </div>
  );
}
```

- [ ] **Step 3: Create `frontend/src/cockpit/CockpitSidebar.module.css`:**

```css
.sidebar {
  display: flex;
  flex-direction: column;
}
.group {
  display: flex;
  flex-direction: column;
}
.groupHeader {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  width: 100%;
  border: none;
  background: transparent;
  padding: 0.3rem 0.5rem;
  cursor: pointer;
  color: var(--app-muted);
  font-size: 0.7rem;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.groupHeader:hover {
  color: var(--app-text);
}
.groupTitle {
  flex: 1;
  text-align: left;
}
.groupCount {
  color: var(--app-dim);
}
.groupBody {
  display: flex;
  flex-direction: column;
}
.outline {
  list-style: none;
  margin: 0;
  padding: 0;
}
.row {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  width: 100%;
  border: none;
  background: transparent;
  padding: 0.25rem 0.5rem 0.25rem 1.25rem;
  cursor: pointer;
  color: var(--app-text);
  font-size: 0.85rem;
  text-align: left;
}
.row:hover {
  background: var(--app-hover);
}
.row[aria-current="true"] {
  background: var(--app-selected);
  box-shadow: inset 2px 0 0 var(--app-accent);
}
.rowLabel {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dirty {
  color: var(--app-warn);
}
.note {
  color: var(--app-muted);
  padding: 0.25rem 0.5rem 0.25rem 1.25rem;
  font-size: 0.85rem;
}
```

- [ ] **Step 4: Remove the in-editor Outline.** In `frontend/src/editor/ArtifactSectionEditor.tsx`, DELETE the entire `<nav aria-label="Outline"> … </nav>` block (the first child of `<main className="cockpit">`). The `<main>` now starts directly with `<section className="cockpit-main">`.

- [ ] **Step 5: Drop the outline grid column.** In `frontend/src/styles/cockpit.css`, change the `.cockpit` grid (so the editor takes the first column now that the nav is gone):

```css
.cockpit {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  gap: 1rem;
  align-items: start;
}
```

- [ ] **Step 6: Register the handle** in `frontend/src/router.tsx` — import and attach (no route added):

```tsx
import { CockpitSidebar } from "./cockpit/CockpitSidebar";
// ...
      { path: "cockpit/:id", element: <Cockpit />, handle: { Sidebar: CockpitSidebar } },
```

- [ ] **Step 7: Update the test harness** so the relocated Outline is in the document. In `frontend/src/routes/Cockpit.test.tsx`, import `CockpitSidebar` and render it alongside `Cockpit` as the route element (the Outline buttons the tests click now come from the sidebar; `screen` queries the whole document, so the existing `getByRole("button", { name: /Methods/ })` calls keep working):

```tsx
import { CockpitSidebar } from "../cockpit/CockpitSidebar";
// ...
function renderCockpit(url = "/cockpit/ws-1") {
  const qc = new QueryClient();
  return {
    qc,
    ...render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[url]}>
          <Routes>
            <Route
              path="/cockpit/:id"
              element={
                <>
                  <CockpitSidebar />
                  <Cockpit />
                </>
              }
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}
```

- [ ] **Step 8: Write the `CockpitSidebar` test** `frontend/src/cockpit/CockpitSidebar.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { CockpitSidebar } from "./CockpitSidebar";
import { resetEditorStore, setDirty } from "./editorStore";

afterEach(() => {
  vi.restoreAllMocks();
  resetEditorStore();
});

const draft = {
  data: {
    head: "a".repeat(40),
    can_edit: true,
    sections: [
      { index: 0, heading: "", text: "preamble\n", sha: "s0" },
      { index: 1, heading: "Methods", text: "## Methods\n", sha: "s1" },
    ],
    citations: {},
  },
  error: undefined,
};

function Probe() {
  return <span data-testid="search">{useLocation().search}</span>;
}
function renderSidebar(url = "/cockpit/ws-1") {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route
            path="/cockpit/:id"
            element={
              <>
                <CockpitSidebar />
                <Probe />
              </>
            }
          />
        </Routes>
      </QueryClientProvider>,
    );
}

it("renders the Outline sections", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draft as never);
  renderSidebar();
  expect(await screen.findByRole("button", { name: /Methods/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /preamble/i })).toBeInTheDocument();
});

it("clicking a section navigates to ?tab=draft&sec=N", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draft as never);
  renderSidebar();
  fireEvent.click(await screen.findByRole("button", { name: /Methods/ }));
  expect(screen.getByTestId("search").textContent).toContain("tab=draft");
  expect(screen.getByTestId("search").textContent).toContain("sec=1");
});

it("shows a dirty marker for a section the store reports", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draft as never);
  renderSidebar();
  await screen.findByRole("button", { name: /Methods/ });
  setDirty([1]);
  expect(await screen.findByLabelText("unsaved")).toBeInTheDocument();
});
```

- [ ] **Step 9: Run + gate + commit** (full suite green — the relocated Outline serves the Cockpit tests via the updated harness):

```bash
pnpm vitest run && pnpm exec tsc --noEmit && pnpm build
git -C "$WT" branch --show-current
git -C "$WT" add frontend/src/cockpit/SidebarGroup.tsx frontend/src/cockpit/CockpitSidebar.tsx frontend/src/cockpit/CockpitSidebar.module.css frontend/src/cockpit/CockpitSidebar.test.tsx frontend/src/editor/ArtifactSectionEditor.tsx frontend/src/styles/cockpit.css frontend/src/router.tsx frontend/src/routes/Cockpit.test.tsx
git -C "$WT" -c commit.gpgsign=false commit -m "feat(cockpit): relocate the Outline into the shell sidebar (handle.Sidebar)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Rewrite `cockpit.css` onto `--app-*` (IDE tabs / bottom panel / scorecard reskin)

**Files:**
- Rewrite: `frontend/src/styles/cockpit.css`

**Interfaces:** none (pure CSS). Tokens from `frontend/src/theme/theme.css`; tinted pill backgrounds via `color-mix`. The `.cockpit` grid stays the Task-6 3-column form. Behavior/classnames unchanged (tests assert classes like `.cm-citation-pill`, `.source-viewer`, `.band-pill`, `.bottom-panel`, `.scorecard-link` — keep them).

- [ ] **Step 1: Replace `frontend/src/styles/cockpit.css` entirely:**

```css
/* Cockpit layout: editor (+ center peek) | pinned source. The Outline moved to the
   shell sidebar (CockpitSidebar) in Phase 3a. Plain global classes (the Cockpit uses
   bare className strings). Tokens come from theme/theme.css (--app-*). */
.cockpit {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  gap: 1rem;
  align-items: start;
}
.cockpit-main {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  min-width: 0;
}
.cockpit-peek,
.cockpit-pinned {
  min-width: 18rem;
  max-width: 28rem;
}

.source-viewer {
  border: 1px solid var(--app-border);
  border-radius: 8px;
  background: var(--app-sidebar);
  box-shadow: var(--app-shadow-sm);
  padding: 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.source-viewer-head {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  flex-wrap: wrap;
}
.source-viewer-title {
  font-weight: 600;
  color: var(--app-text);
}
.source-viewer-pos {
  color: var(--app-muted);
  font-size: 0.85em;
}
.source-viewer-actions {
  margin-left: auto;
  display: flex;
  gap: 0.25rem;
}
.source-viewer-actions button {
  border: 1px solid var(--app-border);
  background: var(--app-editor);
  border-radius: 4px;
  padding: 0 0.4rem;
  cursor: pointer;
  color: var(--app-text);
}
.source-viewer-actions button:disabled {
  opacity: 0.4;
  cursor: default;
}
.source-viewer-body {
  white-space: pre-wrap;
  max-height: 60vh;
  overflow: auto;
  font-size: 0.92em;
  line-height: 1.5;
  color: var(--app-text);
}
.source-viewer-body mark {
  background: color-mix(in srgb, var(--app-warn) 30%, transparent);
  color: var(--app-text);
  border-radius: 2px;
  padding: 0 1px;
}
.source-viewer-claim {
  font-size: 0.9em;
  color: var(--app-muted);
}
.source-viewer-empty,
.source-viewer-note {
  color: var(--app-muted);
  font-size: 0.85em;
}
.source-viewer-tombstoned {
  color: var(--app-bad);
  background: color-mix(in srgb, var(--app-bad) 16%, transparent);
  border: 1px solid color-mix(in srgb, var(--app-bad) 40%, transparent);
  border-radius: 4px;
  padding: 0.25rem 0.5rem;
  font-size: 0.85em;
}
.source-viewer-stance {
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

/* select-to-cite: the Save/Cite action row, the composer pane + surface. */
.cockpit-section-actions {
  display: flex;
  align-items: center;
}
.cite-selection-btn {
  margin-left: 0.5rem;
}
.cockpit-composer {
  min-width: 20rem;
  max-width: 30rem;
}
.cite-composer {
  border: 1px solid var(--app-border);
  border-radius: 8px;
  background: var(--app-sidebar);
  box-shadow: var(--app-shadow-sm);
  padding: 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.cite-composer-head {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
}
.cite-composer-head button {
  margin-left: auto;
}
.cite-composer-claim {
  color: var(--app-muted);
  font-style: italic;
}
.cite-composer-chunk {
  white-space: pre-wrap;
  border: 1px solid var(--app-border);
  border-radius: 4px;
  padding: 0.4rem;
  font-size: 0.9em;
  cursor: text;
  max-height: 40vh;
  overflow: auto;
}
.cite-composer-confirm {
  background: var(--app-selected);
  border: 1px solid var(--app-border-strong);
  border-radius: 4px;
  padding: 0.4rem;
}

/* The artifact [Draft | Spec | Scorecard | Panel review] tab strip — IDE editor tabs. */
.cockpit-shell {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.cockpit-shell > [role="tablist"] {
  display: flex;
  gap: 0;
  background: var(--app-tabbar);
  border-bottom: 1px solid var(--app-border);
}
.cockpit-shell > [role="tablist"] button[role="tab"] {
  border: none;
  border-right: 1px solid var(--app-border);
  background: transparent;
  padding: 0.35rem 0.9rem;
  cursor: pointer;
  color: var(--app-muted);
}
.cockpit-shell > [role="tablist"] button[role="tab"]:hover {
  color: var(--app-text);
}
.cockpit-shell > [role="tablist"] button[role="tab"][aria-selected="true"] {
  background: var(--app-editor);
  color: var(--app-text);
  font-weight: 600;
  box-shadow: inset 0 -2px 0 var(--app-accent);
}
.spec-empty {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.5rem;
  padding: 1rem;
}

/* The editor's own [Section | Full] view toggle. */
.cockpit-main > [role="tablist"] {
  display: flex;
  gap: 0.25rem;
}
.cockpit-main > [role="tablist"] button[role="tab"] {
  border: 1px solid var(--app-border);
  background: var(--app-tabbar);
  border-radius: 6px 6px 0 0;
  padding: 0.2rem 0.6rem;
  cursor: pointer;
  color: var(--app-muted);
}
.cockpit-main > [role="tablist"] button[role="tab"][aria-selected="true"] {
  background: var(--app-editor);
  color: var(--app-text);
  font-weight: 600;
}

/* spec lock banner + owner toggle bar. */
.spec-shell {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.spec-lock-banner {
  margin: 0;
  padding: 0.4rem 0.75rem;
  border: 1px solid var(--app-border);
  border-left: 3px solid var(--app-accent);
  background: var(--app-tabbar);
  border-radius: 6px;
  color: var(--app-text);
  font-weight: 600;
}
.spec-lock-bar {
  display: flex;
  gap: 0.5rem;
}

/* inline pending-proposal review strip. */
.proposal-review {
  border: 1px solid var(--app-border);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
  background: var(--app-tabbar);
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.proposal-review-title { margin: 0; font-size: 0.95rem; }
.proposal-item { list-style: none; border-top: 1px solid var(--app-border); padding-top: 0.5rem; }
.proposal-meta { margin: 0 0 0.25rem; color: var(--app-muted); font-size: 0.8rem; }
.proposal-diff { margin: 0; white-space: pre-wrap; font-size: 0.85rem; }
.proposal-diff .diff-add { background: rgba(46, 160, 67, 0.15); }
.proposal-diff .diff-del { background: rgba(248, 81, 73, 0.15); }
.proposal-actions { display: flex; gap: 0.5rem; margin-top: 0.4rem; }
.draft-shell { display: flex; flex-direction: column; gap: 0.5rem; }
.coherence-bar { display: flex; align-items: center; gap: 0.75rem; margin: 0.5rem 0; }
.coherence-status { font-size: 0.85rem; color: var(--app-muted); }
.coherence-error { color: var(--app-bad); }

/* Scorecard tab — band pills + readiness/staleness meta + criteria rows. */
.scorecard-shell { display: flex; flex-direction: column; gap: 0.5rem; }
.scorecard-header { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
.scorecard-header h1 { margin: 0; }
.scorecard-meta { margin: 0; font-size: 0.85rem; color: var(--app-muted); }
.scorecard-summary { margin: 0.25rem 0 0; color: var(--app-muted); }
.scorecard-empty { margin: 0.5rem 0; color: var(--app-muted); }
.scorecard-list { list-style: none; padding: 0; margin: 0; }
.scorecard-name { font-weight: 600; }
.scorecard-weight { font-size: 0.8rem; color: var(--app-muted); }
.scorecard-link {
  background: none;
  border: none;
  padding: 0;
  color: var(--app-link);
  cursor: pointer;
  font-size: 0.85rem;
}
.band-pill {
  display: inline-block;
  font-size: 0.75em;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  padding: 0 0.4rem;
  border-radius: 999px;
  border: 1px solid transparent;
}
.band-strong   { background: color-mix(in srgb, var(--app-good) 16%, transparent); color: var(--app-good); border-color: color-mix(in srgb, var(--app-good) 40%, transparent); }
.band-adequate { background: var(--app-hover); color: var(--app-muted); border-color: var(--app-border); }
.band-at-risk  { background: color-mix(in srgb, var(--app-warn) 16%, transparent); color: var(--app-warn); border-color: color-mix(in srgb, var(--app-warn) 40%, transparent); }
.band-gap      { background: color-mix(in srgb, var(--app-bad) 16%, transparent); color: var(--app-bad); border-color: color-mix(in srgb, var(--app-bad) 40%, transparent); }
.scorecard-row { list-style: none; border-top: 1px solid var(--app-border); padding-top: 0.5rem; margin-top: 0.5rem; }
.scorecard-note { margin: 0.25rem 0 0; white-space: pre-wrap; }
.scorecard-stale { color: var(--app-warn); }
.scorecard-error { color: var(--app-bad); }

/* bottom-panel region + Problems dashboard — IDE panel. */
.bottom-panel {
  border-top: 1px solid var(--app-border);
  margin-top: 0;
  background: var(--app-editor);
}
.bottom-panel-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  padding: 0.25rem 0.5rem;
  background: var(--app-tabbar);
}
.bottom-panel-tabs {
  display: flex;
  gap: 0.25rem;
}
.bottom-panel-tabs [role="tab"] {
  border: none;
  background: transparent;
  padding: 0.2rem 0.5rem;
  cursor: pointer;
  color: var(--app-muted);
}
.bottom-panel-tabs [role="tab"][aria-selected="true"] {
  color: var(--app-text);
  font-weight: 600;
  border-bottom: 2px solid var(--app-accent);
}
.bottom-panel-toggle {
  border: none;
  background: transparent;
  color: var(--app-muted);
  cursor: pointer;
}
.bottom-panel-body {
  max-height: 14rem;
  overflow-y: auto;
  padding: 0.5rem;
}
.problems-group-title {
  font-size: 0.85rem;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  color: var(--app-muted);
  margin: 0.5rem 0 0.25rem;
}
.problems-stale {
  color: var(--app-warn);
  font-weight: 400;
  text-transform: none;
}
.problem-row {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  padding: 0.2rem 0;
}
.problem-row.severity-error .problem-sev {
  color: var(--app-bad);
}
.problem-row.severity-warning .problem-sev {
  color: var(--app-warn);
}
.problem-detail {
  color: var(--app-muted);
  font-size: 0.9rem;
}
.problem-act {
  margin-left: auto;
}
.problems-empty,
.inbox-empty {
  color: var(--app-muted);
  padding: 0.75rem;
}
.problems-error {
  color: var(--app-bad);
  padding: 0.5rem;
}

/* Activity feed */
.activity-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.activity-row {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 0.3rem;
  padding: 0.25rem 0;
  font-size: 0.9rem;
}
.activity-actor {
  font-weight: 600;
}
.activity-time {
  color: var(--app-muted);
}
.activity-act {
  margin-left: auto;
}
.activity-empty,
.activity-loading {
  color: var(--app-muted);
  padding: 0.75rem;
}
.activity-error {
  color: var(--app-bad);
  padding: 0.5rem;
}
```

- [ ] **Step 2: Gate + commit.** No new tests (CSS-only; the existing suite asserts classnames which are unchanged; visual is screenshot-verified at the end). Confirm `build` compiles the `color-mix` CSS.

```bash
pnpm exec tsc --noEmit && pnpm vitest run && pnpm build
git -C "$WT" branch --show-current
git -C "$WT" add frontend/src/styles/cockpit.css
git -C "$WT" -c commit.gpgsign=false commit -m "style(cockpit): rewrite cockpit.css onto --app-* (IDE tabs/panel/scorecard)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Retire `--cm-*` — migrate shared modules, add `--app-shadow-sm`, delete the remap + tokens.css

**Files:**
- Modify: `frontend/src/components/Button.module.css`, `frontend/src/components/Select.module.css`, `frontend/src/components/Card.module.css`
- Modify: `frontend/src/theme/theme.css` (add `--app-shadow-sm`; delete the `--cm-*` remap block)
- Modify: `frontend/src/main.tsx` (drop the `styles/tokens.css` import)

**Interfaces:** none. After this task NOTHING in the SPA reads `var(--cm-`.

- [ ] **Step 1: Add the shadow token + delete the remap.** In `frontend/src/theme/theme.css`, add one line inside the `:root { … }` block (next to the other `--app-*` declarations, before `color-scheme: dark;`):

```css
  --app-shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.4);
```

Then DELETE the entire dark-remap block at the bottom of the file — the comment `/* Phase-1 dark compatibility remap … */` and the whole `:root[data-mantine-color-scheme="dark"] { … }` rule (every `--cm-*` declaration).

- [ ] **Step 2: Migrate `Button.module.css`:**

```css
.btn {
  background: var(--app-accent-bright);
  color: #06201d;
  border: 1px solid var(--app-border-strong);
  border-radius: 8px;
  padding: 0.5rem 0.9rem;
  cursor: pointer;
}
.btn:hover {
  background: var(--app-accent);
}
.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
```

- [ ] **Step 3: Migrate `Select.module.css`:**

```css
.select {
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 0.4rem 0.6rem;
  background: var(--app-sidebar);
  color: var(--app-text);
}
```

- [ ] **Step 4: Migrate `Card.module.css`:**

```css
.card {
  background: var(--app-sidebar);
  border: 1px solid var(--app-border);
  border-radius: 12px;
  box-shadow: var(--app-shadow-sm);
  padding: 1rem;
}
```

- [ ] **Step 5: Drop the legacy token sheet.** In `frontend/src/main.tsx`, DELETE the line `import "./styles/tokens.css";` (keep the `cockpit.css` and `theme.css` imports).

- [ ] **Step 6: Verify nothing reads `--cm-*` anymore.**

Run: `! grep -rn "var(--cm-" frontend/src`
Expected: prints nothing and exits 0 (the `!` inverts grep's exit-1-on-no-match). If any line prints, migrate it before continuing.

- [ ] **Step 7: Gate + commit**

```bash
pnpm exec tsc --noEmit && pnpm vitest run && pnpm build
git -C "$WT" branch --show-current
git -C "$WT" add frontend/src/components/Button.module.css frontend/src/components/Select.module.css frontend/src/components/Card.module.css frontend/src/theme/theme.css frontend/src/main.tsx
git -C "$WT" -c commit.gpgsign=false commit -m "refactor(spa): retire --cm-* tokens (migrate modules; delete remap + tokens.css)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: Populate the status bar (Ln/Col · language · save state) on the cockpit

**Files:**
- Modify: `frontend/src/shell/StatusBar.tsx`
- Create: `frontend/src/shell/StatusBar.test.tsx`

**Interfaces:**
- Consumes: `useEditorStore` (cursor + save), `react-router-dom` `useLocation`.
- Produces: on `/cockpit*` the status bar shows save-state · `Markdown` · `Ln/Col`; elsewhere it stays the minimal global bar. (Deadline · spec-lock · scorecard band · branch indicator are Plan 3b.)

- [ ] **Step 1: Write the failing test** `frontend/src/shell/StatusBar.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it } from "vitest";

import { resetEditorStore, setCursor, setSave } from "../cockpit/editorStore";
import { StatusBar } from "./StatusBar";

afterEach(() => resetEditorStore());

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <StatusBar />
    </MemoryRouter>,
  );
}

it("off the cockpit, shows only the global label", () => {
  renderAt("/");
  expect(screen.getByText("Call Manager")).toBeInTheDocument();
  expect(screen.queryByText("Markdown")).not.toBeInTheDocument();
});

it("on the cockpit, shows language + Ln/Col + save state", () => {
  setCursor({ line: 12, col: 4 });
  setSave("dirty");
  renderAt("/cockpit/ws-1");
  expect(screen.getByText("Markdown")).toBeInTheDocument();
  expect(screen.getByText("Ln 12, Col 4")).toBeInTheDocument();
  expect(screen.getByText("Unsaved")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run it — Expected: FAIL** (the current StatusBar renders neither `Markdown` nor Ln/Col).

Run: `pnpm vitest run src/shell/StatusBar.test.tsx`

- [ ] **Step 3: Implement** `frontend/src/shell/StatusBar.tsx`:

```tsx
import { useLocation } from "react-router-dom";

import { type SaveState, useEditorStore } from "../cockpit/editorStore";
import styles from "./StatusBar.module.css";

const SAVE_LABEL: Record<SaveState, string> = {
  idle: "",
  saving: "Saving…",
  saved: "Saved",
  dirty: "Unsaved",
  error: "Save failed",
};

// Phase 3a: a global status bar that gains cockpit context (save-state, language,
// Ln/Col) on the /cockpit route. Deadline, spec-lock, and scorecard band are wired
// in Plan 3b. Reads the editorStore (published by the mounted editor) + the URL —
// the same global-channel pattern the rest of the cockpit uses across the shell.
export function StatusBar() {
  const onCockpit = useLocation().pathname.startsWith("/cockpit");
  const { cursor, save } = useEditorStore();
  return (
    <footer className={styles.statusbar} aria-label="Status bar">
      <span className={styles.item}>Call Manager</span>
      <span className={styles.spacer} />
      {onCockpit && (
        <>
          {SAVE_LABEL[save] && <span className={styles.item}>{SAVE_LABEL[save]}</span>}
          <span className={styles.item}>Markdown</span>
          {cursor && (
            <span className={styles.item}>
              Ln {cursor.line}, Col {cursor.col}
            </span>
          )}
        </>
      )}
    </footer>
  );
}
```

- [ ] **Step 4: Run it — Expected: PASS** (2 tests). Existing `AppShell.test.tsx` stays green (StatusBar still renders "Call Manager"; `useLocation` is satisfied by the test's router).

Run: `pnpm vitest run src/shell/StatusBar.test.tsx src/shell/AppShell.test.tsx`

- [ ] **Step 5: Gate + commit**

```bash
pnpm exec tsc --noEmit && pnpm vitest run && pnpm build
git -C "$WT" branch --show-current
git -C "$WT" add frontend/src/shell/StatusBar.tsx frontend/src/shell/StatusBar.test.tsx
git -C "$WT" -c commit.gpgsign=false commit -m "feat(shell): populate the status bar (Ln/Col, language, save) on the cockpit

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (after Task 9)

- [ ] **Whole-suite gate** from `frontend/`: `pnpm exec tsc --noEmit` + `pnpm vitest run` + `pnpm build`, all green.
- [ ] **Token retirement proof:** `! grep -rn "var(--cm-" frontend/src` prints nothing; `! grep -rn "tokens.css" frontend/src` prints nothing.
- [ ] **Visual check** (per `reference_spa_visual_check`): backendless `pnpm build` + static-serve, mock `/api/workspaces/*/draft|spec|problems|proposed-changes` via Playwright `page.route`, screenshot `/cockpit/ws-1`. Confirm: the Outline renders in the left sidebar (collapsible group, active-section highlight, `•` dirty marker), the VS Code editor theme (dark, teal headings/caret, line gutter), the IDE artifact tabs + bottom panel, the teal status bar showing `Markdown` + `Ln/Col`.

## STOP-BEFORE-MERGE (for a detached run)

Implement, review, fix, and COMMIT all 9 tasks on `feat/spa-vscode-redesign-phase3a`; run the final whole-branch review; record findings. Then STOP — do NOT merge, rebase, push, or touch the main working tree. The human verifies independently and merges on reconnect.
