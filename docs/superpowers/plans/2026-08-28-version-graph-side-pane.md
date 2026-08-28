# Version Graph in the Side Pane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the version list out of the History tab into the cockpit's left side pane, drawn as a commit graph; clicking two dots opens the comparison in the tab.

**Architecture:** A new `VersionGraph` renders its own `SidebarGroup` from the git-sourced `useVersions(id, artifact)`, replacing the side pane's DB-sourced `Versions` group. Clicking a dot calls a new `selectAndOpen` on `useHistoryNav` — **one** `setSearchParams` write that both moves the selection and sets `tab=history`, because two writers in one handler provably lose the selection. `HistoryPanel` loses its list and gains an empty state.

**Tech Stack:** React 19, TypeScript, Vite, Mantine, react-router 7.18.2, vitest + @testing-library/react, CSS modules. Playwright (Task 5 gate only).

**Spec:** `docs/superpowers/specs/2026-08-28-version-graph-side-pane-design.md` rev 2 (`e142e198`).

## Global Constraints

- **Work from `frontend/`.** `cd frontend` persists inside one Bash call but NOT across calls — re-anchor every time.
- Gates: `pnpm exec tsc --noEmit`, `pnpm vitest run`, `pnpm build`. All from `frontend/`.
- **Baseline, measured on master at `e142e198`: `Test Files 115 passed (115)`, `Tests 689 passed (689)`.**
- **Judge vitest on the printed file and test counts, never on the absence of red.** `include` is `src/**/*.{test,spec}.{ts,tsx}` — a misnamed or misplaced test file is silently uncollected and reports success.
- **A mutation that breaks a file's import proves nothing** — it yields a whole-file collection error. A mutation proof needs a **named test failing on an assertion**.
- `pnpm add` is refused by a 7-day cooldown. **This plan adds no dependency.**
- **Untouched by every task:** `diff.ts`, `SideBySideDiffView.tsx`, `DiffView.tsx` and all their tests. This plan changes where the list lives, not how a diff renders.
- `toggle` in `historyNav.ts` keeps its exact current behaviour and its existing tests pass **unmodified**. `selectAndOpen` is added beside it.
- Spec §2.1 is binding: this change buys **vertical** space. Today's diff container is already **900px** with **449px** columns. Never justify a decision here on width, and never write a gate that demands the diff got wider.

---

## File Structure

**Create**

| File | Responsibility |
|---|---|
| `frontend/src/cockpit/VersionGraph.tsx` | The whole `Versions` sidebar group: data, states, graph rows, click |
| `frontend/src/cockpit/VersionGraph.module.css` | Lane, dot, connector line, truncation |
| `frontend/src/cockpit/VersionGraph.test.tsx` | Rows, states, click behaviour |

**Modify**

| File | Change |
|---|---|
| `frontend/src/cockpit/historyNav.ts` | `nextSelection` helper + `selectAndOpen` |
| `frontend/src/cockpit/historyNav.test.ts` → **`.tsx`** | Tests for both, incl. the pinned two-writer demonstration. Renamed because it now renders JSX. |
| `frontend/src/cockpit/CockpitSidebar.tsx` | `Versions` group → `<VersionGraph id={id} />`; drop the `api/workspace` `useVersions` import |
| `frontend/src/cockpit/CockpitSidebar.test.tsx` | Four `/versions` stubs → `/history`, with the row's FIELD NAMES changed too (`message`→`subject`, `created_at`→`committed_at`); one graph-specific test added |
| `frontend/src/api/workspace.ts` | Delete the now-callerless `useVersions` |
| `frontend/src/cockpit/HistoryPanel.tsx` | Remove the list + `VersionRowButton`; add the empty state; reword three messages |
| `frontend/src/cockpit/HistoryPanel.test.tsx` | Six list tests out, six empty-state and copy tests in |

---

### Task 1: `selectAndOpen` — one write, not two

**Files:**
- Modify: `frontend/src/cockpit/historyNav.ts`
- Modify: `frontend/src/cockpit/historyNav.test.ts`

**Interfaces:**
- Produces: `export function nextSelection(cur: { a: string | null; b: string | null }, sha: string): { a: string | null; b: string | null }`, and `selectAndOpen: (sha: string) => void` on `HistoryNav`. Task 2 calls `selectAndOpen`.

- [ ] **Step 1: Write the failing tests**

Append inside the existing `describe("useHistoryNav")` in `frontend/src/cockpit/historyNav.test.ts`. The file already provides `wrapperFor(initial)` and imports `renderHook` and `act` — use them; add no second harness.

```ts
it("selectAndOpen fills the first slot AND opens the History tab in one write", () => {
  const { result } = renderHook(() => useHistoryNav(), {
    wrapper: wrapperFor("/cockpit/ws1?tab=draft"),
  });
  act(() => result.current.selectAndOpen("abc1234"));
  expect(result.current.a).toBe("abc1234");
});

it("selectAndOpen follows the same rule as toggle for the second slot", () => {
  const { result } = renderHook(() => useHistoryNav(), {
    wrapper: wrapperFor("/cockpit/ws1?tab=history&a=abc1234"),
  });
  act(() => result.current.selectAndOpen("def5678"));
  expect(result.current.a).toBe("abc1234");
  expect(result.current.b).toBe("def5678");
});

it("selectAndOpen deselects a sha that is already selected", () => {
  const { result } = renderHook(() => useHistoryNav(), {
    wrapper: wrapperFor("/cockpit/ws1?tab=history&a=abc1234&b=def5678"),
  });
  act(() => result.current.selectAndOpen("def5678"));
  expect(result.current.a).toBe("abc1234");
  expect(result.current.b).toBeNull();
});

it("selectAndOpen slides the window along when both slots are full", () => {
  const { result } = renderHook(() => useHistoryNav(), {
    wrapper: wrapperFor("/cockpit/ws1?tab=history&a=aaaaaaa&b=bbbbbbb"),
  });
  act(() => result.current.selectAndOpen("ccccccc"));
  expect(result.current.a).toBe("bbbbbbb");
  expect(result.current.b).toBe("ccccccc");
});

it("selectAndOpen leaves the artifact alone", () => {
  const { result } = renderHook(() => useHistoryNav(), {
    wrapper: wrapperFor("/cockpit/ws1?tab=draft&artifact=spec"),
  });
  act(() => result.current.selectAndOpen("abc1234"));
  expect(result.current.artifact).toBe("spec");
  expect(result.current.a).toBe("abc1234");
});
```

