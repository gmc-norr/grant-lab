# Workspace Rail Information Architecture — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every workspace view its own rail icon and its own pane, so Versions is one
click away instead of fourth in a folded accordion below the fold.

**Architecture:** The rail splits into two bands — app destinations, then views of the open
workspace. `?view` picks which single view the side pane renders; `?tab` picks what the
middle renders. `CockpitSidebar` becomes a switch over five pane components, one per file.
The History tab stops being a tab: the comparison is opened by picking versions in the
pane, exactly as opening a file in VS Code puts it in the editor.

**Tech Stack:** React 18, TypeScript, Vite, Mantine 9.4.0, react-router-dom 7.18.2,
`@vscode/codicons` 0.0.45, vitest + jsdom + Testing Library, pnpm.

**Spec:** `docs/superpowers/specs/2026-08-28-workspace-rail-ia-design.md` revision 5
(`0cd711a8`), four outside review rounds, 16/16 findings confirmed.

## Global Constraints

- **Frontend only.** `git diff --stat <base>..HEAD -- src/` must be empty at every commit.
  Do not run `uv sync`; do not touch the Python environment.
- **Gates**, from `frontend/`: `pnpm exec tsc --noEmit`, `pnpm vitest run`, `pnpm build`.
  Baseline at plan time: **`Test Files 116 passed (116)`**, **`Tests 748 passed (748)`**.
- **vitest's false-pass signature:** `include` is `src/**/*.{test,spec}.{ts,tsx}`. A test
  file outside `src/`, or misnamed, is **silently not collected** — exit 0, all green, zero
  coverage. Judge every run on the printed `Test Files N passed (N)` and
  `Tests M passed (M)`, never on the absence of red. Paste both lines into every report.
- **No new dependencies.** `pnpm add` is refused by a 7-day `minimumReleaseAge` cooldown.
  Needing one is an escalation, not a decision.
- **A mutation that breaks an import proves nothing** — it yields a collection error. A
  mutation proof needs a **named test failing on an assertion**.
- **Three writers of `?tab` exist, and all three guard against discarding unsaved edits:**
  `Cockpit.onTab`, `historyNav.selectAndOpen`, `cockpitNav.navigate`. Nothing structural
  enforces this. **Any code in this plan that writes `?tab` carries the same obligation.**
  The eight existing guard tests (four in `historyNav.test.tsx`, four in
  `cockpitNav.test.tsx`) must stay green.
- **`window.confirm` must never run inside a `setSearchParams` updater.** Updaters are
  expected to be pure and run twice under StrictMode. Guard before calling `setSp`.
- **Two `setSearchParams` writers in one handler clobber each other.** react-router 7.18.2
  gives a functional updater the params memoised at **render** time, so both build from the
  pre-click URL and the second discards the first. One write per handler. The pinned test
  in `historyNav.test.tsx` holds this.
- **Copy rules:** the rail labels are exactly `Calls`, `Workspaces`, `Outline`, `Versions`,
  `References`, `Criteria`, `Members`, `Profiles`, `Settings`, `Admin`. Pane headers use the
  same words as their rail labels.
- **Seed every URL-preservation test with every parameter the rule protects.** A gate
  seeded with less than the rule covers passes on an implementation that drops the rest.
  This plan's spec had three such gates before review caught them.

---

## File Structure

**Create**

| File | Responsibility |
| --- | --- |
| `frontend/src/cockpit/panes/PaneHeader.tsx` | The one pane header: title + optional count. Replaces `SidebarGroup`'s chrome. |
| `frontend/src/cockpit/panes/OutlinePane.tsx` | Section list for the active artifact, with the dirty marker and the jump. |
| `frontend/src/cockpit/panes/ReferencesPane.tsx` | Read-only reference list. |
| `frontend/src/cockpit/panes/CriteriaPane.tsx` | Criterion list plus the owner's Edit-rubric modal. |
| `frontend/src/cockpit/panes/MembersPane.tsx` | Member list plus add/remove for owners. |

**Modify**

| File | Change |
| --- | --- |
| `frontend/src/cockpit/cockpitNav.ts` | `?view` parsing, `viewHref` merge helper; later `diff`, the alias, `?sec` bound. |
| `frontend/src/cockpit/nav.ts` | `TopTab` union gains `diff`, loses `history`; add `View`. |
| `frontend/src/shell/ActivityBar.tsx` | Two bands with a divider; drop the dynamic item. |
| `frontend/src/shell/ActivityBar.module.css` | The divider. |
| `frontend/src/shell/nav-items.ts` | Split into app / workspace / bottom groups. |
| `frontend/src/cockpit/CockpitSidebar.tsx` | Becomes a switch over the five panes. |
| `frontend/src/cockpit/VersionGraph.tsx` | Drops `SidebarGroup` and `openWhen`; gains `PaneHeader` and the Draft/Spec switch. |
| `frontend/src/cockpit/HistoryPanel.tsx` | Loses the Draft/Spec switch. |
| `frontend/src/cockpit/historyNav.ts` | `selectAndOpen` writes `diff`, guards on the parsed destination, sets the tab only for a non-empty selection. |
| `frontend/src/routes/Cockpit.tsx` | Tab strip loses History; `diff` renders `HistoryPanel`. |

**Delete**

| File | Why |
| --- | --- |
| `frontend/src/cockpit/SidebarGroup.tsx` | No consumers once the accordion is gone. |

**Unchanged:** `diff.ts`, `DiffView.tsx`, `SideBySideDiffView.tsx`, `CallsFilters.tsx`,
`WorkspacesFilters.tsx`, and all backend code.

---
## Slice 1 — the rail and the pane

After Slice 1, Versions is one rail click away and nothing is folded. `?tab=history` still
works, and the comparison still lives behind the History tab.

### Task 1: `?view` — parsing and the merge helper

**Files:**
- Modify: `frontend/src/cockpit/nav.ts:1`
- Modify: `frontend/src/cockpit/cockpitNav.ts:7-19`
- Test: `frontend/src/cockpit/cockpitNav.test.tsx`

**Interfaces:**
- Consumes: `tabFromParams(sp: URLSearchParams): TopTab` — already exported.
- Produces:
  - `export type View = "outline" | "versions" | "references" | "criteria" | "members";`
  - `export const VIEWS: View[]`
  - `export function viewFromParams(sp: URLSearchParams): View`
  - `export function viewHref(sp: URLSearchParams, view: View, pathname: string): { pathname: string; search: string }`
  - `useCockpitNav()` gains `view: View` and `setView: (v: View) => void`.

**Why `viewFromParams` reads the tab.** An absent `?view` defaults to `versions` when the
middle is showing the comparison, `outline` otherwise. In this slice the comparison
destination is `"history"`; Slice 2 adds `"diff"` beside it. Written as a set so the rule
does not have to change when the name does. Without this, `?tab=history` shows the Outline
while `HistoryPanel` says "pick two versions from the Versions list in the side pane" — the
original defect, rebuilt.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/cockpit/cockpitNav.test.tsx`:

```tsx
import { VIEWS, viewFromParams, viewHref } from "./cockpitNav";

const sp = (q: string) => new URLSearchParams(q);

it("parses every valid view", () => {
  for (const v of VIEWS) expect(viewFromParams(sp(`view=${v}`))).toBe(v);
  expect(VIEWS).toEqual(["outline", "versions", "references", "criteria", "members"]);
});

it("normalises an unrecognised view instead of erroring", () => {
  // Matches tabFromParams and artifactParam: a URL is user input, and a typo
  // shows the default rather than an error.
  expect(viewFromParams(sp("view=banana"))).toBe("outline");
  expect(viewFromParams(sp(""))).toBe("outline");
});

it("defaults to versions when the middle is showing the comparison", () => {
  // Otherwise a bare ?tab=history renders "pick two versions from the Versions
  // list in the side pane" beside the OUTLINE — the defect this design removes,
  // rebuilt by a default. Slice 2 adds "diff" to the same set.
  expect(viewFromParams(sp("tab=history"))).toBe("versions");
  expect(viewFromParams(sp("tab=draft"))).toBe("outline");
});

it("an explicit view always wins over the comparison default", () => {
  expect(viewFromParams(sp("tab=history&view=criteria"))).toBe("criteria");
});

it("viewHref changes ONLY view and keeps every other parameter", () => {
  // Seeded with every parameter the merge protects. Seeding with two of them
  // passes on an implementation that preserves those two and drops the rest,
  // and asserting only that view=versions is present passes on a plain
  // <Link to="?view=versions">, which REPLACES the search string.
  const start = sp(
    "tab=scorecard&sec=3&artifact=spec&a=aaaaaaa&b=bbbbbbb&layout=unified&view=outline",
  );
  const href = viewHref(start, "versions", "/cockpit/ws-1");
  expect(href.pathname).toBe("/cockpit/ws-1");
  const out = sp(href.search);
  expect(out.get("view")).toBe("versions");
  expect(out.get("tab")).toBe("scorecard");
  expect(out.get("sec")).toBe("3");
  expect(out.get("artifact")).toBe("spec");
  expect(out.get("a")).toBe("aaaaaaa");
  expect(out.get("b")).toBe("bbbbbbb");
  expect(out.get("layout")).toBe("unified");
});

it("viewHref does not mutate the params it was given", () => {
  const start = sp("tab=draft&view=outline");
  viewHref(start, "members", "/cockpit/ws-1");
  expect(start.get("view")).toBe("outline");
});
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd frontend && pnpm vitest run src/cockpit/cockpitNav.test.tsx`
Expected: FAIL — `viewFromParams`/`viewHref`/`VIEWS` are not exported. That is a
**collection error**, which proves nothing about behaviour; it is expected here only
because the module is new. Steps 5 and 6 carry the real mutation proofs.

- [ ] **Step 3: Add the `View` type**

In `frontend/src/cockpit/nav.ts`, add below the existing `TopTab` line:

```ts
export type View = "outline" | "versions" | "references" | "criteria" | "members";
```

- [ ] **Step 4: Implement in `frontend/src/cockpit/cockpitNav.ts`**

Add to the import from `./nav`: `View`. Then, below `tabFromParams`:

```ts
export const VIEWS: View[] = ["outline", "versions", "references", "criteria", "members"];

/** The `?tab` values whose middle IS the comparison. `?view` defaults to
 *  `versions` for these, so the comparison's own empty state points at a pane
 *  that is actually showing. Written as a set because Slice 2 renames the
 *  destination and the rule must not move with it. */
const COMPARISON_TABS: string[] = ["history"];

/** `?view` parsed to a valid View.
 *
 *  Anything unrecognised normalises to the default rather than erroring,
 *  matching tabFromParams and artifactParam: a URL is user input. An explicit
 *  view always wins over the comparison default. */
export function viewFromParams(sp: URLSearchParams): View {
  const v = sp.get("view");
  if (v != null && (VIEWS as string[]).includes(v)) return v as View;
  return COMPARISON_TABS.includes(sp.get("tab") ?? "") ? "versions" : "outline";
}

/** A rail destination that changes the pane and NOTHING else.
 *
 *  A query-only `<Link to="?view=versions">` resolves to a search string that
 *  REPLACES the current one, silently dropping ?tab, ?sec, ?artifact, ?a, ?b
 *  and ?layout — clicking a rail icon would discard the comparison the user was
 *  looking at. Copies first, so the caller's params are not mutated. */