Then add a second `describe` at the top level of the same file. This one asserts the **resulting URL**, which is the only assertion that can see the bug:

```ts
describe("one URL write, not two — spec section 6.1", () => {
  // react-router 7.18.2's setSearchParams is NOT a React state setter: the
  // functional form receives params memoised from location.search AT RENDER
  // TIME, captured in a useCallback closure. Two calls in one handler both
  // build from the pre-click URL and the second discards the first.
  function Probe({ mode }: { mode: "two-writers" | "selectAndOpen" }) {
    const hist = useHistoryNav();
    const cock = useCockpitNav();
    const [sp] = useSearchParams();
    return (
      <>
        <button
          onClick={() => {
            if (mode === "two-writers") {
              hist.toggle("abc1234");
              cock.setTab("history");
            } else {
              hist.selectAndOpen("abc1234");
            }
          }}
        >
          click
        </button>
        <output data-testid="url">{sp.toString()}</output>
      </>
    );
  }

  function urlAfterClick(mode: "two-writers" | "selectAndOpen") {
    render(
      <MemoryRouter initialEntries={["/cockpit/ws1?tab=draft"]}>
        <Probe mode={mode} />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("click"));
    return screen.getByTestId("url").textContent ?? "";
  }

  it("selectAndOpen leaves BOTH the selection and the tab in the URL", () => {
    const url = urlAfterClick("selectAndOpen");
    expect(url).toContain("tab=history");
    expect(url).toContain("a=abc1234");
  });

  it("PINNED: toggle-then-setTab loses the selection — this is why selectAndOpen exists", () => {
    // Measured, not assumed. Deleting selectAndOpen and "simplifying" back to
    // two calls must break something; this is that something.
    const url = urlAfterClick("two-writers");
    expect(url).toContain("tab=history");
    expect(url).not.toContain("a=abc1234");
  });
});
```

**Replace** the file's first and third import lines — do not add a second block.
`renderHook`, `act` and `MemoryRouter` are already imported (lines 1 and 3), and
re-importing them is a duplicate-binding compile error.

```ts
// was: import { renderHook, act } from "@testing-library/react";
import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
// was: import { MemoryRouter } from "react-router-dom";
import { MemoryRouter, useSearchParams } from "react-router-dom";
```

and add one new import beside the existing `./historyNav` one:

```ts
import { useCockpitNav } from "./cockpitNav";
```

Because this file now renders JSX, **rename it** `historyNav.test.ts` → `historyNav.test.tsx`. Vitest's `include` covers both extensions; TypeScript will not compile JSX in a `.ts` file.

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm vitest run src/cockpit/historyNav.test.tsx`

Expected: FAIL — `selectAndOpen is not a function`. The PINNED test may already pass (it describes today's behaviour); that is correct and expected.

- [ ] **Step 3: Implement**

In `frontend/src/cockpit/historyNav.ts`, add above `useHistoryNav`:

```ts
/** The selection rule, as a pure function of the current pair.
 *
 *  Shared by toggle() and selectAndOpen() so the two can never disagree about
 *  what a click means — the same reason blocks() is shared by refineRuns and
 *  pairRows in diff.ts. */
export function nextSelection(
  cur: { a: string | null; b: string | null },
  sha: string,
): { a: string | null; b: string | null } {
  if (sha === cur.a) return { a: cur.b, b: null };
  if (sha === cur.b) return { a: cur.a, b: null };
  if (cur.a == null) return { a: sha, b: cur.b };
  if (cur.b == null) return { a: cur.a, b: sha };
  // Both full: drop the older slot and slide along, so a third click always
  // means "compare this with the one I picked most recently".
  return { a: cur.b, b: sha };
}
```

Add `selectAndOpen: (sha: string) => void;` to the `HistoryNav` type, with this doc comment:

```ts
  /** Apply the selection rule AND open the History tab in ONE
   *  setSearchParams write.
   *
   *  Calling toggle() then setTab() does NOT work: react-router 7.18.2 gives a
   *  functional updater the params memoised at RENDER time, not a live ref, so
   *  two writes in one handler both build from the pre-click URL and the second
   *  discards the first. Measured — see the PINNED test in historyNav.test.tsx
   *  and spec section 6.1. */
  selectAndOpen: (sha: string) => void;
```

Replace the body of `toggle` so it uses the shared rule (behaviour identical):

```ts
  const toggle = useCallback(
    (sha: string) => write(nextSelection({ a, b }, sha)),
    [a, b, write],
  );
```

Add, before the `return`:

```ts
  const selectAndOpen = useCallback(
    (sha: string) =>
      setSp((prev) => {
        const out = new URLSearchParams(prev);
        // Read from `prev`, not the render closure: this is the one write that
        // carries both changes, so it must see the params it is about to replace.
        const next = nextSelection(
          { a: shaParam(prev, "a"), b: shaParam(prev, "b") },
          sha,
        );
        for (const key of ["a", "b"] as const) {
          const v = next[key];
          if (v == null) out.delete(key);
          else out.set(key, v);
        }
        out.set("tab", "history");
        return out;
      }),
    [setSp],
  );
```

And extend the return: `return { artifact, a, b, layout, setArtifact, toggle, setLayout, selectAndOpen };`

- [ ] **Step 4: Run to verify it passes**

Run: `cd frontend && pnpm vitest run src/cockpit/historyNav.test.tsx`

Expected: all existing cases plus **7 new** (5 rule tests + 2 URL tests), all passing.

- [ ] **Step 5: Prove the shared rule is load-bearing**

Temporarily change `nextSelection`'s last line from `return { a: cur.b, b: sha };` to `return { a: cur.a, b: sha };` and re-run.

Expected: **both** "slides the window along" (selectAndOpen) and the existing toggle slide test FAIL on assertions — proving the two really do share one rule. Revert and confirm green.

- [ ] **Step 6: Typecheck and commit**

```bash
cd frontend && pnpm exec tsc --noEmit
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/cockpit/historyNav.ts frontend/src/cockpit/historyNav.test.ts frontend/src/cockpit/historyNav.test.tsx
git commit -m "feat(cockpit): selectAndOpen — select a version and open History in ONE URL write