export function viewHref(
  sp: URLSearchParams,
  view: View,
  pathname: string,
): { pathname: string; search: string } {
  const next = new URLSearchParams(sp);
  next.set("view", view);
  return { pathname, search: next.toString() };
}
```

- [ ] **Step 5: Prove the comparison default bites**

Mutate `COMPARISON_TABS` to `[]` and run
`pnpm vitest run src/cockpit/cockpitNav.test.tsx`.
Expected: **`defaults to versions when the middle is showing the comparison`** fails on
`expected 'outline' to be 'versions'` — a named assertion, not a collection error. Revert.

- [ ] **Step 6: Prove the merge bites**

Replace `viewHref`'s body with `return { pathname, search: \`view=${view}\` };` and run the
same file.
Expected: **`viewHref changes ONLY view and keeps every other parameter`** fails on
`expected null to be 'scorecard'`. Revert.

- [ ] **Step 7: Add `view`/`setView` to the hook**

In `useCockpitNav`, beside `const tab = tabFromParams(sp);`:

```ts
  const view = viewFromParams(sp);
```

and beside `setTab`:

```ts
  // No dirty-editor guard: changing the PANE unmounts no editor. Only writers
  // of ?tab carry that obligation.
  const setView = useCallback(
    (v: View) =>
      setSp((prev) => {
        const next = new URLSearchParams(prev);
        next.set("view", v);
        return next;
      }),
    [setSp],
  );
```

Add `view` and `setView` to the `CockpitNav` type and to the returned object.

- [ ] **Step 8: Full gate**

Run, from `frontend/`:
```
pnpm exec tsc --noEmit
pnpm vitest run
pnpm build
```
Expected: tsc silent; **`Test Files 116 passed (116)`** and **`Tests 754 passed (754)`**
(748 + 6 added here); build succeeds with only the pre-existing chunk-size warning. If the
total differs, name the term that changed — never adjust the expectation to fit.

- [ ] **Step 9: Commit**

```bash
git add frontend/src/cockpit/nav.ts frontend/src/cockpit/cockpitNav.ts \
        frontend/src/cockpit/cockpitNav.test.tsx
git commit -m "feat(cockpit): ?view picks the pane, and the rail merges rather than replaces"
```

---
### Task 2: the two-band rail

**Files:**
- Modify: `frontend/src/shell/nav-items.ts`
- Modify: `frontend/src/shell/ActivityBar.tsx:82-123`
- Modify: `frontend/src/shell/ActivityBar.module.css`
- Modify: `frontend/src/shell/ActivityBar.test.tsx` — **six existing tests reference the item
  this task deletes.** Step 1 says what happens to each. Do not discover them one red test
  at a time.

**Interfaces:**
- Consumes: `View`, `VIEWS`, `viewFromParams`, `viewHref` (Task 1);
  `cockpitIdFromPath(pathname): string | null` — already exported from `cockpitNav` and
  already imported by `ActivityBar.tsx:6`.
- Produces: `ACTIVITY_WORKSPACE: WorkspaceItem[]` where
  `WorkspaceItem = { id: View; label: string; icon: string }`.

**The test helper is `renderWithProviders(<ActivityBar />, { route })` from
`../test-utils`** — it supplies Mantine, a QueryClient and a MemoryRouter. Use it. Do not
write a new render helper.

**Two behaviours must survive the deletion of the Current-workspace item.**

1. **Clicking the already-active item toggles the pane** instead of navigating
   (`ActivityBar.tsx:32-37`, VS Code behaviour). Today the editor route's active item is
   Current workspace. After this task the active item is a workspace-band icon, so the band
   must accept `onToggleSidebar` and do the same `preventDefault()` + toggle.
2. **References is not a top-level destination.** The existing test asserts this on `/`,
   where there is no workspace band, so it still passes — but strengthen it, because
   References becoming a rail label makes the name ambiguous.

All ten codicon names below resolve against `@vscode/codicons@0.0.45`.

- [ ] **Step 1: Deal with the six existing tests, explicitly**

In `frontend/src/shell/ActivityBar.test.tsx`:

| Line | Test | Action |
| --- | --- | --- |
| 35 | `on the editor route the Current-workspace chip is active, Workspaces is not` | **Rewrite** — see below. |
| 41 | `hides the current-workspace item outside a cockpit` | **Delete.** The new `shows no workspace band when no workspace is open` covers the same route with the replacement assertion. |
| 48 | `shows the current-workspace item inside a cockpit and deactivates Calls` | **Rewrite** — see below. |
| 65 | `hides the current-workspace item on the cockpit list route` | **Delete.** The new band test uses `/cockpit` and asserts the band is absent. |
| 70 | `hides the current-workspace item on the create route` | **Rewrite** — keep the `/cockpit/new` route, assert no band. |
| 108 | `active nav item toggles the sidebar instead of navigating (preserves query state)` | **Rewrite** — click the active workspace icon. |

Replace those four with:

```tsx
it("on the editor route Workspaces is NOT active — the band says where you are", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit/abc" });
  expect(screen.getByRole("link", { name: "Workspaces" })).not.toHaveAttribute(
    "aria-current",
    "page",
  );
  expect(screen.getByRole("link", { name: "Versions" })).toBeInTheDocument();
});

it("inside a cockpit the band is present and Calls is not active", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit/abc" });
  expect(screen.getByRole("link", { name: "Outline" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Calls" })).not.toHaveAttribute(
    "aria-current",
    "page",
  );
});

it("shows no workspace band on the create route", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit/new" });
  expect(screen.queryByRole("link", { name: "Versions" })).toBeNull();
});

it("clicking the ACTIVE workspace icon toggles the pane and keeps the query", () => {
  // VS Code behaviour, previously carried by the Current-workspace item. Without
  // it, the only way to collapse the pane from the rail disappears with that
  // item — and a plain navigation would drop ?tab and ?sec.
  const onToggleSidebar = vi.fn();
  renderWithProviders(
    <>
      <ActivityBar onToggleSidebar={onToggleSidebar} />
      <LocationEcho />
    </>,
    { route: "/cockpit/abc?tab=spec&sec=2&view=versions" },
  );
  fireEvent.click(screen.getByRole("link", { name: "Versions" }));
  expect(onToggleSidebar).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId("loc").textContent).toBe(
    "/cockpit/abc?tab=spec&sec=2&view=versions",
  );
});
```

Strengthen the References test at line 61 in place:

```tsx
it("References is a per-workspace view, never a top-level destination", () => {
  renderWithProviders(<ActivityBar />, { route: "/" });
  expect(screen.queryByRole("link", { name: /references/i })).toBeNull();
});
```

- [ ] **Step 2: Write the new failing tests**

Append to the same file:

```tsx
it("shows no workspace band when no workspace is open", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit" });
  expect(screen.queryByRole("link", { name: "Versions" })).toBeNull();
  expect(screen.queryByRole("link", { name: "Outline" })).toBeNull();
  expect(screen.getByRole("link", { name: "Workspaces" })).toBeInTheDocument();
});

it("shows all five workspace icons when a workspace is open", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit/ws-1?tab=draft" });
  for (const label of ["Outline", "Versions", "References", "Criteria", "Members"]) {
    expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
  }
});

it("marks EXACTLY ONE workspace icon active, and it is the matching one", () => {
  // Asserting only that the expected icon is active passes on an implementation
  // that lights all five.
  for (const view of ["outline", "versions", "references", "criteria", "members"]) {
    const { unmount } = renderWithProviders(<ActivityBar />, {
      route: `/cockpit/ws-1?view=${view}`,
    });
    const active = screen
      .getAllByRole("link")
      .filter((el) => el.getAttribute("aria-current") === "page");
    expect(active).toHaveLength(1);
    expect(active[0].getAttribute("aria-label")?.toLowerCase()).toBe(view);
    unmount();
  }
});

it("a workspace icon keeps every other parameter in its href", () => {
  renderWithProviders(<ActivityBar />, {
    route: "/cockpit/ws-1?tab=scorecard&sec=3&artifact=spec&a=aaaaaaa&b=bbbbbbb&layout=unified",
  });
  const href = screen.getByRole("link", { name: "Versions" }).getAttribute("href") ?? "";
  expect(href.startsWith("/cockpit/ws-1")).toBe(true);
  const out = new URLSearchParams(href.split("?")[1] ?? "");
  expect(out.get("view")).toBe("versions");
  expect(out.get("tab")).toBe("scorecard");
  expect(out.get("sec")).toBe("3");
  expect(out.get("artifact")).toBe("spec");
  expect(out.get("a")).toBe("aaaaaaa");
  expect(out.get("b")).toBe("bbbbbbb");
  expect(out.get("layout")).toBe("unified");
});

it("separates the two bands with a divider", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit/ws-1" });
  expect(screen.getByTestId("activity-divider")).toBeInTheDocument();
});

it("no longer renders the Current workspace item", () => {
  renderWithProviders(<ActivityBar />, { route: "/cockpit/ws-1" });
  expect(screen.queryByRole("link", { name: "Current workspace" })).toBeNull();
});
```

- [ ] **Step 3: Run and watch them fail**

Run: `cd frontend && pnpm vitest run src/shell/ActivityBar.test.tsx`
Expected: the five workspace labels are not found; the toggle test cannot find `Versions`.

- [ ] **Step 4: Add the workspace band to `frontend/src/shell/nav-items.ts`**

```ts
import type { View } from "../cockpit/nav";

/** Views of the OPEN workspace. Rendered only while one is open, below the
 *  divider. Each is a `?view` value, not a path: the rail switches which pane
 *  is showing and must not decide anything about the middle. */
export interface WorkspaceItem {
  id: View;
  label: string;
  icon: string; // Codicon name without the "codicon-" prefix
}

export const ACTIVITY_WORKSPACE: WorkspaceItem[] = [
  { id: "outline", label: "Outline", icon: "list-tree" },
  { id: "versions", label: "Versions", icon: "history" },
  { id: "references", label: "References", icon: "references" },
  { id: "criteria", label: "Criteria", icon: "checklist" },
  { id: "members", label: "Members", icon: "organization" },
];
```

`history` rather than `source-control` or `git-commit`: the feature is called Versions
everywhere, and a git glyph would promise branching this app does not have.

- [ ] **Step 5: Render the band in `frontend/src/shell/ActivityBar.tsx`**

Add `useSearchParams` to the `react-router-dom` import, and `viewFromParams`, `viewHref` to
the `../cockpit/cockpitNav` import. Add `ACTIVITY_WORKSPACE` to the `./nav-items` import.

Add above `ActivityBar`:

```tsx
function WorkspaceBand({
  pathname,
  onToggleSidebar,
}: {
  pathname: string;
  onToggleSidebar?: () => void;
}) {
  const [sp] = useSearchParams();
  const view = viewFromParams(sp);
  return (
    <>
      <div className={styles.divider} data-testid="activity-divider" />
      <div className={styles.group}>
        {ACTIVITY_WORKSPACE.map((it) => {
          const active = it.id === view;
          return (
            <Tooltip key={it.id} label={it.label} position="right" withArrow>
              <Link
                to={viewHref(sp, it.id, pathname)}
                aria-label={it.label}
                aria-current={active ? "page" : undefined}
                className={active ? `${styles.item} ${styles.active}` : styles.item}
                onClick={(e) => {
                  // Same rule as the app band: clicking the ALREADY-ACTIVE item
                  // collapses the pane instead of navigating. preventDefault
                  // stops react-router's Link nav, so ?tab/?sec survive.
                  if (active && onToggleSidebar) {
                    e.preventDefault();
                    onToggleSidebar();
                  }
                }}
              >
                <Glyph icon={it.icon} />
              </Link>
            </Tooltip>
          );
        })}
      </div>
    </>
  );
}
```

Replace the whole `{wsId && (…)}` block at `ActivityBar.tsx:102-114` with:

```tsx
        {wsId && <WorkspaceBand pathname={pathname} onToggleSidebar={onToggleSidebar} />}
```

- [ ] **Step 6: Add the divider to `frontend/src/shell/ActivityBar.module.css`**

```css
.divider {
  width: 24px;
  height: 1px;
  margin: 8px 0;
  background: var(--app-border);
}
```

- [ ] **Step 7: Run the file**

Run: `cd frontend && pnpm vitest run src/shell/ActivityBar.test.tsx`
Expected: PASS.

- [ ] **Step 8: Prove the active-state gate bites**

Change `const active = it.id === view;` to `const active = true;` and re-run.
Expected: **`marks EXACTLY ONE workspace icon active, and it is the matching one`** fails
on `expected length 5 to be 1`. Revert.

- [ ] **Step 9: Prove the merge gate bites**

Change the `to` to `` {`?view=${it.id}`} `` and re-run.
Expected: **`a workspace icon keeps every other parameter in its href`** fails on
`expected null to be 'scorecard'`. Revert.

- [ ] **Step 10: Prove the toggle gate bites**

Delete the `onClick` handler and re-run.
Expected: **`clicking the ACTIVE workspace icon toggles the pane and keeps the query`**
fails on `expected "spy" to be called 1 times, but got 0 times`. Revert.

- [ ] **Step 11: Full gate**

```
pnpm exec tsc --noEmit
pnpm vitest run
pnpm build
```
Expected: **`Test Files 116 passed (116)`** and **`Tests 758 passed (758)`**.

Arithmetic, term by term:

| Term | Count |
| --- | --- |
| After Task 1 | 754 |
| Step 1 rewrites (4 tests replaced in place) | +0 |
| Step 1 deletions (lines 41 and 65) | −2 |
| Step 2 additions | +6 |
| **Total** | **758** |

If the run prints anything else, **name the term that changed**. Never adjust the
expectation to make the arithmetic close.

- [ ] **Step 12: Commit**

```bash
git add frontend/src/shell/nav-items.ts frontend/src/shell/ActivityBar.tsx \
        frontend/src/shell/ActivityBar.module.css frontend/src/shell/ActivityBar.test.tsx
git commit -m "feat(shell): the rail splits into an app band and a workspace band"
```

---
### Task 3: one pane at a time

**Files:**
- Create: `frontend/src/cockpit/panes/PaneHeader.tsx`
- Create: `frontend/src/cockpit/panes/OutlinePane.tsx`
- Create: `frontend/src/cockpit/panes/ReferencesPane.tsx`
- Create: `frontend/src/cockpit/panes/CriteriaPane.tsx`
- Create: `frontend/src/cockpit/panes/MembersPane.tsx`
- Modify: `frontend/src/cockpit/CockpitSidebar.tsx` — becomes a switch
- Modify: `frontend/src/cockpit/VersionGraph.tsx:127-133` — `PaneHeader` in place of `SidebarGroup`
- Delete: `frontend/src/cockpit/SidebarGroup.tsx`
- Test: `frontend/src/cockpit/CockpitSidebar.test.tsx`, `frontend/src/cockpit/VersionGraph.test.tsx`

**Interfaces:**
- Consumes: `viewFromParams` (Task 1).
- Produces:
  - `PaneHeader({ title, count }: { title: string; count?: number })`
  - `OutlinePane({ id }: { id: string })`, and the same signature for
    `ReferencesPane`, `CriteriaPane`, `MembersPane`, `VersionGraph`.

**This is a move, not a rewrite.** Each pane's body is the existing JSX from
`CockpitSidebar.tsx` with its `SidebarGroup` wrapper swapped for `PaneHeader`, and the
hooks it needs moved with it. Copy the bodies verbatim, including their comments. Behaviour
changes are out of scope for this task — the only intended change is which one renders.

**The header pattern already exists.** `CallsFilters` and `WorkspacesFilters` head their
panes with `<Text component="h2" tt="uppercase" fz="0.7rem" fw={600} c="dimmed">`.
`PaneHeader` is that element plus an optional count, so every pane in the app is headed
identically.

**`styles` moves with the JSX.** All five bodies use `CockpitSidebar.module.css` classes
(`note`, `outline`, `row`, `rowLabel`, `dirty`, `metaRow`, `metaSub`, `memberRemove`,
`memberAdd`, `criterion`, `criterionName`, `editRubric`). Each pane imports that same
stylesheet: `import styles from "../CockpitSidebar.module.css";`. Do not split the CSS.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/cockpit/CockpitSidebar.test.tsx`:

```tsx
it("renders ONLY the view the URL asks for", async () => {
  // Asserting the chosen view is present is not enough: that passes on the
  // accordion this task removes. The others must be ABSENT.
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draft as never);
  renderSidebar("/cockpit/ws-1?view=outline");
  expect(await screen.findByRole("button", { name: /Methods/ })).toBeInTheDocument();
  expect(screen.queryByRole("list", { name: "References" })).toBeNull();
  expect(screen.queryByRole("list", { name: "Members" })).toBeNull();
  expect(screen.queryByRole("list", { name: "Criteria" })).toBeNull();
  expect(screen.queryByRole("list", { name: "Versions" })).toBeNull();
});

it("switches the whole pane when ?view changes", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draft as never);
  renderSidebar("/cockpit/ws-1?view=members");
  expect(await screen.findByText(/member/i)).toBeInTheDocument();
  expect(screen.queryByRole("list", { name: "Outline" })).toBeNull();
});

it("heads every pane with its own name", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draft as never);
  renderSidebar("/cockpit/ws-1?view=references");
  expect(await screen.findByRole("heading", { name: /references/i })).toBeInTheDocument();
});

it("has no collapsible group chrome left", async () => {
  // SidebarGroup rendered a header BUTTON with aria-expanded. One view at a
  // time needs no toggle, and leaving one invites the folded-below-the-fold
  // problem back.
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(draft as never);
  const { container } = renderSidebar("/cockpit/ws-1?view=outline");
  await screen.findByRole("button", { name: /Methods/ });
  expect(container.querySelectorAll("[aria-expanded]")).toHaveLength(0);
});
```

- [ ] **Step 2: Run and watch them fail**

Run: `cd frontend && pnpm vitest run src/cockpit/CockpitSidebar.test.tsx`
Expected: `renders ONLY the view the URL asks for` fails because References/Members/Criteria
are all present; `has no collapsible group chrome left` fails on a non-zero length.

- [ ] **Step 3: Create `frontend/src/cockpit/panes/PaneHeader.tsx`**

```tsx
import type { ReactNode } from "react";