react-router 7.18.2 hands a functional updater the params memoised at render
time, not a live ref, so toggle() followed by setTab() both build from the
pre-click URL and the second discards the first -- the selection vanishes.
Measured, and pinned as a test so nobody simplifies it back.

nextSelection is extracted so toggle and selectAndOpen cannot disagree about
what a click means.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `VersionGraph`

**Files:**
- Create: `frontend/src/cockpit/VersionGraph.tsx`, `frontend/src/cockpit/VersionGraph.module.css`, `frontend/src/cockpit/VersionGraph.test.tsx`

**Interfaces:**
- Consumes: `selectAndOpen` (Task 1); `useVersions(id, artifact)` and `VersionRow` from `../api/versions`; `SidebarGroup` from `./SidebarGroup`.
- Produces: `export function VersionGraph({ id }: { id: string }): JSX.Element`. It renders its **own** `SidebarGroup` — including the title and count — so the version data lives in one place. Task 3 drops it into `CockpitSidebar`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/cockpit/VersionGraph.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useSearchParams } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import * as versionsMod from "../api/versions";
import { VersionGraph } from "./VersionGraph";

const ROW = (sha: string, subject: string, created_by: string | null = "user") => ({
  commit_sha: sha,
  committed_at: "2026-08-28T10:00:00+00:00",
  subject,
  created_by,
  triggering_turn_id: null,
  turn_prompt: null,
});

function mockList(data: Partial<versionsMod.VersionList>) {
  vi.spyOn(versionsMod, "useVersions").mockReturnValue({
    data: { versions: [], truncated: false, history_available: true, ...data },
    isLoading: false,
    isError: false,
  } as never);
}

function UrlProbe() {
  const [sp] = useSearchParams();
  return <output data-testid="url">{sp.toString()}</output>;
}

function wrap(initial = "/cockpit/ws1?tab=draft") {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <VersionGraph id="ws1" />
      <UrlProbe />
    </MemoryRouter>,
  );
}