import { Group, Text } from "@mantine/core";

/** The one pane header. Same element CallsFilters and WorkspacesFilters already
 *  use, plus an optional count, so every pane in the app is headed identically.
 *
 *  Deliberately NOT collapsible: one view renders at a time, so a toggle would
 *  only let the user hide the thing they just asked for — and a folded group
 *  four down a scrolling pane is the defect this design removes. */
export function PaneHeader({
  title,
  count,
  children,
}: {
  title: string;
  count?: number;
  children?: ReactNode;
}) {
  return (
    <Group justify="space-between" px="sm" pt="sm" pb={4} wrap="nowrap">
      <Text component="h2" tt="uppercase" fz="0.7rem" fw={600} c="dimmed">
        {title}
        {count != null && ` ${count}`}
      </Text>
      {children}
    </Group>
  );
}
```

`children` is the slot Task 4 uses for the Versions pane's Draft/Spec switch. No other pane
passes one.

- [ ] **Step 4: Create the four pane files**

Each file takes the matching block out of `CockpitSidebar.tsx` **verbatim**, with its
hooks, and swaps `<SidebarGroup title="X" …>` for `<PaneHeader title="X" count={…} />`
followed by the body. Concretely:

- `OutlinePane.tsx` — the block at `CockpitSidebar.tsx:71-100`, plus `useDraft`, `useSpec`,
  `useEditorDirty`, `useCockpitNav` and `requestSectionJump`. Keep the comment explaining
  why the jump is an event and the section is URL state.
- `ReferencesPane.tsx` — `:102-112`, plus `useReferences` and its `Array.isArray` guard.
- `CriteriaPane.tsx` — `:114-137` **and** the Edit-rubric `Modal` at `:189-206`, plus
  `useScorecard`, `useRubric`, `useUpdateRubric`, `useCockpitMeta`, and the `editOpen`,
  `draftCriteria`, `draftNotes` state with its `useEffect`. The modal moves with the button
  that opens it — they are one feature.
- `MembersPane.tsx` — `:139-186`, plus `useMembers`, `useCockpitMeta`, `useAddMember`,
  `useRemoveMember` and the `newEmail` state.

Each file starts with `import styles from "../CockpitSidebar.module.css";` and
`import { PaneHeader } from "./PaneHeader";`.

- [ ] **Step 5: Reduce `CockpitSidebar.tsx` to a switch**

```tsx
import { useParams, useSearchParams } from "react-router-dom";

import { viewFromParams } from "./cockpitNav";
import styles from "./CockpitSidebar.module.css";
import { CriteriaPane } from "./panes/CriteriaPane";
import { MembersPane } from "./panes/MembersPane";
import { OutlinePane } from "./panes/OutlinePane";
import { ReferencesPane } from "./panes/ReferencesPane";
import { VersionGraph } from "./VersionGraph";

// The cockpit's left sidebar (the route's handle.Sidebar). Renders exactly ONE
// view, chosen by ?view — the rail icon and the pane are the same decision.
// Each pane owns its own queries; React Query dedupes, so a pane that shares a
// query with the middle stays in sync with no props across the AppShell
// boundary.
export function CockpitSidebar() {
  const { id = "" } = useParams();
  const [sp] = useSearchParams();
  const view = viewFromParams(sp);
  return (
    <div className={styles.sidebar}>
      {view === "outline" && <OutlinePane id={id} />}
      {view === "versions" && <VersionGraph id={id} />}
      {view === "references" && <ReferencesPane id={id} />}
      {view === "criteria" && <CriteriaPane id={id} />}
      {view === "members" && <MembersPane id={id} />}
    </div>
  );
}
```

Every other import in the old file moves out with its block —
the file should end with none of `useState`, `useEffect`, `Modal`, `Button`, `BandBadge`,
`RubricEditor`, or any `../api/*` import left behind. An unused import is a `tsc` error
under this project's config; let that be the check.

- [ ] **Step 6: Swap `VersionGraph`'s wrapper**

In `frontend/src/cockpit/VersionGraph.tsx`, delete the `useCockpitNav` import and the
`const { tab } = useCockpitNav();` line, and replace the `SidebarGroup` opening tag and its
preceding comment with:

```tsx
  return (
    <>
      <PaneHeader title="Versions" count={rows.length || undefined} />
```

and the closing `</SidebarGroup>` with `</>`. Import `PaneHeader` from `./panes/PaneHeader`.
`openWhen` disappears with the wrapper — it existed only to unfold an accordion.

- [ ] **Step 7: Fix `VersionGraph.test.tsx`'s helper**

Its `wrap()` clicks the group header to expand it (`VersionGraph.test.tsx:32-46`). There is
no header button any more. Delete these two lines from `wrap()`:

```tsx
  const header = screen.getByRole("button", { name: /versions/i });
  if (header.getAttribute("aria-expanded") !== "true") fireEvent.click(header);
```

Nothing replaces them: the rows render immediately. Remove the now-unused `fireEvent`
import if `tsc` flags it.

- [ ] **Step 8: Delete `SidebarGroup`**

```bash
git rm frontend/src/cockpit/SidebarGroup.tsx
```

`tsc` is the check that nothing still imports it.

- [ ] **Step 9: Run everything**

```
pnpm exec tsc --noEmit
pnpm vitest run
```
Expected: tsc silent. Any red test here is a **move that changed behaviour** — find what
differs from the original block rather than editing the test.

- [ ] **Step 10: Prove the switch bites**

Change `{view === "references" && <ReferencesPane id={id} />}` to
`{<ReferencesPane id={id} />}` and re-run `src/cockpit/CockpitSidebar.test.tsx`.
Expected: **`renders ONLY the view the URL asks for`** fails on
`expected [...] to be null` for the References list. Revert.

- [ ] **Step 11: Full gate**

```
pnpm exec tsc --noEmit
pnpm vitest run
pnpm build
```
Expected: **`Test Files 116 passed (116)`** — the four new pane files carry no test files
of their own, so the file count does not move. Their behaviour is covered where it already
was, through `CockpitSidebar.test.tsx`.

Expected: **`Tests 762 passed (762)`** — 758 plus the four added in Step 1.

This task is a **move**, so no existing test should need changing except `wrap()` in
`VersionGraph.test.tsx` (Step 7), which is a helper, not an assertion. If any other test
goes red, the move changed behaviour: find the difference from the original block rather
than editing the test. If a test genuinely must go, name it and why — do not adjust the
total to fit.

- [ ] **Step 12: Commit**

```bash
git add -A frontend/src/cockpit/
git commit -m "refactor(cockpit): the side pane renders one view, each in its own file"
```

---
### Task 4: the Draft/Spec switch moves to the pane it governs

**Files:**
- Modify: `frontend/src/cockpit/VersionGraph.tsx`
- Modify: `frontend/src/cockpit/HistoryPanel.tsx:95-107`
- Test: `frontend/src/cockpit/VersionGraph.test.tsx`, `frontend/src/cockpit/HistoryPanel.test.tsx`

**Interfaces:**
- Consumes: `PaneHeader`'s `children` slot (Task 3);
  `useHistoryNav().artifact` and `.setArtifact` — already exported, unchanged.
- Produces: nothing new.

`?artifact` decides **whose history the Versions pane lists**. Its control today sits in
`HistoryPanel`'s header — the middle. After Slice 1 the pane is the list, so the switch is
on the one surface it does not govern: a user looking at the Versions pane and wanting Spec
history would have to go to the middle to change what the pane shows.

`setArtifact` already clears both shas (a sha from the draft's history is not a version of
the spec). That is unchanged, and Step 1 pins it.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/cockpit/VersionGraph.test.tsx`:

```tsx
it("offers the Draft/Spec switch in the pane that lists them", () => {
  mockList({ versions: [ROW("aaaaaaa", "one")] });
  wrap();
  expect(screen.getByLabelText(/which artifact/i)).toBeInTheDocument();
});

it("switching artifact clears BOTH shas", async () => {
  // A sha from the draft's history is not a version of the spec; leaving one
  // selected would fetch a 404. Pinning the existing setArtifact behaviour at
  // its new call site.
  const user = userEvent.setup();
  mockList({ versions: [ROW("aaaaaaa", "one")] });
  wrap("/cockpit/ws1?tab=history&view=versions&a=aaaaaaa&b=bbbbbbb");
  // Mantine's SegmentedControl hides its radio at opacity 0 — Playwright
  // refuses to click it and userEvent must target the LABEL.
  await user.click(screen.getByText("Spec"));
  const url = new URLSearchParams(screen.getByTestId("url").textContent ?? "");
  expect(url.get("artifact")).toBe("spec");
  expect(url.get("a")).toBeNull();
  expect(url.get("b")).toBeNull();
});
```

Append to `frontend/src/cockpit/HistoryPanel.test.tsx`:

```tsx
it("no longer carries the artifact switch — the pane that lists them does", () => {
  mockList({ versions: [ROW("aaaaaaa", "one")] });
  mockVersion(null);
  wrap(<HistoryPanel id="ws1" />);
  expect(screen.queryByLabelText(/which artifact/i)).toBeNull();
});
```

- [ ] **Step 2: Run and watch them fail**

Run: `cd frontend && pnpm vitest run src/cockpit/VersionGraph.test.tsx src/cockpit/HistoryPanel.test.tsx`
Expected: the two `VersionGraph` tests fail (no such control there); the `HistoryPanel` test
fails because the control is still present.

Note: `names BOTH selectors, not just the layout one` in `HistoryPanel.test.tsx` asserts
that control exists there. It is superseded by this task — **delete it**, and say so in the
report. Its guarantee moves to the first `VersionGraph` test above.

- [ ] **Step 3: Add the switch to the Versions pane**

In `frontend/src/cockpit/VersionGraph.tsx`, add to the Mantine import: `SegmentedControl`.
Add `import type { Artifact } from "../api/proposals";`. Then:

```tsx
      <PaneHeader title="Versions" count={rows.length || undefined}>
        {/* Lives here, not in the comparison: this decides WHOSE history the
            list below shows, so it belongs on the list. */}
        <SegmentedControl
          size="xs"
          aria-label="Which artifact"
          value={nav.artifact}
          onChange={(v) => nav.setArtifact(v as Artifact)}
          data={[
            { label: "Draft", value: "draft" },
            { label: "Spec", value: "spec" },
          ]}
        />
      </PaneHeader>
```

- [ ] **Step 4: Remove it from `HistoryPanel`**

Delete the `<SegmentedControl … aria-label="Which artifact" …>` block at
`HistoryPanel.tsx:97-106`. The surrounding `<Group justify="space-between">` now holds only
the `<Title>`; keep the `Group` — the layout-control `Group` below it is a different one.
Remove the `Artifact` type import if `tsc` reports it unused.

- [ ] **Step 5: Run the two files**

Run: `cd frontend && pnpm vitest run src/cockpit/VersionGraph.test.tsx src/cockpit/HistoryPanel.test.tsx`
Expected: PASS.

- [ ] **Step 6: Prove the clearing bites**

In `historyNav.ts`, change `setArtifact` to `write({ artifact: x })` (dropping
`a: null, b: null`) and re-run `src/cockpit/VersionGraph.test.tsx`.
Expected: **`switching artifact clears BOTH shas`** fails on
`expected 'aaaaaaa' to be null`. Revert.

- [ ] **Step 7: Full gate**

```
pnpm exec tsc --noEmit
pnpm vitest run
pnpm build
```
Expected: **`Test Files 116 passed (116)`**, **`Tests 764 passed (764)`** — 762, plus three
added, minus the one deleted in Step 2.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/cockpit/VersionGraph.tsx frontend/src/cockpit/VersionGraph.test.tsx \
        frontend/src/cockpit/HistoryPanel.tsx frontend/src/cockpit/HistoryPanel.test.tsx
git commit -m "feat(cockpit): the Draft/Spec switch sits on the list it governs"
```

**Slice 1 is complete here.** Versions is one rail click away, the pane shows one thing,
and nothing is folded. `?tab=history` still works and still renders the comparison.

---

## Slice 2 — the History tab becomes an opened diff

### Task 5: `diff` replaces `history`, parser and consumers together