describe("VersionGraph", () => {
  it("renders one row per version, newest first, with sha and subject", () => {
    mockList({ versions: [ROW("aaaaaaa", "newer one"), ROW("bbbbbbb", "older one")] });
    wrap();
    const rows = screen.getAllByRole("button", { name: /aaaaaaa|bbbbbbb/ });
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain("aaaaaaa");
    expect(rows[0].textContent).toContain("newer one");
    expect(rows[1].textContent).toContain("bbbbbbb");
  });

  it("shortens the sha to 7 characters", () => {
    mockList({ versions: [ROW("0123456789abcdef0123456789abcdef01234567", "s")] });
    wrap();
    expect(screen.getByText("0123456")).toBeInTheDocument();
  });

  it("marks the role on the dot, including no provenance", () => {
    mockList({ versions: [ROW("aaaaaaa", "s", "agent"), ROW("bbbbbbb", "s", null)] });
    const { container } = wrap();
    const dots = [...container.querySelectorAll("[data-role]")].map((n) =>
      (n as HTMLElement).dataset.role,
    );
    expect(dots).toEqual(["agent", "none"]);
  });

  it("clicking a dot writes BOTH the selection and tab=history to the URL", async () => {
    // The whole point of selectAndOpen. Asserting the URL, not the calls:
    // two separate calls both happen while the feature is broken (spec 6.1).
    const user = userEvent.setup();
    mockList({ versions: [ROW("aaaaaaa", "s")] });
    wrap();
    await user.click(screen.getByRole("button", { name: /aaaaaaa/ }));
    const url = screen.getByTestId("url").textContent ?? "";
    expect(url).toContain("a=aaaaaaa");
    expect(url).toContain("tab=history");
  });

  it("marks a selected version with aria-pressed", () => {
    mockList({ versions: [ROW("aaaaaaa", "s"), ROW("bbbbbbb", "s")] });
    wrap("/cockpit/ws1?tab=history&a=aaaaaaa");
    expect(screen.getByRole("button", { name: /aaaaaaa/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /bbbbbbb/ })).toHaveAttribute("aria-pressed", "false");
  });

  it("clicking a selected version deselects it", async () => {
    const user = userEvent.setup();
    mockList({ versions: [ROW("aaaaaaa", "s")] });
    wrap("/cockpit/ws1?tab=history&a=aaaaaaa");
    await user.click(screen.getByRole("button", { name: /aaaaaaa/ }));
    expect(screen.getByTestId("url").textContent ?? "").not.toContain("a=aaaaaaa");
  });

  it("says there are no versions rather than showing an empty group", () => {
    mockList({ versions: [] });
    wrap();
    expect(screen.getByText(/No versions yet/i)).toBeInTheDocument();
  });

  it("distinguishes an unreadable history from an empty one", () => {
    mockList({ versions: [], history_available: false });
    wrap();
    expect(screen.getByText(/not available/i)).toBeInTheDocument();
    expect(screen.queryByText(/No versions yet/i)).toBeNull();
  });

  it("reports a failed request distinctly from an empty list", () => {
    vi.spyOn(versionsMod, "useVersions").mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
    } as never);
    wrap();
    expect(screen.getByText(/Could not load version history/i)).toBeInTheDocument();
    expect(screen.queryByText(/No versions yet/i)).toBeNull();
  });

  it("says older versions exist when the list is truncated", () => {
    mockList({ versions: [ROW("aaaaaaa", "s")], truncated: true });
    wrap();
    expect(screen.getByText(/Older versions exist/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm vitest run src/cockpit/VersionGraph.test.tsx`

Expected: FAIL — cannot resolve `./VersionGraph`.

- [ ] **Step 3: Write the stylesheet**

Create `frontend/src/cockpit/VersionGraph.module.css`:

```css
/* The lane: a dot per version, joined by a vertical line. One lane — measured
   across all 14 workspace repos, every one has a single branch and zero merge
   commits, so the graph is a straight line today (spec section 10). */
.row {
  position: relative;
}
.button {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  width: 100%;
  padding: 0.2rem 0.5rem 0.2rem 0.5rem;
  background: transparent;
  border: 0;
  text-align: left;
  color: var(--app-text);
  font-size: 0.85rem;
  cursor: pointer;
}
.button:hover {
  background: var(--app-hover, rgba(255, 255, 255, 0.06));
}
.button[aria-pressed="true"] {
  background: var(--app-sidebar);
  outline: 1px solid var(--app-accent);
  outline-offset: -1px;
}

/* Fixed-width lane so every dot lines up regardless of sha or subject length. */
.lane {
  position: relative;
  flex: 0 0 12px;
  align-self: stretch;
  min-height: 1.4em;
}
/* The connector. Drawn on the lane rather than between rows so the last row
   simply has no line below it — no special case, no :last-child rule. */
.lane::before {
  content: "";
  position: absolute;
  left: 5px;
  top: 0;
  bottom: -0.4rem;
  width: 2px;
  background: var(--app-border);
}
.row:last-child .lane::before {
  bottom: 50%;
}
.dot {
  position: absolute;
  left: 1px;
  top: calc(50% - 5px);
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--app-muted);
  border: 2px solid var(--app-editor);
  box-sizing: border-box;
}
.dot[data-role="agent"] { background: #4c8dff; }
.dot[data-role="user"] { background: #3fb950; }
.dot[data-role="reconciled"] { background: #d29922; }
.dot[data-role="none"] { background: var(--app-muted); }

.sha {
  flex: 0 0 auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.78rem;
  color: var(--app-muted);
}
/* 276px is the pane's default width; the subject is what gives way. `min-width:
   0` is load-bearing — without it a flex item refuses to shrink below its
   content and the row overflows the pane instead of ellipsising. */
.subject {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.note {
  color: var(--app-muted);
  padding: 0.25rem 0.5rem;
  font-size: 0.85rem;
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
}
```

- [ ] **Step 4: Write the component**

Create `frontend/src/cockpit/VersionGraph.tsx`:

```tsx
import { useVersions } from "../api/versions";
import { useHistoryNav } from "./historyNav";
import { SidebarGroup } from "./SidebarGroup";
import styles from "./VersionGraph.module.css";

/** The cockpit side pane's Versions group, drawn as a commit graph.
 *
 *  Owns its own SidebarGroup (title + count) so the version data has exactly
 *  one reader — the group header's count and the rows cannot disagree.
 *
 *  Git-sourced, via api/versions. The DB-sourced list this replaces could omit
 *  versions in principle: four routine commit paths write no artifact_versions
 *  row. On live data the two agreed exactly, so this is a layout and
 *  duplication change, not a correctness fix. */
export function VersionGraph({ id }: { id: string }) {
  const nav = useHistoryNav();
  const list = useVersions(id, nav.artifact);
  const data = list.data;
  const rows = data?.versions ?? [];

  return (
    <SidebarGroup title="Versions" count={rows.length || undefined} defaultOpen={false}>
      {/* Each state gets its own words. "Could not load" and "not available"
          and "none yet" are three different situations, and a single empty
          message for all three tells the user nothing about which. */}
      {list.isError && <p className={styles.note}>Could not load version history.</p>}
      {!list.isError && data && !data.history_available && (
        <p className={styles.note}>Version history is not available yet.</p>
      )}
      {!list.isError && data?.history_available && rows.length === 0 && (
        <p className={styles.note}>No versions yet.</p>
      )}

      {rows.length > 0 && (
        <ul className={styles.list} aria-label="Versions">
          {rows.map((v) => {
            const selected = v.commit_sha === nav.a || v.commit_sha === nav.b;
            return (
              <li key={v.commit_sha} className={styles.row}>
                <button
                  type="button"
                  className={styles.button}
                  aria-pressed={selected}
                  // The full subject and exact time do not fit at 276px; the
                  // tooltip is where they live until the pane is dragged wider.
                  title={`${v.subject} — ${new Date(v.committed_at).toLocaleString()}`}
                  onClick={() => nav.selectAndOpen(v.commit_sha)}
                >
                  <span className={styles.lane} aria-hidden="true">
                    <span className={styles.dot} data-role={v.created_by ?? "none"} />
                  </span>
                  <span className={styles.sha}>{v.commit_sha.slice(0, 7)}</span>
                  <span className={styles.subject}>{v.subject}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}

      {data?.truncated && (
        <p className={styles.note}>
          {/* No number: it would duplicate the backend's HISTORY_LIMIT across
              the wire and silently lie if that constant moved. */}
          Older versions exist than are shown here.
        </p>
      )}
    </SidebarGroup>
  );
}
```

- [ ] **Step 5: Run to verify it passes**

Run: `cd frontend && pnpm vitest run src/cockpit/VersionGraph.test.tsx`

Expected: **10 tests passed**.

- [ ] **Step 6: Prove the click test can fail**

Temporarily change `onClick={() => nav.selectAndOpen(v.commit_sha)}` to `onClick={() => nav.toggle(v.commit_sha)}` and re-run.

Expected: "clicking a dot writes BOTH the selection and tab=history" **FAILS** on the missing `tab=history`. This is the whole spec §6 mechanism, verified end to end at the call site. Revert and confirm green.

- [ ] **Step 7: Typecheck, build, commit**

```bash
cd frontend && pnpm exec tsc --noEmit && pnpm build
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/cockpit/VersionGraph.tsx frontend/src/cockpit/VersionGraph.module.css frontend/src/cockpit/VersionGraph.test.tsx
git commit -m "feat(cockpit): VersionGraph — the Versions group as a commit graph

Owns its own SidebarGroup so the header count and the rows read the same
data. One lane: measured across all 14 workspace repos, every one has a
single branch and zero merge commits.

At 276px the subject is what gives way; min-width:0 on it is load-bearing,
or the row overflows the pane instead of ellipsising.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Drop it into the side pane, delete the dead hook

**Files:**
- Modify: `frontend/src/cockpit/CockpitSidebar.tsx`, `frontend/src/cockpit/CockpitSidebar.test.tsx`, `frontend/src/api/workspace.ts`

**Interfaces:**
- Consumes: `VersionGraph` (Task 2).

- [ ] **Step 1: Write the failing test**

`CockpitSidebar.test.tsx` stubs `/versions` on four lines — **167, 190, 216, 237**. Line 178 is `findByRole("button", { name: /versions/i })`, which targets the group's disclosure button and **must not change** (the group is still called "Versions").

Change each of the four stubs from the DB shape to the git shape. Where a stub reads:

```ts
    if (path.endsWith("/versions")) return Promise.resolve({ data: [], error: undefined });
```

it becomes:

```ts
    if (path.includes("/history"))
      return Promise.resolve({
        data: { versions: [], truncated: false, history_available: true },
        error: undefined,
      });
```

`includes`, not `endsWith`: the history request carries `?artifact=draft`, so an `endsWith("/history")` stub never matches and the component falls through to the real client.

**Line 167's stub needs its FIELD NAMES changed, not just its envelope.** It currently returns a DB-shaped row:

```ts
{ commit_sha: "a".repeat(40), message: "user spec init", created_by: "user", created_at: "2026-06-24T00:00:00Z" }
```

`VersionGraph` renders `v.subject` and reads `v.committed_at`. Wrapping this row unchanged leaves "user spec init" **unrendered**, and the existing assertion on line 179 (`findByText("user spec init")`) fails. It becomes:

```ts
    if (path.includes("/history"))
      return Promise.resolve({
        data: {
          versions: [
            {
              commit_sha: "a".repeat(40),
              committed_at: "2026-06-24T00:00:00Z",
              subject: "user spec init",
              created_by: "user",
              triggering_turn_id: null,
              turn_prompt: null,
            },
          ],
          truncated: false,
          history_available: true,
        },
        error: undefined,
      });
```

`message` → `subject`, `created_at` → `committed_at`, plus the two fields `VersionRow` requires. Line 179's assertion then passes unchanged.

Then add one new test, using the file's existing `renderSidebar()` helper (line 62):

```ts
it("renders the version GRAPH, not the old plain list", async () => {
  // Must assert something only the graph produces. "No versions yet." is NOT
  // that: the old sidebar renders those exact words too (CockpitSidebar.tsx:139),
  // so an empty-list assertion passes either way and proves nothing.
  vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.includes("/history"))
      return Promise.resolve({
        data: {
          versions: [
            {
              commit_sha: "abcdef1234567890abcdef1234567890abcdef12",
              committed_at: "2026-06-24T00:00:00Z",
              subject: "user spec init",
              created_by: "agent",
              triggering_turn_id: null,
              turn_prompt: null,
            },
          ],
          truncated: false,
          history_available: true,
        },
        error: undefined,
      });
    if (path.endsWith("/members")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/cockpit-meta"))
      return Promise.resolve({ data: { workspace_title: "W", workspace_deadline: null, current_phase: "active", role: "owner", spec_locked: false, member_count: 1 }, error: undefined });
    if (path.endsWith("/references")) return Promise.resolve({ data: [], error: undefined });
    if (path.endsWith("/scorecard")) return Promise.resolve({ data: null, error: undefined });
    return Promise.resolve(draft as never);
  }) as never);
  renderSidebar();
  fireEvent.click(await screen.findByRole("button", { name: /versions/i }));
  // Graph-only output: the 7-char sha and a role dot. The old list rendered
  // neither — it showed the full message and the role as plain text.
  expect(await screen.findByText("abcdef1")).toBeInTheDocument();
  expect(document.querySelector('[data-role="agent"]')).not.toBeNull();
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm vitest run src/cockpit/CockpitSidebar.test.tsx`

Expected: FAIL — the sidebar still calls `/versions`, so the `/history` stubs go unused and the group renders the old list.

- [ ] **Step 3: Implement**

In `frontend/src/cockpit/CockpitSidebar.tsx`:

Change the import on line 11 from

```ts
import { useCockpitMeta, useMembers, useVersions } from "../api/workspace";
```

to

```ts
import { useCockpitMeta, useMembers } from "../api/workspace";
```

Add `import { VersionGraph } from "./VersionGraph";` beside the other local imports.

Delete these two lines (currently 44-45):

```ts
  const versions = useVersions(id);
  const versionRows = Array.isArray(versions.data) ? versions.data : [];
```

Replace the whole `Versions` `SidebarGroup` block with one line:

```tsx
      <VersionGraph id={id} />
```

In `frontend/src/api/workspace.ts`, delete the `useVersions` function and its doc comment (lines 20-32). Its only caller was `CockpitSidebar.tsx:44`.

**Leave the backend route `GET /api/workspaces/{id}/versions` alone.** It will have no frontend consumer; removing it touches the OpenAPI schema and generated types and is a separate decision (spec §4).

- [ ] **Step 4: Run to verify it passes**

Run: `cd frontend && pnpm vitest run src/cockpit/CockpitSidebar.test.tsx src/cockpit/VersionGraph.test.tsx`

Expected: both files green, with the sidebar file's count up by 1.

- [ ] **Step 5: Confirm the dead hook is really gone**

```bash
cd frontend && grep -rn "from \"../api/workspace\"" src | grep -i version || echo "no version import from api/workspace remains"
grep -rn "useVersions" src/api/workspace.ts || echo "hook deleted"
```

Expected: both lines print their "no"/"deleted" message. A hook left behind with no caller is dead code the next reader has to reason about.

- [ ] **Step 6: Typecheck and commit**

```bash
cd frontend && pnpm exec tsc --noEmit
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/cockpit/CockpitSidebar.tsx frontend/src/cockpit/CockpitSidebar.test.tsx frontend/src/api/workspace.ts
git commit -m "feat(cockpit): the side pane's Versions group is the graph

Collapses a duplicate: the pane was DB-sourced while the History tab was
git-sourced, showing the same versions from two endpoints. The pane now
reads the git-sourced list and api/workspace's useVersions -- whose only
caller was CockpitSidebar:44 -- is deleted.

The sidebar test's /versions stubs become /history and must use includes(),
not endsWith(): the request carries ?artifact=draft.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Strip the History tab

**Files:**
- Modify: `frontend/src/cockpit/HistoryPanel.tsx`, `frontend/src/cockpit/HistoryPanel.test.tsx`

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/cockpit/HistoryPanel.test.tsx`, using its existing `wrap`, `ROW`, `mockList`, `mockVersion` and `mockPair` helpers:

```tsx
it("no longer renders version rows — the side pane owns them now", () => {
  mockList({ versions: [ROW("aaaaaaa", "one"), ROW("bbbbbbb", "two")] });
  mockVersion(null);
  wrap(<HistoryPanel id="ws1" />);
  expect(screen.queryByRole("button", { name: /one/ })).toBeNull();
  expect(screen.queryByRole("button", { name: /two/ })).toBeNull();
});

it("tells you where to pick versions when NEITHER is selected", () => {
  mockList({ versions: [ROW("aaaaaaa", "one"), ROW("bbbbbbb", "two")] });
  mockVersion(null);
  wrap(<HistoryPanel id="ws1" />);
  expect(screen.getByText(/side pane/i)).toBeInTheDocument();
});

it("still reads ONE selected version — the empty state is not 'fewer than two'", () => {
  // A single selection renders a read-only document today and must keep doing
  // so. Gating the empty state on "fewer than two" would replace working
  // behaviour with a placeholder.
  mockList({ versions: [ROW("aaaaaaa", "one"), ROW("bbbbbbb", "two")] });
  mockVersion("# an old draft\n");
  wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=aaaaaaa");
  expect(screen.getByText(/# an old draft/)).toBeInTheDocument();
  expect(screen.queryByText(/side pane/i)).toBeNull();
});

it("points a failed single read at the side pane, not at a list above", () => {
  mockList({ versions: [ROW("bbbbbbb", "newer")] });
  vi.spyOn(versionsMod, "useVersion").mockReturnValue({
    data: null,
    isLoading: false,
    isError: true,
  } as never);
  wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&b=bbbbbbb");
  const msg = screen.getByText(/could not be read/i);
  expect(msg.textContent).toMatch(/side pane/i);
  expect(msg.textContent).not.toMatch(/above/i);
});

it("points the not-in-this-list message at the side pane", () => {
  mockList({ versions: [ROW("bbbbbbb", "newer"), ROW("aaaaaaa", "older")] });
  mockVersion(null);
  wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=abc1234&b=bbbbbbb");
  const msg = screen.getByText(/not in the Versions list/i);
  expect(msg.textContent).not.toMatch(/above/i);
});

it("no message in this panel still says 'above'", () => {
  // A blanket guard: three messages were reworded, and the next one added must
  // not reintroduce the phrase.
  mockList({ versions: [ROW("aaaaaaa", "one")] });
  mockVersion(null);
  const { container } = wrap(<HistoryPanel id="ws1" />, "/cockpit/ws1?tab=history&a=abc1234&b=aaaaaaa");
  expect(container.textContent ?? "").not.toMatch(/from the list above|from above/i);
});
```

Delete from this file the **six** tests that depend on version rows existing in this panel:

| Test | Equivalent in `VersionGraph.test.tsx`? |
|---|---|
| "renders a row's role badge and its subject verbatim" | yes — "renders one row per version…" + "marks the role on the dot" |
| "shows WHY a version exists when a prompt caused it" | **NO** — see below |
| "shows no reason line at all when there is no prompt" | **NO** — see below |
| "renders a row with no provenance as ordinary, not as an error" | yes — "marks the role on the dot, including no provenance" |
| "says so when the list is truncated" | yes — "says older versions exist when the list is truncated" |
| **"selecting a row puts its sha in the URL"** (`HistoryPanel.test.tsx:176-190`) | yes — "marks a selected version with aria-pressed" + "clicking a dot writes BOTH…" |

That last one is easy to miss: it calls `getByRole("button", { name: /one/ })`, and Task 4 deletes every `VersionRowButton`, so it cannot pass. It must go.

**The turn-prompt behaviour has no home.** Two of the six tests cover "because you asked: …" — the line showing which instruction produced a version — and `VersionGraph` as written in Task 2 does **not** render `turn_prompt`. The implementer must either add it to `VersionGraph.tsx` and cover it in `VersionGraph.test.tsx` (adjusting the counts below), or record in the task report that it was dropped deliberately. **Silently losing it is a failed task.**

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm vitest run src/cockpit/HistoryPanel.test.tsx`

Expected: FAIL — no "side pane" copy, and version rows still render.

- [ ] **Step 3: Implement**

In `frontend/src/cockpit/HistoryPanel.tsx`:

Delete the `VersionRowButton` component entirely (it has no other caller), and the `Badge` import if nothing else uses it.

Delete the block that renders the rows and the truncation note — from `{data && data.versions.length > 0 && (` through its closing `)}`. The truncation note now lives in the side pane.

Delete the `{data && data.versions.length === 1 && (` "Comparing needs two versions." block: with the list gone this is advice about a control that is no longer here, and the new empty state covers the case.

Add the empty state, immediately before the one-selected read block:

```tsx
      {/* NEITHER selected — not "fewer than two". One selected renders as a
          read-only document below, and that behaviour is kept. */}
      {nav.a == null && nav.b == null && data && data.versions.length > 0 && (
        <Text size="sm" c="dimmed">
          Pick two versions from the Versions list in the side pane to compare them.
        </Text>
      )}
```

Reword the three messages that point at a list which is no longer above them:

```tsx
          That version could not be read. Pick one from the Versions list in the side pane.
```

```tsx
          One of those versions could not be read. Pick two from the Versions list in the side
          pane.
```

```tsx
          One of the selected versions is not in the Versions list. Pick two from the side pane.
```

Keep every other state exactly as it is: loading, `list.isError`, `history_available: false`, zero versions, "Loading both versions…", the one-version read, the layout toggle, the compare view and the too-large fallback.

- [ ] **Step 4: Run to verify it passes**

Run: `cd frontend && pnpm vitest run src/cockpit/HistoryPanel.test.tsx`

Expected: green, with the file's test count reflecting 6 added and 5 removed.

- [ ] **Step 5: Run the whole frontend gate**

```bash
cd frontend && pnpm exec tsc --noEmit && pnpm vitest run && pnpm build
```

Expected: tsc clean, and exactly:

```
Test Files  116 passed (116)
Tests       708 passed (708)
```

Derived, not guessed:

| | Files | Tests |
|---|---|---|
| Baseline (master `e142e198`, measured by running the suite) | 115 | 689 |
| Task 1 — `historyNav.test.ts` → `.tsx` | +0 (a rename is not a new file) | +7 |
| Task 2 — `VersionGraph.test.tsx` | +1 | +10 |
| Task 3 — `CockpitSidebar.test.tsx` | +0 | +1 |
| Task 4 — `HistoryPanel.test.tsx` | +0 | +6 −6 = **0** |
| **Total** | **116** | **707** |

The Task 4 row is **six** deletions, not five: the sixth is "selecting a row puts its sha in the URL", which cannot survive the removal of `VersionRowButton`. Adding turn-prompt coverage to `VersionGraph` (Task 4 Step 1) would raise both the Task 2 file's count and this total — if you do that, say so and give the new number rather than reporting a mismatch.

**If your total differs, name the term that changed — never adjust the expectation to make it close.** A file count of 115 means `VersionGraph.test.tsx` was not collected and its ten tests never ran; vitest reports that as success.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/cockpit/HistoryPanel.tsx frontend/src/cockpit/HistoryPanel.test.tsx
git commit -m "feat(cockpit): History tab drops its list, gains an empty state

The list lives in the side pane now. The empty state is gated on NEITHER
sha selected, not 'fewer than two' -- one selected version renders a
read-only document and that behaviour is kept.

Three messages said 'Pick ... from the list above'. The list is not above
anything any more. Keeping a state working is not the same as keeping its
words true.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: The browser gate

jsdom performs no layout. No test above can show that a row fits at 276px, that the line joins the dots, or that the diff did not lose width.

**Files:**
- Create: `<scratchpad>/graph-gate.mjs` — scratch, **not** committed, **not** under `frontend/` or `src/`.

**Isolation:** identical to the rules the previous slice ran under. Build inside the repo, serve the build on **8011**, rewrite `/api` reads to the app already on **8000**, **abort any non-GET**, never restart the app, never write `src/call_manager/web/spa/`.

- [ ] **Step 1: Build and serve**

```bash
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm build
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/
node /home/parlar_ai/sbs-run/serve.mjs /home/parlar_ai/dev/call-monitor/frontend/dist 8011 &
sleep 1
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8011/cockpit/312f51e2-26d2-4ad8-9526-55222e4db60a
```

Expected: `200` from both. `serve.mjs` already exists from the previous slice; if it is missing, recreate it — a plain `python3 -m http.server` 404s on `/cockpit/<uuid>` and wastes the gate.

**If 8000 is not 200, do NOT start it.** Record that the gate could not run and finish without it.

- [ ] **Step 2: Write the gate**

Create `graph-gate.mjs` in the scratchpad:

```js
import { chromium } from '/home/parlar_ai/dev/call-monitor/frontend/node_modules/.pnpm/playwright@1.60.0/node_modules/playwright/index.mjs';
import fs from 'node:fs';

const WS = fs.readFileSync('/home/parlar_ai/sbs-run/workspace.txt', 'utf8').trim();
const cookie = fs.readFileSync('/home/parlar_ai/sbs-run/cookie.txt', 'utf8').trim();
const APP = 'http://127.0.0.1:8000';
const SPA = 'http://127.0.0.1:8011';

const b = await chromium.launch();
const ctx = await b.newContext({ viewport: { width: 1600, height: 1000 } });
await ctx.addCookies([{ name: 'session', value: cookie, domain: '127.0.0.1', path: '/' }]);
const p = await ctx.newPage();
p.on('pageerror', (e) => console.log('  [pageerror]', String(e).slice(0, 300)));
await p.route('**/api/**', (route) => {
  const req = route.request();
  if (req.method() !== 'GET') { console.log('  [BLOCKED non-GET]', req.method(), req.url()); return route.abort(); }
  return route.continue({ url: req.url().replace(SPA, APP) });
});

await p.goto(`${SPA}/cockpit/${WS}?tab=history&artifact=draft`, { waitUntil: 'networkidle' });
// Open the Versions group (it is collapsed by default).
await p.getByRole('button', { name: /versions/i }).first().click();
await p.waitForSelector('ul[aria-label="Versions"] button', { timeout: 20000 });

const pane = await p.evaluate(() => {
  const ul = document.querySelector('ul[aria-label="Versions"]');
  const rows = [...ul.querySelectorAll('button')];
  let sc = ul; while (sc && sc !== document.body) { const o = getComputedStyle(sc).overflowY; if (o === 'auto' || o === 'scroll') break; sc = sc.parentElement; }
  const dots = [...ul.querySelectorAll('[data-role]')].map((d) => Math.round(d.getBoundingClientRect().left));
  return {
    rowCount: rows.length,
    paneWidth: Math.round(sc.clientWidth),
    // Rows must fit the pane: overflow here is the 276px failure mode.
    rowOverflow: rows.some((r) => r.scrollWidth > r.clientWidth + 1),
    paneOverflow: sc.scrollWidth > sc.clientWidth,
    // Every dot on one lane: all left offsets identical.
    dotsAligned: new Set(dots).size === 1,
  };
});
console.log('PANE:', JSON.stringify(pane));

// Click two dots; the tab must open AND both selections must survive.
const rows = await p.$$('ul[aria-label="Versions"] button');
await rows[1].click();
await rows[0].click();
await p.waitForSelector('[data-testid="side-by-side-diff"]', { timeout: 20000 });
const after = await p.evaluate(() => {
  const sp = new URLSearchParams(location.search);
  const d = document.querySelector('[data-testid="side-by-side-diff"]');
  const c = d.querySelector('[data-side="left"][data-cell]');
  return {
    tab: sp.get('tab'), a: sp.get('a'), b: sp.get('b'),
    diffContainerWidth: Math.round(d.getBoundingClientRect().width),
    leftColumnWidth: Math.round(c.getBoundingClientRect().width),
  };
});
console.log('AFTER TWO CLICKS:', JSON.stringify(after));
await p.screenshot({ path: '/home/parlar_ai/sbs-run/graph-gate.png' });

// Spec section 11.2 item 4: dragging the pane wider must show MORE subject.
// A pane that resizes while the text stays clipped to its old width is a real
// failure mode (a fixed width on the row, or a missing min-width:0).
const handle = await p.$('[aria-label="Resize sidebar"]');
const before = await p.evaluate(() => {
  const s = document.querySelector('ul[aria-label="Versions"] [class*="subject"]');
  return { w: Math.round(s.getBoundingClientRect().width), scrollW: s.scrollWidth };
});
const hb = await handle.boundingBox();
await p.mouse.move(hb.x + hb.width / 2, hb.y + hb.height / 2);
await p.mouse.down();
await p.mouse.move(hb.x + 260, hb.y + hb.height / 2, { steps: 10 });
await p.mouse.up();
await p.waitForTimeout(400);
const afterDrag = await p.evaluate(() => {
  const ul = document.querySelector('ul[aria-label="Versions"]');
  const s = ul.querySelector('[class*="subject"]');
  const rows = [...ul.querySelectorAll('button')];
  return {
    w: Math.round(s.getBoundingClientRect().width),
    rowOverflow: rows.some((r) => r.scrollWidth > r.clientWidth + 1),
  };
});
console.log('RESIZE:', JSON.stringify({ subjectWidthBefore: before.w, subjectWidthAfter: afterDrag.w, grew: afterDrag.w > before.w + 20, rowOverflowAfter: afterDrag.rowOverflow }));
await p.screenshot({ path: '/home/parlar_ai/sbs-run/graph-gate-wide.png' });
await b.close();
```

- [ ] **Step 3: Run it and check every field**

Run: `node <scratchpad>/graph-gate.mjs`

| Field | Must be | Why |
|---|---|---|
| `rowCount` | > 0 | the graph rendered |
| `rowOverflow` | `false` | rows fit at the pane's width |
| `paneOverflow` | `false` | no horizontal scrollbar in the pane |
| `dotsAligned` | `true` | one lane — every dot at the same offset |
| `after.tab` | `"history"` | the click opened the tab |
| `after.a` and `after.b` | both non-null | **the §6.1 bug** — a tab that opened with no selection is the failure |
| `after.diffContainerWidth` | **>= 900** | no-regression vs the recorded baseline |
| `after.leftColumnWidth` | **>= 449** | ditto |
| `[BLOCKED non-GET]` | never printed | the gate mutated nothing |
| `RESIZE.grew` | `true` | dragging the pane wider really shows more subject |
| `RESIZE.rowOverflowAfter` | `false` | and it still does not overflow at the new width |

The width rows are a **no-regression** check, not a gain. Spec §2.1: this change buys vertical space; the diff container was already 900px. Do not rewrite these as "wider than before" — that is unsatisfiable and would get relaxed.

- [ ] **Step 4: Prove the gate can fail**

Temporarily set `.subject { min-width: 0 }` to `min-width: 400px` in `VersionGraph.module.css`, rebuild, re-run.

Expected: **`rowOverflow` or `paneOverflow` becomes `true`.** Revert, rebuild, re-run, confirm every field is back. If neither flips, the overflow check is measuring the wrong element — record that as a defect and say so plainly.

- [ ] **Step 5: Look at the screenshot**

Read `/home/parlar_ai/sbs-run/graph-gate.png` and actually look at it. Confirm: dots in a single vertical line joined by a connector, two of them visibly selected, short shas readable, subjects ellipsised rather than wrapped or clipped mid-row, and the diff filling the editor beside them.

- [ ] **Step 6: Stop the server and record**

Kill `serve.mjs` by its pid. Confirm `curl 127.0.0.1:8000/` is still `200`. Paste both runs' JSON and what the screenshot showed into the task report. No commit — this task produces no tracked files.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §2.1 vertical-only, 900/449 baseline | Global Constraints; Task 5 Step 3 |
| §4 one source, delete the dead hook | Task 3 Steps 3, 5 |
| §5 row content at 276px | Task 2 Steps 3-4; Task 5 `rowOverflow` |
| §6.1 the two-writer bug | Task 1 Step 1 (PINNED test) |
| §6.2 `selectAndOpen` | Task 1 Step 3; Task 2 Step 6 mutation |
| §7 artifact from `?artifact` | Task 2 (`useVersions(id, nav.artifact)`); Task 1's artifact test |
| §8 empty state on NEITHER selected | Task 4 Step 1 (two tests, incl. one-version read) |
| §8.1 three reworded messages | Task 4 Steps 1, 3, plus a blanket "no 'above'" guard |
| §9 side-pane states | Task 2 Step 1 (four state tests) |
| §10 one lane | Task 2 Step 3 comment; Task 5 `dotsAligned` |
| §11.2 browser gate + fail-proof | Task 5 Steps 3-4 |

**Placeholder scan:** none. Every code step carries its code; every command its expected output.

**Type consistency:** `nextSelection` and `selectAndOpen` (Task 1) are consumed by name in Task 2. `VersionGraph({ id })` matches Task 3's call site. `VersionList` / `VersionRow` come from `api/versions`, unchanged.

**Known risk, called out rather than hidden:** Task 4 Step 1 deletes five list-rendering tests from `HistoryPanel.test.tsx`. Four have equivalents in `VersionGraph.test.tsx`; the **turn-prompt** ("because you asked: …") behaviour does **not**, and `VersionGraph` as specced does not render it. The implementer must either add it to the graph and its tests, or record in the task report that it was dropped deliberately. Silently losing it is a failed task.