**Files:**
- Modify: `frontend/src/cockpit/nav.ts:1`
- Modify: `frontend/src/cockpit/cockpitNav.ts`
- Modify: `frontend/src/routes/Cockpit.tsx:82-94`
- Modify: `frontend/src/cockpit/historyNav.ts`
- Test: `frontend/src/cockpit/cockpitNav.test.tsx`, `frontend/src/cockpit/historyNav.test.tsx`,
  `frontend/src/routes/Cockpit.test.tsx`

**Why this is ONE task.** Removing `"history"` from the `TopTab` union breaks every
consumer that writes the literal — `Cockpit.tsx:86,94` and `historyNav.ts:162`. Splitting
the parser from its consumers would leave a commit that does not compile, so they land
together.

**Interfaces:**
- Consumes: `TOP_TABS`, `tabFromParams`, `viewFromParams`, `COMPARISON_TABS` (Task 1).
- Produces: `TopTab` gains `"diff"` and loses `"history"`; `tabFromParams` maps a literal
  `history` to `diff`.

**Precedence matters and is easy to get backwards.** The alias supplies `view=versions`
**only when `view` is absent**; an explicit `view` always wins. Without that, a legacy
`?tab=history` URL plus a rail click — which merges, producing `?tab=history&view=criteria`
— would have the alias force the pane back to Versions and the click would do nothing.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/cockpit/cockpitNav.test.tsx`:

```tsx
it("parses the comparison as diff, and TOP_TABS no longer offers history", () => {
  expect(tabFromParams(sp("tab=diff"))).toBe("diff");
  expect(TOP_TABS).toEqual(["draft", "spec", "scorecard", "review", "diff"]);
});

it("a legacy ?tab=history link still opens the comparison", () => {
  // Parse-time alias, not a redirect: the URL the user arrived on is not
  // rewritten, so the back button and the broken link both survive.
  expect(tabFromParams(sp("tab=history"))).toBe("diff");
});

it("the alias supplies the pane only when ?view is absent", () => {
  expect(viewFromParams(sp("tab=history"))).toBe("versions");
  expect(viewFromParams(sp("tab=diff"))).toBe("versions");
  // An explicit view WINS. Otherwise a rail click from a legacy URL — which
  // merges to ?tab=history&view=criteria — would silently do nothing.
  expect(viewFromParams(sp("tab=history&view=criteria"))).toBe("criteria");
  expect(viewFromParams(sp("tab=diff&view=criteria"))).toBe("criteria");
});

it("?sec rejects a value that is not a safe integer", () => {
  // The predicate was /^\d+$/ + Number, so twenty-five nines returned 1e+25 —
  // measured — which was then compared against real section indices and
  // written back to the URL.
  expect(secFromParams(sp("sec=3"))).toBe(3);
  expect(secFromParams(sp(`sec=${"9".repeat(25)}`))).toBe(0);
  expect(secFromParams(sp("sec=-1"))).toBe(0);
  expect(secFromParams(sp("sec=x"))).toBe(0);
});
```

Add `TOP_TABS`, `tabFromParams` and `secFromParams` to the file's import from `./cockpitNav`
if they are not already there.

- [ ] **Step 2: Run and watch them fail**

Run: `cd frontend && pnpm vitest run src/cockpit/cockpitNav.test.tsx`
Expected: `tab=diff` parses to `draft`; the sec test returns `1e+25`.

- [ ] **Step 3: Change the union in `frontend/src/cockpit/nav.ts`**

```ts
export type TopTab = "draft" | "spec" | "scorecard" | "review" | "diff";
```

- [ ] **Step 4: Update the parser in `frontend/src/cockpit/cockpitNav.ts`**

```ts
export const TOP_TABS: TopTab[] = ["draft", "spec", "scorecard", "review", "diff"];

/** The comparison destination, by every name it has had. `history` is the name
 *  it shipped under; links already exist. */
const COMPARISON_TABS: string[] = ["diff", "history"];

/** `?tab` parsed to a valid TopTab, defaulting to "draft".
 *
 *  `history` parses to `diff` — a parse-time ALIAS, not a redirect. The URL the
 *  user arrived on is not rewritten: doing so costs them the back button and
 *  hides which link was broken. */
export function tabFromParams(sp: URLSearchParams): TopTab {
  const t = sp.get("tab");
  if (t === "history") return "diff";
  return (TOP_TABS as string[]).includes(t ?? "") ? (t as TopTab) : "draft";
}

/** `?sec` parsed to a safe non-negative int, defaulting to 0.
 *
 *  Number.isSafeInteger, not just /^\d+$/: a long digit string parsed to a
 *  float — measured, twenty-five nines gave 1e+25 — which was then compared
 *  against real section indices and written back to the URL. */
export function secFromParams(sp: URLSearchParams): number {
  const s = sp.get("sec");
  if (s == null || !/^\d+$/.test(s)) return 0;
  const n = Number(s);
  return Number.isSafeInteger(n) ? n : 0;
}
```

`viewFromParams` is unchanged: it already consults `COMPARISON_TABS`, which now holds both
names, and already returns an explicit `view` before consulting it.

- [ ] **Step 5: Run the file**

Run: `cd frontend && pnpm vitest run src/cockpit/cockpitNav.test.tsx`
Expected: PASS.

- [ ] **Step 6: Prove the precedence bites**

Change `viewFromParams` so the comparison default is checked **before** the explicit value:

```ts
  if (COMPARISON_TABS.includes(sp.get("tab") ?? "")) return "versions";
```
as the first line of the function, and re-run.
Expected: **`the alias supplies the pane only when ?view is absent`** fails on
`expected 'versions' to be 'criteria'`. Revert.

- [ ] **Step 7: Prove the sec bound bites**

Delete the `Number.isSafeInteger` check and re-run.
Expected: **`?sec rejects a value that is not a safe integer`** fails on
`expected 1e+25 to be 0`. Revert.


- [ ] **Step 8: Write the failing consumer tests**

Append to `frontend/src/cockpit/historyNav.test.tsx`:

```tsx
describe("selectAndOpen after the rename", () => {
  function Probe() {
    const hist = useHistoryNav();
    const [sp] = useSearchParams();
    return (
      <>
        <button onClick={() => hist.selectAndOpen("aaaaaaa")}>pick</button>
        <output data-testid="url">{sp.toString()}</output>
      </>
    );
  }

  function clickPickAt(url: string) {
    render(
      <MemoryRouter initialEntries={[url]}>
        <Probe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("pick"));
    return new URLSearchParams(screen.getByTestId("url").textContent ?? "");
  }

  afterEach(() => {
    act(() => resetEditorStore());
    vi.restoreAllMocks();
  });

  it("opens the comparison as tab=diff", () => {
    expect(clickPickAt("/cockpit/ws1?tab=draft").get("tab")).toBe("diff");
  });

  it("does NOT prompt from a legacy ?tab=history URL", () => {
    // The guard asks "am I already on the comparison?". Comparing the RAW ?tab
    // to "diff" would say no for a legacy link and prompt to discard edits that
    // are not being discarded. It must compare the PARSED destination.
    act(() => setDirty([1]));
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    clickPickAt("/cockpit/ws1?tab=history&b=bbbbbbb");
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("deselecting the last version neither opens the comparison nor prompts", () => {
    // The resulting selection must be computed BEFORE the guard runs. Guarding
    // first prompts "discard your unsaved edits?" and then discovers no
    // destination change was going to happen — a prompt for nothing, which is
    // how users learn to dismiss prompts without reading them.
    act(() => setDirty([1]));
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const url = clickPickAt("/cockpit/ws1?tab=draft&a=aaaaaaa");
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(url.get("tab")).toBe("draft");
    expect(url.get("a")).toBeNull();
  });
});
```

Append to `frontend/src/routes/Cockpit.test.tsx`:

```tsx
it("the tab strip no longer offers History", () => {
  renderCockpit("/cockpit/ws-1?tab=draft");
  expect(screen.queryByRole("tab", { name: "History" })).toBeNull();
  for (const name of ["Draft", "Spec", "Scorecard", "Panel review"]) {
    expect(screen.getByRole("tab", { name })).toBeInTheDocument();
  }
});

it("?tab=diff renders the comparison even though no tab offers it", () => {
  renderCockpit("/cockpit/ws-1?tab=diff");
  expect(screen.getByText(/version history/i)).toBeInTheDocument();
});

it("a legacy ?tab=history renders the comparison too", () => {
  renderCockpit("/cockpit/ws-1?tab=history");
  expect(screen.getByText(/version history/i)).toBeInTheDocument();
});
```

`renderCockpit` may not exist under that name — **read the file's existing helper and use
it**, rather than adding a second one. `Cockpit.test.tsx` has 28 tests and already renders
the route.

- [ ] **Step 9: Run and watch them fail**

Run: `cd frontend && pnpm vitest run src/cockpit/historyNav.test.tsx src/routes/Cockpit.test.tsx`
Expected: `tab` is `history` not `diff`; the History tab is still present.

- [ ] **Step 10: Update `frontend/src/routes/Cockpit.tsx`**

Delete the History tab from the list at `:86`:

```tsx
            <Tabs.Tab value="review">Panel review</Tabs.Tab>
```
(that line is the last one; the `history` line goes.)

Change the render line at `:94`:

```tsx
          {tab === "diff" && <HistoryPanel id={id} />}
```

Mantine's `Tabs` receives `value={tab}`. On `?tab=diff` no tab matches, which is the
intent — the comparison is opened, not tabbed to — and Mantine renders the list with
nothing selected. Do not add a hidden tab to "fix" this.

- [ ] **Step 11: Update `selectAndOpen` in `frontend/src/cockpit/historyNav.ts`**

Replace the body with:

```tsx
  const selectAndOpen = useCallback(
    (sha: string) => {
      const next = nextSelection({ a: shaParam(sp, "a"), b: shaParam(sp, "b") }, sha);
      const opens = next.a != null || next.b != null;
      // Computed BEFORE the guard: a click that empties the selection changes no
      // destination, so there is nothing to discard and prompting would be a
      // prompt for nothing.
      //
      // `tabFromParams`, not the raw string: a legacy ?tab=history IS the
      // comparison, and comparing raw would prompt on every legacy link.
      //
      // The guard runs OUTSIDE setSp — window.confirm must never run inside an
      // updater, which is expected to be pure and runs twice under StrictMode.
      const alreadyThere = tabFromParams(sp) === "diff";
      if (opens && !alreadyThere && !confirmDiscardIfDirty()) return;
      setSp((prev) => {
        const out = new URLSearchParams(prev);
        // Read from `prev`, not the render closure: this is the one write that
        // carries both changes, so it must see the params it is about to
        // replace. See the PINNED two-writer test.
        const fresh = nextSelection(
          { a: shaParam(prev, "a"), b: shaParam(prev, "b") },
          sha,
        );
        for (const key of ["a", "b"] as const) {
          const v = fresh[key];
          if (v == null) out.delete(key);
          else out.set(key, v);
        }
        if (fresh.a != null || fresh.b != null) out.set("tab", "diff");
        return out;
      });
    },
    [sp, setSp],
  );
```

Import `tabFromParams` from `./cockpitNav`. The `opens` computed from the render closure
decides the *guard*; the `fresh` computed from `prev` decides the *write*. They agree in
every real case, and only `fresh` may touch the URL.

- [ ] **Step 12: Run the two files, then the whole suite**

```
pnpm exec tsc --noEmit
pnpm vitest run
```
`tsc` must be silent — if it still reports a `"history"` literal, that consumer was missed.

- [ ] **Step 13: Prove the parsed-destination guard bites**

Change `tabFromParams(sp) === "diff"` to `sp.get("tab") === "diff"` and re-run
`src/cockpit/historyNav.test.tsx`.
Expected: **`does NOT prompt from a legacy ?tab=history URL`** fails on
`expected "spy" not to be called`. Revert.

- [ ] **Step 14: Prove the deselect ordering bites**

Move the guard above the `opens` computation — that is, guard on
`!alreadyThere && !confirmDiscardIfDirty()` — and re-run.
Expected: **`deselecting the last version neither opens the comparison nor prompts`** fails
on `expected "spy" not to be called`. Revert.

- [ ] **Step 15: Full gate**

```
pnpm exec tsc --noEmit
pnpm vitest run
pnpm build
```
Expected: **`Test Files 116 passed (116)`**, **`Tests 774 passed (774)`** — 764, plus four
in Step 1 and six in Step 8. If the total differs, name the term that changed.

The eight pre-existing dirty-guard tests (four in `historyNav.test.tsx`, four in
`cockpitNav.test.tsx`) must be among the passes. If any went red, the guard changed shape
rather than name — that is a finding, not a test to edit.

- [ ] **Step 16: Commit**

```bash
git add frontend/src/cockpit/nav.ts frontend/src/cockpit/cockpitNav.ts \
        frontend/src/cockpit/cockpitNav.test.tsx frontend/src/cockpit/historyNav.ts \
        frontend/src/cockpit/historyNav.test.tsx frontend/src/routes/Cockpit.tsx \
        frontend/src/routes/Cockpit.test.tsx
git commit -m "feat(cockpit): the comparison is opened from the pane, not tabbed to"
```

---

## Done

Five tasks. After Task 4, Versions is one rail click away and nothing is folded. After
Task 5, the History tab is gone and the comparison opens like a document.

**Expected final gate:** `Test Files 116 passed (116)`, `Tests 774 passed (774)`, tsc
clean, build clean, `git diff --stat <base>..HEAD -- src/` empty.

**Not in this plan, and named so they are not mistaken for oversights:**

- **The bottom panel** — Inbox, Problems, Activity, Library, Citations, Claims. Some are
  lists with the same disease. Six more tabs is a separate design conversation.
- **Criteria rows all open the same Scorecard** (`CriteriaPane`, moved verbatim in Task 3).
  The row promises specificity and delivers none. Fixing it needs an anchor per criterion
  on the Scorecard and a `criterion_index` on `NavTarget`; neither exists. Spec §3.1.
- **A single guarded path for `?tab`.** Three writers, three guards, nothing enforcing a
  fourth. Both guards were added after a bug was found. Spec §7.3.
